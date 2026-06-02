"""macOS menu-bar shell for the Voitta-chainlit backend.

Wraps the FastAPI / Chainlit app in a ``rumps.App`` so the user can
launch Voitta from a tray icon instead of running ``./start.sh``. The
backend itself is unchanged: uvicorn runs on a daemon thread, the
rumps event loop owns the main thread (Cocoa requires it).

Menu items:
  About Voitta
  Open in browser
  Copy bookmarklet
  Settings…              (status dialog + MCP-debug toggle)
  Show data folder
  (Re)create TLS certificates…
  Reset…
  Quit

macOS-only — rumps + AppKit. On other platforms just run
``./start.sh`` directly. The Settings dialog summarises BE state and
flips the ``mcpDebugEnabled`` switch that gates the ``/mcp``
debugging endpoint.
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

# Sequencing note (carried over from the legacy build): this import
# must come before importing rumps. rumps eagerly initialises NSApp on
# import; if env vars that affect ``app.config`` are set later, the
# path constants are already frozen at the wrong location.
from app.config import (  # noqa: E402
    HOST,
    PORT,
    PLAINTEXT_PORT,
    PROJECT_ROOT,
    TLS_CERT_PATH,
    TLS_KEY_PATH,
    USER_DATA_ROOT,
)

import time

import rumps  # noqa: E402

# ---------------------------------------------------------------------------
# Uvicorn state (module-level so menu and setup share it)
# ---------------------------------------------------------------------------

_uvicorn_server: "uvicorn.Server | None" = None  # set on first start
_uvicorn_thread: threading.Thread | None = None
_uvicorn_plaintext_server: "uvicorn.Server | None" = None  # bridge listener
_uvicorn_plaintext_thread: threading.Thread | None = None
_uvicorn_lock = threading.Lock()
_installing: bool = False  # True while the first-time setup is running


APP_NAME = "Voitta"

def _get_app_version() -> str:
    try:
        from app.installer import current_app_version
        return current_app_version()
    except Exception:
        return "unknown"

ABOUT_TEXT = (
    f"Version {_get_app_version()}\n\n"
    "Local Voitta backend.\n"
    "Click the bookmarklet on any page to open the chat."
)


# ---------------------------------------------------------------------------
# AppKit helpers — modal dialogs from a status-bar (accessory) app
# ---------------------------------------------------------------------------


def _alert(*args, **kwargs):
    """Wrapper around ``rumps.alert`` that makes the modal visible from
    a status-bar app. See the legacy build for the full story — short
    version: NSApp must be temporarily set to Regular activation policy
    so the alert can steal focus, then restored to Accessory."""
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


def _settings_alert_with_switch(
    title: str,
    message: str,
    ok: str,
    cancel: str | None,
    switch_label: str,
    switch_on: bool,
) -> tuple[int, bool]:
    """NSAlert with an NSSwitch accessory view. Returns
    ``(rumps-style response, final switch state)``.

    Response: ``1`` = ok pressed, ``0`` = cancel pressed (only when
    ``cancel`` is non-None).
    """
    from AppKit import (
        NSAlert,
        NSApplication,
        NSApplicationActivationPolicyAccessory,
        NSApplicationActivationPolicyRegular,
        NSControlStateValueOff,
        NSControlStateValueOn,
        NSMakeRect,
        NSModalPanelWindowLevel,
        NSSwitch,
        NSTextField,
        NSView,
    )

    alert = NSAlert.alloc().init()
    alert.setMessageText_(title)
    alert.setInformativeText_(message)
    alert.addButtonWithTitle_(ok)
    if cancel:
        alert.addButtonWithTitle_(cancel)

    container = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 320, 28))
    label = NSTextField.alloc().initWithFrame_(NSMakeRect(0, 4, 240, 20))
    label.setStringValue_(switch_label)
    label.setBezeled_(False)
    label.setDrawsBackground_(False)
    label.setEditable_(False)
    label.setSelectable_(False)
    container.addSubview_(label)

    switch = NSSwitch.alloc().initWithFrame_(NSMakeRect(260, 0, 50, 28))
    switch.setState_(NSControlStateValueOn if switch_on else NSControlStateValueOff)
    container.addSubview_(switch)
    alert.setAccessoryView_(container)

    nsapp = NSApplication.sharedApplication()
    try:
        nsapp.setActivationPolicy_(NSApplicationActivationPolicyRegular)
        nsapp.activateIgnoringOtherApps_(True)
    except Exception:
        pass
    try:
        win = alert.window()
        win.setLevel_(NSModalPanelWindowLevel)
        win.makeKeyAndOrderFront_(None)
    except Exception:
        pass
    try:
        raw = alert.runModal()
    finally:
        try:
            nsapp.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
        except Exception:
            pass

    # NSAlert returns 1000 + button-index. Map to rumps codes.
    response = 1 if raw == 1000 else (0 if raw == 1001 else int(raw))
    new_state = bool(switch.state() == NSControlStateValueOn)
    return response, new_state


# ---------------------------------------------------------------------------
# URL / bookmarklet / clipboard helpers
# ---------------------------------------------------------------------------


def _server_url() -> str:
    scheme = "https" if TLS_CERT_PATH.exists() and TLS_KEY_PATH.exists() else "http"
    return f"{scheme}://{HOST}:{PORT}"


def _bookmarklet_text() -> str:
    """Return the ``javascript:`` URL the user pastes into a browser bookmark.
    Origin is the tray's local server URL — unchanged from before; the string
    builder now lives in app.bookmarklets so the server can reuse it."""
    from app.bookmarklets import normal_bookmarklet
    return normal_bookmarklet(_server_url())


def _bridge_bookmarklet_text() -> str:
    """Bookmarklet for hardened-CSP pages (e.g. Salesforce Lightning). Origin is
    the tray's plain-http loopback sibling — unchanged from before."""
    from app.bookmarklets import bridge_bookmarklet
    return bridge_bookmarklet(f"http://{HOST}:{PLAINTEXT_PORT}")


def _copy_to_clipboard(text: str) -> bool:
    try:
        from AppKit import NSPasteboard, NSPasteboardTypeString
    except ImportError:
        try:
            p = subprocess.run(["pbcopy"], input=text, text=True, check=True)
            return p.returncode == 0
        except (OSError, subprocess.CalledProcessError):
            return False
    pb = NSPasteboard.generalPasteboard()
    pb.clearContents()
    return bool(pb.setString_forType_(text, NSPasteboardTypeString))


def _human_path(p: Path) -> str:
    s = str(p)
    home = str(Path.home())
    return s.replace(home, "~", 1) if s.startswith(home) else s


# ---------------------------------------------------------------------------
# Uvicorn launcher
# ---------------------------------------------------------------------------


def _start_uvicorn(log: logging.Logger) -> threading.Thread:
    """Launch uvicorn on a daemon thread. Stores server ref for restart/status."""
    global _uvicorn_server, _uvicorn_thread, _installing
    _installing = False

    import uvicorn

    kwargs: dict = {"host": HOST, "port": PORT, "log_level": "info"}
    if TLS_CERT_PATH.exists() and TLS_KEY_PATH.exists():
        kwargs["ssl_certfile"] = str(TLS_CERT_PATH)
        kwargs["ssl_keyfile"] = str(TLS_KEY_PATH)

    config = uvicorn.Config("app.main:app", **kwargs)
    server = uvicorn.Server(config)

    with _uvicorn_lock:
        _uvicorn_server = server

    def _run() -> None:
        log.info("starting uvicorn on %s", _server_url())
        try:
            server.run()
        except Exception:
            log.exception("uvicorn died")

    t = threading.Thread(target=_run, name="voitta-uvicorn", daemon=True)
    t.start()
    with _uvicorn_lock:
        _uvicorn_thread = t

    # Sibling plain-HTTP listener for the hardened-site bridge popup. Same
    # ASGI app, no TLS, on PLAINTEXT_PORT. Runs in the same process so it
    # shares all in-memory state (sessions, sockets) with the TLS listener.
    _start_uvicorn_plaintext(log)

    return t


def _start_uvicorn_plaintext(log: logging.Logger) -> threading.Thread:
    """Launch a second uvicorn (plain http, no TLS) on PLAINTEXT_PORT.

    The bridge popup loads from http://127.0.0.1:PLAINTEXT_PORT so it needs
    no trusted cert. Daemon thread; failures are logged but non-fatal — the
    ordinary https bookmarklet keeps working regardless.
    """
    global _uvicorn_plaintext_server, _uvicorn_plaintext_thread
    import uvicorn

    config = uvicorn.Config(
        "app.main:app", host=HOST, port=PLAINTEXT_PORT, log_level="warning"
    )
    server = uvicorn.Server(config)
    with _uvicorn_lock:
        _uvicorn_plaintext_server = server

    def _run() -> None:
        log.info("starting bridge listener on http://%s:%s", HOST, PLAINTEXT_PORT)
        try:
            server.run()
        except Exception:
            log.exception("bridge listener died")

    t = threading.Thread(target=_run, name="voitta-uvicorn-bridge", daemon=True)
    t.start()
    with _uvicorn_lock:
        _uvicorn_plaintext_thread = t
    return t


def _uvicorn_status() -> str:
    """Return a short status string for the menu label."""
    if _installing:
        return "starting…"
    with _uvicorn_lock:
        t = _uvicorn_thread
        srv = _uvicorn_server
    if t is None:
        return "stopped"
    if not t.is_alive():
        return "crashed"
    if srv is not None and getattr(srv, "started", False):
        return "running"
    return "starting…"


# ---------------------------------------------------------------------------
# Status / diagnostics helpers
# ---------------------------------------------------------------------------


def _rag_status_line() -> str:
    """One-line RAG state for the Settings dialog."""
    try:
        from app.tools.rag.index import index_status
        st = index_status("docs")
    except Exception as exc:
        return f"RAG      ·  ? ({exc})"
    if st.get("built"):
        return (
            f"RAG      ·  ✓ {st.get('chunk_count', 0)} chunks across "
            f"{st.get('files_count', 0)} docs"
        )
    return "RAG      ·  ⚠ not built — run scripts/build_rag.py"


def _plugins_status_line() -> str:
    try:
        from app.plugins import all_plugins
        plugins = all_plugins()
    except Exception:
        return "Plugins  ·  ?"
    return f"Plugins  ·  {len(plugins)} loaded: {', '.join(p.name for p in plugins)}"


def _mcp_connectors_line() -> str:
    try:
        from app.services.mcp.registry import list_connectors
        conns = list_connectors()
    except Exception:
        return "MCP      ·  ?"
    if not conns:
        return "MCP      ·  no connectors declared"
    parts = [f"{c.decl.plugin_name}:{c.decl.id}={c.status}" for c in conns]
    return "MCP      ·  " + ", ".join(parts)


# ---------------------------------------------------------------------------
# Menu-bar app
# ---------------------------------------------------------------------------


class VoittaMenuBarApp(rumps.App):
    def __init__(self) -> None:
        # title="V" is the idle-state glyph. The activity poller swaps
        # its colour while tools run — see app.activity for the
        # category → colour mapping.
        super().__init__(APP_NAME, title="V", quit_button=None)
        self._server_status_item = rumps.MenuItem("Server: checking…")
        self._server_status_item.set_callback(None)  # display-only
        self.menu = [
            rumps.MenuItem(f"About {APP_NAME}", callback=self.show_about),
            None,  # separator
            rumps.MenuItem("Open in browser", callback=self.open_browser),
            rumps.MenuItem("Copy bookmarklet", callback=self.copy_bookmarklet),
            rumps.MenuItem("Copy bookmarklet (Salesforce/strict CSP)", callback=self.copy_bridge_bookmarklet),
            rumps.MenuItem("Settings…", callback=self.show_settings),
            None,
            self._server_status_item,
            rumps.MenuItem("Restart server", callback=self.restart_server),
            None,
            rumps.MenuItem("Workspace…", callback=self.open_workspace),
            rumps.MenuItem("(Re)create TLS certificates…", callback=self.recreate_certs),
            rumps.MenuItem("Rebuild…", callback=self.rebuild),
            rumps.MenuItem("Reset…", callback=self.reset),
            None,
            rumps.MenuItem(f"Quit {APP_NAME}", callback=self.quit_app),
        ]
        self._log = logging.getLogger("voitta.desktop")

        # Activity poller — 400 ms feels live without burning cycles.
        # Always-on while the app runs; cheap (one dict read).
        self._activity_timer = rumps.Timer(self._tick_activity, 0.4)
        self._activity_timer.start()
        self._last_color: str | None = None
        self._spin_frame: int = 0
        self._last_server_status: str | None = None

    # ---- activity poller -------------------------------------------------

    _SPIN_FRAMES = ["⣾", "⣽", "⣻", "⢿", "⡿", "⣟", "⣯", "⣷"]

    def _tick_activity(self, _timer) -> None:
        from app import activity
        status = _uvicorn_status()

        if status == "starting…":
            # Animate a rotating braille ring around V while server boots.
            frame = self._SPIN_FRAMES[self._spin_frame % len(self._SPIN_FRAMES)]
            self._spin_frame += 1
            self._apply_colored_title(f"{frame}V", "idle")
            self._last_color = "idle"
        else:
            self._spin_frame = 0
            color_name = activity.current_color()
            if color_name != self._last_color or status != self._last_server_status:
                self._last_color = color_name
                self._apply_colored_title(activity.current_glyph(), color_name)

        self._last_server_status = status

        icons = {"running": "✓", "starting…": "⋯", "stopped": "✕", "crashed": "⚠"}
        label = f"Server: {icons.get(status, '')} {status}"
        if self._server_status_item.title != label:
            self._server_status_item.title = label

    @staticmethod
    def _ns_color_for(name: str):
        from AppKit import NSColor
        if name == "label":
            return NSColor.labelColor()
        method = f"system{name.capitalize()}Color"
        if hasattr(NSColor, method):
            return getattr(NSColor, method)()
        return NSColor.labelColor()

    def _apply_colored_title(self, text: str, color_name: str) -> None:
        try:
            from AppKit import (
                NSAttributedString,
                NSFont,
                NSFontAttributeName,
                NSForegroundColorAttributeName,
            )
        except ImportError:
            self.title = text
            return
        attrs = {
            NSForegroundColorAttributeName: self._ns_color_for(color_name),
            NSFontAttributeName: NSFont.boldSystemFontOfSize_(14),
        }
        attributed = NSAttributedString.alloc().initWithString_attributes_(text, attrs)
        try:
            button = self._nsapp.nsstatusitem.button()
        except Exception:
            self.title = text
            return
        if button is None:
            self.title = text
            return
        button.setAttributedTitle_(attributed)

    # ---- menu callbacks --------------------------------------------------

    def restart_server(self, _sender) -> None:
        log = self._log
        def _do_restart() -> None:
            with _uvicorn_lock:
                srv = _uvicorn_server
                bridge_srv = _uvicorn_plaintext_server
            if srv is not None:
                log.info("restart_server: signalling uvicorn to exit")
                srv.should_exit = True
                if bridge_srv is not None:
                    bridge_srv.should_exit = True
                # Give it a moment to free the ports before restarting.
                time.sleep(1.5)
            log.info("restart_server: starting fresh uvicorn")
            _start_uvicorn(log)
        threading.Thread(target=_do_restart, name="voitta-restart", daemon=True).start()

    def show_about(self, _sender) -> None:
        _alert(title=APP_NAME, message=ABOUT_TEXT, ok="OK")

    def open_browser(self, _sender) -> None:
        webbrowser.open(_server_url())

    def copy_bookmarklet(self, _sender) -> None:
        text = _bookmarklet_text()
        if _copy_to_clipboard(text):
            try:
                rumps.notification(
                    title=APP_NAME,
                    subtitle="Bookmarklet copied",
                    message="Paste into your bookmarks bar.",
                )
            except Exception:
                pass
        else:
            _alert(title=APP_NAME, message="Couldn't access the clipboard.", ok="OK")

    def copy_bridge_bookmarklet(self, _sender) -> None:
        text = _bridge_bookmarklet_text()
        if _copy_to_clipboard(text):
            try:
                rumps.notification(
                    title=APP_NAME,
                    subtitle="Salesforce bookmarklet copied",
                    message="For pages with a strict CSP. Opens a small popup; keep it open.",
                )
            except Exception:
                pass
        else:
            _alert(title=APP_NAME, message="Couldn't access the clipboard.", ok="OK")

    def show_settings(self, _sender) -> None:
        """Status dialog with the MCP-debug toggle.

        The bookmarklet's in-pane Settings panel is the place to edit
        API keys / providers / Google OAuth / per-plugin config. This
        tray dialog is for backend-level state and the privileged
        ``/mcp`` switch (which we don't want in the in-page UI because
        the bookmarklet runs on third-party origins).
        """
        try:
            self._show_settings_impl()
        except Exception:
            self._log.exception("show_settings raised")

    def _show_settings_impl(self) -> None:
        from app import activity
        from app.services import user_settings as _us

        tls_on = TLS_CERT_PATH.exists() and TLS_KEY_PATH.exists()
        active_now = activity.snapshot()

        rag_line = _rag_status_line()
        plugins_line = _plugins_status_line()
        mcp_line = _mcp_connectors_line()

        if active_now:
            preview = ", ".join(
                f"{a['detail']} ({a['category']})" for a in active_now[:3]
            )
            if len(active_now) > 3:
                preview += f" +{len(active_now) - 3} more"
            activity_line = f"Activity ·  ▶ {preview}"
        else:
            activity_line = "Activity ·  idle"

        mcp_on = _us.mcp_debug_enabled()
        mcp_url = f"{_server_url()}/mcp/"
        mcp_dbg_line = (
            f"MCP-debug · URL: {mcp_url}\n"
            f"           Transport: streamable-http (loopback only)"
        )

        body = (
            f"Server  ·  {_server_url()}  ·  {'TLS' if tls_on else 'HTTP only'}\n\n"
            f"{plugins_line}\n"
            f"{rag_line}\n"
            f"{mcp_line}\n"
            f"{activity_line}\n"
            f"{mcp_dbg_line}\n\n"
            f"Data folder\n  {_human_path(PROJECT_ROOT.parent)}\n\n"
            "Chat settings — API keys, model, theme, plugin config —\n"
            "live in the bookmarklet sidebar. Click the gear icon there."
        )

        response, new_mcp = _settings_alert_with_switch(
            title=APP_NAME,
            message=body,
            ok="Close",
            cancel=None,
            switch_label="Enable MCP debugging endpoint",
            switch_on=mcp_on,
        )
        if new_mcp != mcp_on:
            _us.set_mcp_debug_enabled(new_mcp)
            self._log.info("MCP debug toggle: %s → %s", mcp_on, new_mcp)
        _ = response  # rumps-style response — single-button dialog, ignored

    def open_workspace(self, _sender) -> None:
        # Open the Voitta page with ?workspace=1 so the frontend auto-opens
        # the workspace panel on load.
        webbrowser.open(f"{_server_url()}?workspace=1")

    def recreate_certs(self, _sender) -> None:
        """Regenerate the TLS cert pair via mkcert.

        The chainlit build doesn't ship a Python wrapper around mkcert
        — we just shell out. If mkcert isn't installed, we surface
        the brew command the user needs.
        """
        if shutil.which("mkcert") is None:
            _alert(
                title="mkcert not installed",
                message=(
                    "Install with:\n\n  brew install mkcert\n  mkcert -install\n\n"
                    "Then re-run this menu item."
                ),
                ok="OK",
            )
            return
        confirm = _alert(
            title="Recreate TLS certificates?",
            message=(
                "Regenerates the local TLS cert pair via mkcert and "
                "installs the local CA into your trust store.\n\n"
                f"Cert location: {_human_path(TLS_CERT_PATH.parent)}\n\n"
                "Restart Voitta after this completes so uvicorn picks up "
                "the new cert."
            ),
            ok="Generate",
            cancel="Cancel",
        )
        if not confirm:
            return
        certs_dir = TLS_CERT_PATH.parent
        certs_dir.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.run(
                [
                    "mkcert",
                    "-cert-file", str(TLS_CERT_PATH),
                    "-key-file", str(TLS_KEY_PATH),
                    "127.0.0.1", "localhost",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            _alert(
                title="Cert generation failed",
                message=(exc.stderr or exc.stdout or str(exc))[:500],
                ok="OK",
            )
            return
        _alert(
            title="Certificates regenerated",
            message=(
                f"New cert pair at:\n  {_human_path(TLS_CERT_PATH)}\n\n"
                "Restart Voitta so uvicorn loads the new cert."
            ),
            ok="OK",
        )

    def rebuild(self, _sender) -> None:
        confirm = _alert(
            title="Rebuild Voitta?",
            message=(
                "Re-runs pip install and rebuilds all RAG indexes.\n\n"
                "The server will restart automatically when done.\n"
                "This may take a few minutes."
            ),
            ok="Rebuild",
            cancel="Cancel",
        )
        if not confirm:
            return
        with _uvicorn_lock:
            srv = _uvicorn_server
            bridge_srv = _uvicorn_plaintext_server
        if srv is not None:
            srv.should_exit = True
        if bridge_srv is not None:
            bridge_srv.should_exit = True

        from app.installer import force_rebuild_stamps
        force_rebuild_stamps()
        self._log.info("rebuild: stamps wiped, re-running setup")

        from PyObjCTools import AppHelper
        AppHelper.callAfter(_run_first_time_setup, self._log)

    def reset(self, _sender) -> None:
        """Wipe all runtime artefacts (rag indexes, settings, scripts state,
        python_storage) and restart. The source tree is never touched."""
        _data_dirs = [
            PROJECT_ROOT.parent / "rag",
            USER_DATA_ROOT / "scripts_state",
            USER_DATA_ROOT / "python_storage",
        ]
        from app.config import USER_SETTINGS_PATH
        _data_files = [USER_SETTINGS_PATH]
        items_listed = "\n  ".join(
            _human_path(p) for p in _data_dirs + _data_files if p.exists()
        ) or "(nothing to delete)"
        confirm = _alert(
            title=f"Reset {APP_NAME}?",
            message=(
                f"This deletes:\n  {items_listed}\n\n"
                "All settings, RAG indexes, snapshots, and per-script\n"
                "render state will be removed. This cannot be undone.\n\n"
                "Restart Voitta after this completes."
            ),
            ok="Reset",
            cancel="Cancel",
        )
        if not confirm:
            return
        for p in _data_dirs:
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
        for p in _data_files:
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass

    def quit_app(self, _sender) -> None:
        # rumps's default Quit bypasses our cleanup. Funnel through here
        # so we can shut uvicorn down explicitly in future revisions
        # (currently relies on daemon-thread reaping).
        rumps.quit_application()


def main() -> None:
    """Entry point: run the installer if needed, then start uvicorn + rumps."""
    log = logging.getLogger("voitta.desktop")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Chainlit reads CHAINLIT_APP_ROOT at module import time to set
    # FILES_DIRECTORY. Inside a frozen .app getcwd() returns "/" (read-only),
    # so set it early — before any chainlit import can happen.
    os.environ.setdefault("CHAINLIT_APP_ROOT", str(PROJECT_ROOT.parent))


    # The frozen .app bundles the frontend under voitta_compute/resources/.
    # Set VOITTA_FRONTEND_DIST so app.config.FRONTEND_DIST resolves correctly
    # before any import of app.config happens inside the uvicorn worker.
    _bundle_fe = Path(__file__).resolve().parents[2] / "voitta_compute" / "resources" / "frontend_dist"
    if _bundle_fe.is_dir():
        os.environ.setdefault("VOITTA_FRONTEND_DIST", str(_bundle_fe))

    # Wipe userbase/ + rag/ when the app version has changed.
    try:
        from app.installer import ensure_fresh_deploy
        ensure_fresh_deploy(log)
    except Exception:
        log.exception("ensure_fresh_deploy raised")

    from app.certs import is_present as certs_present
    from app.installer import is_complete as packages_complete, lib_sources_need_update
    from app.rag_build import is_built as rag_built

    needs_setup = (
        not certs_present()
        or not packages_complete()
        or lib_sources_need_update()
        or not rag_built()
    )

    # Start the menu-bar app first so the V icon appears immediately.
    # Both branches schedule their work via AppHelper.callAfter so they
    # fire after the Cocoa runloop starts.
    from PyObjCTools import AppHelper

    if needs_setup:
        global _installing
        _installing = True
        AppHelper.callAfter(_run_first_time_setup, log)
    else:
        AppHelper.callAfter(_start_uvicorn, log)

    VoittaMenuBarApp().run()


def _run_first_time_setup(log: logging.Logger) -> None:
    """Show the 3-phase installer window and run setup on a worker thread.

    The window is shown by scheduling via ``AppHelper.callAfter`` so it
    fires after the rumps/NSApp runloop starts. Uvicorn is launched only
    after all 3 phases complete successfully.
    """
    from PyObjCTools import AppHelper
    from app.install_window import InstallWindow, show_error_alert

    win = InstallWindow()

    def _worker() -> None:
        # Phase 0 — Certificates
        win.start_phase(0)
        try:
            from app.certs import provision_with_progress
            ok = provision_with_progress(lambda msg: win.log(msg))
        except Exception as exc:
            ok = False
            win.log(f"!!! cert error: {exc}")
        if ok:
            win.finish_phase(0)
        else:
            win.fail_phase(0, "Failed — will run over HTTP")

        # Phase 1 — Python packages
        win.start_phase(1, "Preparing…")
        from app.installer import install_all, last_failure_detail, is_complete

        def _pkg_progress(current, total, label, log_line):
            win.update_phase(1, current, total, label)
            if log_line:
                win.log(log_line)

        if is_complete():
            win.skip_phase(1, "Already installed")
        else:
            ok = install_all(_pkg_progress)
            if ok:
                win.finish_phase(1)
            else:
                import app.installer as _inst
                win.fail_phase(1, "Install failed — see log")
                AppHelper.callAfter(
                    show_error_alert,
                    f"Package install failed:\n\n{_inst.last_failure_detail or 'Unknown error'}",
                )
                AppHelper.callAfter(__import__("rumps").quit_application)
                return

        # Phase 2 — Source libraries (shallow-clone submodules)
        win.start_phase(2, "Cloning source libraries…")
        from app.installer import clone_lib_sources, lib_sources_need_update

        if not lib_sources_need_update():
            win.skip_phase(2, "Already up to date")
        else:
            src_ok = clone_lib_sources(lambda msg: win.log(msg))
            if src_ok:
                win.finish_phase(2)
            else:
                import app.installer as _inst2
                log.warning("lib-sources clone failed: %s", _inst2.last_failure_detail)
                win.fail_phase(2, "Clone failed — RAG code corpus unavailable")

        # Phase 3 — RAG indexes
        win.start_phase(3, "Indexing documentation and source code…")
        from app.rag_build import build_all, is_built

        rag_ok = True
        if is_built():
            win.skip_phase(3, "Indexes up to date")
        else:
            def _rag_progress(line: str) -> None:
                log.info("rag: %s", line)
                win.log(line)

            ok = build_all(_rag_progress)
            if ok:
                win.finish_phase(3)
            else:
                import app.rag_build as _rb
                detail = _rb.last_failure_detail or "unknown error"
                log.warning("RAG build failed: %s", detail)
                win.fail_phase(3, "RAG build failed — see log below")
                rag_ok = False

        # Mark deployment complete and start serving.
        try:
            from app.installer import mark_deploy_complete
            mark_deploy_complete()
        except Exception:
            pass

        if not rag_ok:
            # Keep the window visible so the user can read the error.
            # Add a Continue button via callAfter so they can dismiss and proceed.
            from PyObjCTools import AppHelper as _AH
            def _show_rag_error():
                win.log("─" * 60)
                win.log("RAG build failed. Search will not work this session.")
                win.log("The app will start anyway. You can retry via the menu.")
                win.add_continue_button(lambda: (win.close(), _start_uvicorn(log)))
            _AH.callAfter(_show_rag_error)
        else:
            # win.close() and _start_uvicorn must run on the main AppKit thread.
            from PyObjCTools import AppHelper as _AH
            _AH.callAfter(win.close)
            _AH.callAfter(_start_uvicorn, log)

    # Schedule: show window first, then kick the worker thread.
    def _kick() -> None:
        win.show()
        t = threading.Thread(target=_worker, name="voitta-setup", daemon=True)
        t.start()

    AppHelper.callAfter(_kick)


if __name__ == "__main__":
    main()
