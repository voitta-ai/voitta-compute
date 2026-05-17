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
        Copy bookmark text
        Settings…
        Quit
  • LSUIElement (no Dock icon) is set automatically by rumps when the
    bundle's `Info.plist` has it; for `python -m app.desktop` runs from
    a checkout, the Dock icon stays — fine for development.

Mutable state location:
  • Source checkout: `<repo>/python_storage/` (cache + compute/reports/
    flows all live under here) — picks up `app.config.PROJECT_ROOT`
    from `parents[2]`.
  • Frozen .app: `~/Library/Application Support/Voitta/`. The build
    script sets `VOITTA_PROJECT_ROOT` in the bundled launcher so the
    same config.py code path picks the writable directory.
"""

from __future__ import annotations

import logging
import os
import re
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


def _alert(*args, **kwargs):
    """Wrapper around ``rumps.alert`` that makes the modal actually
    visible from a status-bar (LSUIElement / Accessory) app.

    The problem: rumps apps run with NSApplicationActivationPolicyAccessory.
    Accessory apps CANNOT reliably steal foreground focus from another
    app — calling activateIgnoringOtherApps_ alone is not enough; the
    NSAlert opens but stays buried behind the active app, with no way
    for the user to see or dismiss it. The main thread blocks inside
    runModal() waiting for a click, and the menu freezes.

    Fix (lifted from voitta-desktop's _promote_for_keyboard pattern):
    flip to Regular activation policy for the lifetime of the modal,
    then restore Accessory. A Regular app can steal focus reliably.
    The status-bar icon stays put across policy changes — only the
    Dock icon would appear, briefly, during the alert.
    """
    try:
        from AppKit import (
            NSApplication,
            NSApplicationActivationPolicyAccessory,
            NSApplicationActivationPolicyRegular,
        )
        nsapp = NSApplication.sharedApplication()
        nsapp.setActivationPolicy_(NSApplicationActivationPolicyRegular)
        nsapp.activateIgnoringOtherApps_(True)
    except Exception:
        nsapp = None
    try:
        return rumps.alert(*args, **kwargs)
    finally:
        if nsapp is not None:
            try:
                nsapp.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
            except Exception:
                pass


def _server_url() -> str:
    scheme = "https" if TLS_CERT_PATH.exists() and TLS_KEY_PATH.exists() else "http"
    return f"{scheme}://{HOST}:{PORT}"


def _bookmarklet_source_path() -> Path | None:
    """Locate the readable bookmarklet source file.

    Source checkout: ``<repo>/bookmarklet/bookmarklet.js``.
    Packaged .app:   ``src/voitta/resources/bookmarklet/bookmarklet.js``
                     staged by ``build_app.sh``.
    """
    repo_candidate = PROJECT_ROOT / "bookmarklet" / "bookmarklet.js"
    if repo_candidate.is_file():
        return repo_candidate
    try:
        import voitta
    except ImportError:
        return None
    bundled = Path(voitta.__file__).resolve().parent / "resources" / "bookmarklet" / "bookmarklet.js"
    return bundled if bundled.is_file() else None


def _minify_bookmarklet(src: str) -> str:
    """Collapse the readable bookmarklet into a single line.

    Approximate, not a full JS minifier — but the bookmarklet has no
    ``//`` inside strings and no string spans multiple lines, so a
    line-wise comment strip + single-space join is safe and stays
    legible if someone inspects the bookmark.
    """
    cleaned = []
    for line in src.splitlines():
        idx = line.find("//")
        if idx >= 0:
            head = line[:idx]
            # Only treat ``//`` as a comment if it isn't inside an
            # unterminated string on this line.
            if head.count('"') % 2 == 0 and head.count("'") % 2 == 0:
                line = head
        line = line.strip()
        if line:
            cleaned.append(line)
    body = " ".join(cleaned)
    # Drop the optional space after ``javascript:`` for a tidier URL.
    return body.replace("javascript: ", "javascript:", 1)


def _bookmarklet_text() -> str:
    """Return the ``javascript:`` URL the user pastes into a browser
    bookmark. The embedded backend URL is rewritten on the fly so the
    bookmark matches whatever scheme/port the running server uses,
    even if the file's hard-coded value drifts."""
    path = _bookmarklet_source_path()
    if path is None:
        raise FileNotFoundError("bookmarklet/bookmarklet.js not found")
    text = _minify_bookmarklet(path.read_text(encoding="utf-8"))
    # Replace the FIRST quoted ``http(s)://...`` literal — that's the
    # backend base URL in the bookmarklet source.
    return re.sub(r'"https?://[^"]+"', f'"{_server_url()}"', text, count=1)


def _copy_to_clipboard(text: str) -> bool:
    """Write ``text`` to the macOS general pasteboard. Returns True on
    success. pyobjc is the canonical path on macOS (and is already a
    bundled dependency for the menu-bar app); ``pbcopy`` is the dev
    fallback for plain ``python -m app.desktop`` runs without pyobjc."""
    try:
        from AppKit import NSPasteboard, NSPasteboardTypeString
    except ImportError:
        try:
            p = subprocess.run(
                ["pbcopy"], input=text, text=True, check=True
            )
            return p.returncode == 0
        except (OSError, subprocess.CalledProcessError):
            return False
    pb = NSPasteboard.generalPasteboard()
    pb.clearContents()
    return bool(pb.setString_forType_(text, NSPasteboardTypeString))


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
            rumps.MenuItem("Copy bookmark text", callback=self.copy_bookmarklet),
            rumps.MenuItem("Settings…", callback=self.show_settings),
            None,
            rumps.MenuItem("Show data folder", callback=self.show_data_folder),
            rumps.MenuItem("(Re)create TLS certificates…", callback=self.recreate_certs),
            rumps.MenuItem("Reinstall Python packages…", callback=self.reinstall_packages),
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
        _alert(title=APP_NAME, message=ABOUT_TEXT, ok="OK")

    def open_browser(self, _sender) -> None:
        webbrowser.open(_server_url())

    def copy_bookmarklet(self, _sender) -> None:
        try:
            text = _bookmarklet_text()
        except FileNotFoundError:
            self._log.warning("bookmarklet source missing from bundle")
            _alert(
                title=APP_NAME,
                message=(
                    "Couldn't find the bookmarklet source.\n\n"
                    "Rebuild the .app (./build_app.sh) — the file "
                    "bookmarklet/bookmarklet.js may not have been staged."
                ),
                ok="OK",
            )
            return
        if not _copy_to_clipboard(text):
            self._log.warning("clipboard write failed")
            _alert(
                title=APP_NAME,
                message="Couldn't write to the clipboard.",
                ok="OK",
            )
            return
        # Brief confirmation. rumps.notification needs a bundle id; in
        # the packaged .app briefcase sets one, so this just works. In
        # dev (``python -m app.desktop``) it can fail silently — that's
        # fine, the clipboard already has the text.
        try:
            rumps.notification(
                title=APP_NAME,
                subtitle="Bookmark text copied",
                message="Paste it into a new bookmark in your browser.",
            )
        except Exception:  # noqa: BLE001
            pass

    def show_settings(self, _sender) -> None:
        self._log.info("show_settings: callback fired")
        try:
            self._show_settings_impl()
        except BaseException:
            self._log.exception("show_settings: handler raised")

    def _show_settings_impl(self) -> None:
        from app import activity, installer, rag_build, scripts_seed
        self._log.info("show_settings: imports done")

        tls_on = TLS_CERT_PATH.exists() and TLS_KEY_PATH.exists()
        active_now = activity.snapshot()
        self._log.info("show_settings: activity snapshot done")

        # ---- install status -----
        install = installer.status_summary()
        self._log.info("show_settings: installer.status_summary done")
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
        self._log.info("show_settings: scripts_seed.status_summary done")
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
        self._log.info("show_settings: rag_build.status_summary done")
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

        self._log.info("show_settings: about to display alert (log_path=%s)", log_path)
        if log_path is not None and log_label is not None and log_path.exists():
            response = _alert(
                title=APP_NAME,
                message=body,
                ok="Close",
                cancel=log_label,
            )
            self._log.info("show_settings: alert dismissed response=%s", response)
            # rumps.alert returns 1 for OK (Close), 0 for the cancel
            # slot (which we've repurposed as "Open log"). When the
            # user picks the log button, surface the file in TextEdit.
            if response == 0:
                subprocess.run(["open", "-e", str(log_path)], check=False)
        else:
            response = _alert(title=APP_NAME, message=body, ok="Close")
            self._log.info("show_settings: alert dismissed response=%s", response)

    def show_data_folder(self, _sender) -> None:
        # Open ``python_storage/`` — the canonical artefact store where
        # snapshots, downloads, query results, and curves all live.
        #
        # We used to open PROJECT_ROOT, but that's only sensible in the
        # packaged .app (where PROJECT_ROOT == ~/Library/Application
        # Support/Voitta Bookmarklet/, exclusively writable data). In a
        # source checkout PROJECT_ROOT IS the repo root, so the user
        # saw a Finder window full of source files instead of the data
        # artefacts they were after. python_storage/ is a strict subdir
        # in both modes — clean signal, no source noise. The broader
        # data dir is one ↑ click away if needed.
        from app.services.python_storage import STORAGE_ROOT

        STORAGE_ROOT.mkdir(parents=True, exist_ok=True)
        subprocess.run(["open", str(STORAGE_ROOT)], check=False)

    def recreate_certs(self, _sender) -> None:
        """Regenerate the TLS cert pair via mkcert and prompt the user
        to restart so uvicorn picks them up."""
        self._log.info("recreate_certs: callback fired")
        from app import certs

        confirm = _alert(
            title="Recreate TLS certificates?",
            message=(
                "This regenerates the local TLS cert pair via mkcert and\n"
                "installs the local CA into your system trust store.\n\n"
                f"Cert location: {certs.CERTS_DIR}\n\n"
                "Requires the `mkcert` tool on PATH. Restart the app\n"
                "after this completes so uvicorn picks up the new cert."
            ),
            ok="Generate",
            cancel="Cancel",
        )
        if not confirm:
            self._log.info("recreate_certs: cancelled by user")
            return
        try:
            cert_path = certs.provision(force=True)
        except certs.CertError as exc:
            self._log.warning("recreate_certs failed: %s", exc)
            _alert(
                title="Cert generation failed",
                message=str(exc),
                ok="OK",
            )
            return
        self._log.info("recreate_certs: provisioned %s", cert_path)
        _alert(
            title="Certificates regenerated",
            message=(
                f"New cert pair at:\n  {cert_path}\n\n"
                "Restart the app for uvicorn to load the new cert."
            ),
            ok="OK",
        )

    def reinstall_packages(self, _sender) -> None:
        """Wipe ``userbase/`` + ``install_state.json`` and re-exec.

        Targets ONLY the pip-installed heavy packages. Preserves
        python_storage/, scripts/, settings.json, certs, logs, and the
        RAG index. The next launch finds an empty userbase, runs the
        full first-run installer, and shows the progress window.
        """
        self._log.info("reinstall_packages: callback fired")
        try:
            self._reinstall_packages_impl()
        except BaseException:
            self._log.exception("reinstall_packages: callback raised")

    def _reinstall_packages_impl(self) -> None:
        import os

        # ``userbase/`` holds the pip-installed heavy packages and
        # ``install_state.json``. Wiping it forces the first-launch
        # installer to rerun. Everything else under the data dir
        # (python_storage, scripts, settings, certs, RAG index) is left
        # alone — this is the "just the libs" partial reset.
        userbase = PROJECT_ROOT / "userbase"
        confirm = _alert(
            title="Reinstall Python packages?",
            message=(
                f"This deletes:\n  {userbase}\n\n"
                "Your scripts, snapshots, reports, and settings are\n"
                "NOT touched. The first-launch installer window will\n"
                "appear immediately to reinstall the heavy packages.\n\n"
                "Allow ~5 minutes on a fast connection."
            ),
            ok="Reinstall",
            cancel="Cancel",
        )
        if not confirm:
            self._log.info("reinstall_packages: cancelled by user")
            return

        self._log.info("reinstall_packages: wiping %s", userbase)
        if userbase.is_dir():
            shutil.rmtree(userbase, ignore_errors=True)

        for h in self._log.handlers:
            h.flush()
        logging.shutdown()
        os.execv(sys.executable, [sys.executable, "-m", "app.desktop_launcher"])

    def reset(self, _sender) -> None:
        """Wipe all user-writable data and restart with the install window.

        Deletes only the artefacts the app has written at runtime: settings,
        pip-installed deps, snapshots, scripts, TLS certs, and log files.
        The source tree / .venv are left untouched. The process then
        re-execs itself so the install window appears immediately — the
        user doesn't need to relaunch manually.
        """
        self._log.info("reset: callback fired")
        try:
            self._reset_impl()
        except BaseException:
            self._log.exception("reset: callback raised")

    def _reset_impl(self) -> None:
        import os

        # Explicit list of data dirs so we never accidentally nuke the
        # source tree or .venv in dev mode (where PROJECT_ROOT == repo root).
        _data_dirs = [
            PROJECT_ROOT / "rag",
            PROJECT_ROOT / "scripts",
            PROJECT_ROOT / "python_storage",
            PROJECT_ROOT / "userbase",
            PROJECT_ROOT / "backend" / "certs",
            PROJECT_ROOT / "backend" / "settings.json",
        ]
        _log_files = [
            PROJECT_ROOT / "voitta.log",
            PROJECT_ROOT / "voitta-launch.log",
        ]
        items_listed = "\n  ".join(
            str(p) for p in _data_dirs + _log_files if p.exists()
        ) or "(nothing to delete)"
        confirm = _alert(
            title=f"Reset {APP_NAME}?",
            message=(
                f"This deletes:\n  {items_listed}\n\n"
                "All settings, snapshots, scripts, and installed Python\n"
                "packages will be removed. Setup will run again immediately.\n\n"
                "This cannot be undone."
            ),
            ok="Reset and reinstall",
            cancel="Cancel",
        )
        if not confirm:
            self._log.info("reset: cancelled by user")
            return

        self._log.info("reset: user confirmed — wiping data dirs")
        for p in _data_dirs:
            if p.is_dir():
                self._log.info("reset: rmtree %s", p)
                shutil.rmtree(p, ignore_errors=True)
                self._log.info("reset: rmtree %s done", p)
            elif p.is_file():
                self._log.info("reset: unlink %s", p)
                p.unlink(missing_ok=True)
        for p in _log_files:
            # unlink log files last so the lines above are visible
            p.unlink(missing_ok=True)

        self._log.info(
            "reset: wipe complete — re-execing via desktop_launcher; "
            "executable=%s argv=%s",
            sys.executable,
            [sys.executable, "-m", "app.desktop_launcher"],
        )
        # Flush handlers so the log lines above survive the exec boundary
        for h in self._log.handlers:
            h.flush()
        logging.shutdown()

        os.execv(sys.executable, [
            sys.executable, "-m", "app.desktop_launcher",
        ])

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

        # Phase 3: TLS certs. After reset, backend/certs/ is gone;
        # without certs uvicorn falls through to HTTP, which the
        # bookmarklet can't load on HTTPS host pages (mixed-content
        # block). Try mkcert if it's available; non-fatal otherwise.
        try:
            from app import certs as _certs
            generated = _certs.provision_if_missing()
            if generated is not None:
                log.info("certs: provisioned %s during install flow", generated)
        except Exception:
            log.exception("certs: provision_if_missing failed during install flow")

        window.close()
        # Reaching here means heavy-package install succeeded
        # (``not ok`` returned early). RAG-build failures are
        # non-fatal — surfaced as a log line in the install window
        # and reflected in rag_query's "not built" envelope.
        AppHelper.callAfter(_start_uvicorn, log)

    threading.Thread(target=_worker, name="voitta-installer", daemon=True).start()


def _install_async_logging() -> None:
    """Set up non-blocking logging.

    Critical for dev mode where the process is attached to a terminal:
    if stderr writes ever block (terminal buffer full, slow tty drain),
    Python's default StreamHandler will block the CALLING thread on
    every log.info(). When the rumps main thread is the caller, this
    freezes the menu bar — clicks stop dispatching, the menu turns grey.

    Fix: route ALL log records through a QueueHandler that just appends
    to an in-memory queue and returns instantly. A background daemon
    thread (QueueListener) drains the queue and writes to stderr +
    rotating file. If a downstream handler ever blocks, only the worker
    thread blocks — the producer (main thread) keeps moving.
    """
    import queue
    from logging.handlers import QueueHandler, QueueListener, RotatingFileHandler

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    # Strip any handlers basicConfig or libraries may have already added
    # so we control the full chain.
    for h in list(root.handlers):
        root.removeHandler(h)

    fmt = logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s")

    # Sink 1: stderr (terminal output for dev mode).
    stderr_handler = logging.StreamHandler()
    stderr_handler.setFormatter(fmt)
    stderr_handler.setLevel(logging.INFO)

    sinks: list[logging.Handler] = [stderr_handler]

    # Sink 2: rotating file at PROJECT_ROOT/voitta.log.
    try:
        log_path = PROJECT_ROOT / "voitta.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(log_path, maxBytes=2_000_000, backupCount=3)
        fh.setFormatter(fmt)
        fh.setLevel(logging.INFO)
        sinks.append(fh)
    except OSError:
        pass

    # The queue is unbounded — better to grow than to drop diagnostics.
    log_queue: queue.Queue = queue.Queue(-1)
    qh = QueueHandler(log_queue)
    root.addHandler(qh)

    listener = QueueListener(log_queue, *sinks, respect_handler_level=True)
    # QueueListener.start() spawns a daemon thread already; no need to
    # touch the daemon flag after the fact.
    listener.start()


def main() -> int:
    _install_async_logging()
    log = logging.getLogger("voitta.desktop")
    log.info("desktop main() starting pid=%d", os.getpid())

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
        # Provision certs if missing — covers the case where install
        # is already done but certs were wiped (e.g. by Reset).
        try:
            from app import certs as _certs
            generated = _certs.provision_if_missing()
            if generated is not None:
                log.info("certs: provisioned %s at startup", generated)
        except Exception:
            log.exception("certs: provision_if_missing failed at startup")
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
