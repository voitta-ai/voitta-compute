"""macOS menu-bar shell for the Voitta backend.

Wraps the existing FastAPI app in a `rumps.App` so the user can launch
Voitta by double-clicking a `.app` bundle instead of running
`./run.sh`. The backend itself is unchanged: uvicorn runs on a daemon
thread; the rumps event loop owns the main thread (Cocoa requires it).

Layout:
  • Status-bar icon (text "V" — replace with a `.png` later if you want
    a glyph). Click → menu drops down.
  • Menu items:
        About Voitta
        Open in browser
        Settings…
        Quit
  • LSUIElement (no Dock icon) is set automatically by rumps when the
    bundle's `Info.plist` has it; for `python -m app.desktop` runs from
    a checkout, the Dock icon stays — fine for development.

Mutable state location:
  • Source checkout: `<repo>/python_storage/`, `<repo>/scripts/`, etc.
    (unchanged — picks up `app.config.PROJECT_ROOT` from `parents[2]`).
  • Frozen .app: `~/Library/Application Support/Voitta/`. The build
    script sets `VOITTA_PROJECT_ROOT` in the bundled launcher so the
    same config.py code path picks the writable directory.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

# IMPORTANT: this import must come before importing rumps. rumps
# eagerly initialises NSApp on import; if we set `VOITTA_PROJECT_ROOT`
# AFTER `app.config` has loaded, the path constants are already frozen
# at the wrong location. The launcher script (or the py2app stub) is
# responsible for setting the env var before this module is imported.
from app.config import (  # noqa: E402  (env-var sequencing comment above)
    HOST,
    PORT,
    PROJECT_ROOT,
    TLS_CERT_PATH,
    TLS_KEY_PATH,
)

import rumps  # noqa: E402

APP_NAME = "Voitta Bookmarklet"
APP_VERSION = "0.1.0"
ABOUT_TEXT = (
    f"Version {APP_VERSION}\n\n"
    "Local FastAPI backend that powers the Voitta bookmarklet — a\n"
    "right-side chat sidebar injectable into any HTTPS page.\n\n"
    "Click the bookmarklet on any page to open the chat."
)


def _server_url() -> str:
    scheme = "https" if TLS_CERT_PATH.exists() and TLS_KEY_PATH.exists() else "http"
    return f"{scheme}://{HOST}:{PORT}"


def _human_path(p: Path) -> str:
    """``/Users/roman/Library/...`` → ``~/Library/...``.

    Long absolute paths in NSAlert wrap at the dialog edge and look
    ugly; abbreviating the home prefix takes ~10 characters off and
    matches what the user sees in Finder / Terminal.
    """
    s = str(p)
    home = str(Path.home())
    return s.replace(home, "~", 1) if s.startswith(home) else s


def _start_uvicorn(log: logging.Logger) -> threading.Thread:
    """Launch uvicorn on a daemon thread. Daemon=True so the rumps quit
    handler doesn't have to coordinate shutdown — the OS reaps the
    thread when the main process exits."""

    # Diagnostic — log every entry to this function with a stack
    # trace, so when we see two "uvicorn started:" lines in voitta.log
    # we know exactly which call path led to each.
    import os, traceback
    log.info(
        "_start_uvicorn called from pid=%d ppid=%d; stack:\n%s",
        os.getpid(), os.getppid(),
        "".join(traceback.format_stack(limit=8)),
    )

    import uvicorn  # local import: keeps rumps cold-start fast
    from app.main import app as fastapi_app

    use_tls = TLS_CERT_PATH.exists() and TLS_KEY_PATH.exists()
    config_kwargs = dict(
        host=HOST,
        port=PORT,
        log_level="info",
        access_log=False,
    )
    if use_tls:
        config_kwargs["ssl_certfile"] = str(TLS_CERT_PATH)
        config_kwargs["ssl_keyfile"] = str(TLS_KEY_PATH)

    config = uvicorn.Config(fastapi_app, **config_kwargs)
    server = uvicorn.Server(config)

    def _run():
        try:
            server.run()
        except Exception:  # noqa: BLE001
            log.exception("uvicorn crashed")

    t = threading.Thread(target=_run, name="voitta-uvicorn", daemon=True)
    t.start()
    log.info(
        "uvicorn started: %s  (cert=%s)",
        _server_url(),
        "yes" if use_tls else "NO — HTTP only, mixed-content will block bookmarklet",
    )
    return t


class VoittaMenuBarApp(rumps.App):
    def __init__(self) -> None:
        # title="V" is the idle-state glyph. The activity poller below
        # swaps it for emoji squares while tools run — see
        # app.activity for the category → glyph mapping.
        super().__init__(APP_NAME, title="V", quit_button=None)
        self.menu = [
            rumps.MenuItem(f"About {APP_NAME}", callback=self.show_about),
            None,  # separator
            rumps.MenuItem("Open in browser", callback=self.open_browser),
            rumps.MenuItem("Settings…", callback=self.show_settings),
            None,
            rumps.MenuItem("Show data folder", callback=self.show_data_folder),
            rumps.MenuItem("Reset…", callback=self.reset),
            None,
            rumps.MenuItem(f"Quit {APP_NAME}", callback=self.quit_app),
        ]
        self._log = logging.getLogger("voitta.desktop")

        # Poll the activity registry every ~400 ms and update the menu
        # bar glyph. 400 ms is fast enough that short tool calls
        # (~100–500 ms) flicker visibly, slow enough that we don't
        # waste cycles on an idle backend. The Timer runs on the main
        # thread (Cocoa-safe). We use ``setAttributedTitle_`` directly
        # on the underlying NSStatusBarButton so the title text stays
        # constant ("V") while only its foreground colour changes —
        # no width-recalc, no emoji vs text dance, no duplicate-slot
        # render glitches we hit when the title was changing letterform.
        self._activity_timer = rumps.Timer(self._tick_activity, 0.4)
        self._activity_timer.start()
        self._last_color: str | None = None

    # ---- activity poller -------------------------------------------------

    def _tick_activity(self, _timer) -> None:
        from app import activity
        color_name = activity.current_color()
        if color_name == self._last_color:
            return
        self._last_color = color_name
        self._apply_colored_title(activity.current_glyph(), color_name)

    @staticmethod
    def _ns_color_for(name: str):
        """Resolve a logical colour name to the matching NSColor.

        ``"label"`` returns the dynamic system text colour so the idle
        state tracks Light/Dark mode automatically.
        """
        from AppKit import NSColor
        # NSColor presets are class methods named ``systemRedColor`` etc.
        # ``labelColor`` is the Light/Dark adaptive default.
        if name == "label":
            return NSColor.labelColor()
        method = f"system{name.capitalize()}Color"
        if hasattr(NSColor, method):
            return getattr(NSColor, method)()
        return NSColor.labelColor()

    def _apply_colored_title(self, text: str, color_name: str) -> None:
        """Set the status item's title to ``text`` rendered in
        ``color_name``. Falls back gracefully if rumps' internals
        change shape — losing colour beats crashing the app."""
        try:
            from AppKit import (
                NSAttributedString,
                NSFont,
                NSFontAttributeName,
                NSForegroundColorAttributeName,
            )
        except ImportError:
            self.title = text  # rumps default — colour-less
            return

        attrs = {
            NSForegroundColorAttributeName: self._ns_color_for(color_name),
            # Bold matches Apple's status-bar app convention; the menu
            # bar text otherwise sits visually lighter than the system
            # icons next to it.
            NSFontAttributeName: NSFont.boldSystemFontOfSize_(14),
        }
        attributed = NSAttributedString.alloc().initWithString_attributes_(
            text, attrs,
        )
        # Drill into the status bar button. rumps stores its handle on
        # ``self._nsapp.nsstatusitem``. The button is the modern (10.10+)
        # accessor — older fallback paths exist but every supported
        # macOS for this app has the button.
        try:
            button = self._nsapp.nsstatusitem.button()
        except Exception:
            self.title = text
            return
        if button is None:
            self.title = text
            return
        button.setAttributedTitle_(attributed)

    # ---- menu callbacks ---------------------------------------------------

    def show_about(self, _sender) -> None:
        rumps.alert(title=APP_NAME, message=ABOUT_TEXT, ok="OK")

    def open_browser(self, _sender) -> None:
        webbrowser.open(_server_url())

    def show_settings(self, _sender) -> None:
        # The user-facing chat settings (API keys, model selection)
        # live in the bookmarklet's localStorage, not here. This panel
        # surfaces backend runtime info — what URL to hit, where data
        # lives, install + RAG status — so the user can debug
        # "why isn't this working?" without reading source.
        #
        # NSAlert renders ``message`` in the system proportional font,
        # so trying to align rows with spaces yields jagged columns.
        # Format as a short list of "Label   value" pairs and let the
        # natural wrap handle long paths.
        from app import activity, installer, rag_build, scripts_seed

        tls_on = TLS_CERT_PATH.exists() and TLS_KEY_PATH.exists()
        active_now = activity.snapshot()

        # ---- install status -----
        install = installer.status_summary()
        if install["ok"]:
            install_line = (
                f"Install  ·  ✓ all {install['total']} packages present"
            )
        else:
            install_line = (
                f"Install  ·  ⚠ {len(install['missing'])} of "
                f"{install['total']} packages missing"
            )
            if len(install["missing"]) <= 6:
                install_line += " — " + ", ".join(install["missing"])

        # ---- seed scripts status -----
        seed = scripts_seed.status_summary()
        if seed["all_seeded"]:
            seed_line = (
                f"Scripts  ·  ✓ {len(seed['present'])} canonical scripts ready"
            )
        elif seed["expected"]:
            seed_line = (
                f"Scripts  ·  ⚠ {len(seed['missing'])} of "
                f"{len(seed['expected'])} canonical scripts missing"
            )
        else:
            seed_line = "Scripts  ·  ⚠ no seed bundle (rebuild .app)"

        # ---- RAG status -----
        rag = rag_build.status_summary()
        if rag["built"]:
            rag_line = (
                f"RAG      ·  ✓ {rag['chunk_count']} chunks across "
                f"{rag['files_count']} docs"
            )
        elif rag["last_error"]:
            rag_line = "RAG      ·  ✗ build failed (Show error log…)"
        else:
            rag_line = "RAG      ·  ⚠ not built (will build on next restart)"

        # ---- activity (what's running right now) -----
        if active_now:
            # Show up to 3 detail lines so the dialog stays compact;
            # higher concurrency is rare in practice.
            preview = ", ".join(
                f"{a['detail']} ({a['category']})" for a in active_now[:3]
            )
            if len(active_now) > 3:
                preview += f" +{len(active_now) - 3} more"
            activity_line = f"Activity ·  ▶ {preview}"
        else:
            activity_line = "Activity ·  idle"

        body = (
            f"Server  ·  {_server_url()}  ·  {'TLS' if tls_on else 'HTTP only'}\n\n"
            f"{install_line}\n"
            f"{seed_line}\n"
            f"{rag_line}\n"
            f"{activity_line}\n\n"
            f"Data folder\n  {_human_path(PROJECT_ROOT)}\n\n"
            "Chat settings — API keys, model, theme — live in the\n"
            "bookmarklet sidebar. Click the gear icon there."
        )

        # When there's a problem worth investigating (install error or
        # RAG error) add a third button that opens the relevant log
        # file in TextEdit. NSAlert returns 1000 = first button (Close),
        # 1001 = second (Open log), per Cocoa's NSAlertFirstButtonReturn.
        log_path: Path | None = None
        log_label: str | None = None
        if rag["last_error"]:
            log_path = Path(rag["bm25_dir"]).parent / "last_build_error.txt"
            log_label = "Open RAG error log"
        elif install["last_error"]:
            # Persist the in-memory installer error to disk on demand
            # (it isn't otherwise written) so we can `open` it.
            log_path = PROJECT_ROOT / "install_error.txt"
            try:
                log_path.write_text(install["last_error"], encoding="utf-8")
            except OSError:
                log_path = None
            log_label = "Open install error log"

        if log_path is not None and log_label is not None and log_path.exists():
            response = rumps.alert(
                title=APP_NAME,
                message=body,
                ok="Close",
                cancel=log_label,
            )
            # rumps.alert returns 1 for OK (Close), 0 for the cancel
            # slot (which we've repurposed as "Open log"). When the
            # user picks the log button, surface the file in TextEdit.
            if response == 0:
                subprocess.run(["open", "-e", str(log_path)], check=False)
        else:
            rumps.alert(title=APP_NAME, message=body, ok="Close")

    def show_data_folder(self, _sender) -> None:
        # Open the writable data dir in Finder for the user to inspect
        # snapshots, logs, scripts.
        if not PROJECT_ROOT.exists():
            PROJECT_ROOT.mkdir(parents=True, exist_ok=True)
        subprocess.run(["open", str(PROJECT_ROOT)], check=False)

    def reset(self, _sender) -> None:
        """Wipe the entire user data dir and quit.

        Deletes every artefact the app has ever written: settings, the
        site-packages dir of pip-installed deps, snapshots, scripts,
        TLS certs, log file. The next launch starts from scratch — the
        first-run install dialog will reappear.
        """
        confirm = rumps.alert(
            title=f"Reset {APP_NAME}?",
            message=(
                f"This deletes:\n  {PROJECT_ROOT}\n\n"
                "All settings, snapshots, scripts, and installed Python\n"
                "packages will be removed. The app will quit immediately.\n"
                "Restart it for a clean first-run setup.\n\n"
                "This cannot be undone."
            ),
            ok="Reset and quit",
            cancel="Cancel",
        )
        if not confirm:
            return
        try:
            if PROJECT_ROOT.exists():
                shutil.rmtree(PROJECT_ROOT, ignore_errors=True)
        finally:
            rumps.quit_application()

    def quit_app(self, _sender) -> None:
        # rumps's default Quit button bypasses our cleanup. Funnel
        # through here so we can shut uvicorn down explicitly later
        # (currently relies on daemon-thread reaping).
        rumps.quit_application()


def _kick_install_then_serve(log: logging.Logger) -> None:
    """First-run flow: show progress window, install heavy packages on
    a worker thread, then start uvicorn when done.

    Scheduled via ``AppHelper.callAfter`` so it runs on the main thread
    once the rumps run loop is up. The worker thread posts UI updates
    back to the main thread through the InstallWindow's ``callAfter``-
    wrapped methods.
    """
    from PyObjCTools import AppHelper

    from app import installer
    from app.install_window import InstallWindow, show_error_alert

    from app import rag_build, scripts_seed

    # Seed the curated compute + report scripts every launch — cheap
    # filesystem copy, idempotent, and recovers automatically if the
    # user's scripts/ dir got nuked (e.g. via Reset…). Errors here
    # are non-fatal; the LLM just won't have dat_parse / a4db_parse
    # available until the user fixes it.
    try:
        scripts_seed.seed()
    except Exception as exc:  # pragma: no cover — defensive
        log.warning(f"scripts_seed failed: {exc}")

    todo_count = sum(
        1 for name, _ in installer.HEAVY_PACKAGES
        if name not in installer.installed_set()
    )
    rag_needs_build = not rag_build.is_built()

    if todo_count == 0 and not rag_needs_build:
        # Both packages and RAG indexes already on disk — fast path.
        # An importlib sanity check might still fail; uvicorn surfaces
        # those naturally on the first relevant request.
        _start_uvicorn(log)
        return

    # Two-phase progress: heavy-package install ticks + RAG-build ticks.
    # We don't know the exact RAG tick count until we discover the .md
    # files, so reserve a generous budget (~20 ticks) and overshoot is
    # fine — the bar shows the relative position even if we stop early.
    rag_budget = 20 if rag_needs_build else 0
    window = InstallWindow(total=todo_count + rag_budget)
    window.show()

    def _worker():
        # Phase 1: heavy packages. Each tick label includes a friendly
        # blurb (see installer.PACKAGE_BLURBS) so the user sees what
        # "scipy" / "panel" / "chromadb" actually does.
        ok = installer.install_all(
            lambda i, _total, label, line: window.set_progress(i, label, line)
        )
        if not ok:
            window.close()
            detail = (installer.last_failure_detail
                      or "(no diagnostic was captured)")
            msg = (
                "Voitta could not finish installing its required\n"
                "Python packages. Check your internet connection and\n"
                "relaunch the app — successfully-installed packages\n"
                "are remembered so the next attempt resumes from\n"
                "where this one stopped.\n\n"
                f"{detail}"
            )
            AppHelper.callAfter(show_error_alert, msg)
            AppHelper.callAfter(rumps.quit_application)
            return

        # Phase 2: RAG indexes. Heavy packages are now on sys.path so
        # the rag_build helpers can import chromadb + bm25s. Failure
        # here is non-fatal — uvicorn still starts; only rag_query is
        # affected. Tick offset by todo_count so the progress bar
        # continues smoothly into the second phase.
        if rag_needs_build:
            rag_build.build(
                lambda i, _total, label, line: window.set_progress(
                    todo_count + min(i, rag_budget - 1), label, line,
                )
            )

        window.close()
        # Reaching here means heavy-package install succeeded
        # (``not ok`` returned early). RAG-build failures are
        # non-fatal — surfaced as a log line in the install window
        # and reflected in rag_query's "not built" envelope.
        AppHelper.callAfter(_start_uvicorn, log)

    threading.Thread(target=_worker, name="voitta-installer", daemon=True).start()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    log = logging.getLogger("voitta.desktop")

    # Make sure the data dir exists before uvicorn starts touching it.
    PROJECT_ROOT.mkdir(parents=True, exist_ok=True)

    # Build the rumps app first, but don't start uvicorn yet — the
    # heavy packages it imports may not exist on first launch. The
    # decision tree:
    #
    #   • All heavy packages importable → start uvicorn now.
    #   • Anything missing → defer uvicorn until after install runs;
    #     show progress window, install on a worker thread, then start
    #     uvicorn from the main thread when done.
    #
    # The install dispatch goes through AppHelper.callAfter so it
    # runs ONLY after rumps's NSApplicationMain has spun up the run
    # loop — Cocoa won't render an NSPanel before that.
    from app import installer

    if installer.is_complete():
        _start_uvicorn(log)
    else:
        from PyObjCTools import AppHelper
        log.info(
            "first-run install needed: %d packages",
            sum(
                1 for name, _ in installer.HEAVY_PACKAGES
                if name not in installer.installed_set()
            ),
        )
        AppHelper.callAfter(_kick_install_then_serve, log)

    # rumps.App.run() blocks on the main thread (Cocoa requirement).
    # Returns when the user picks Quit; daemon threads die with us.
    VoittaMenuBarApp().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
