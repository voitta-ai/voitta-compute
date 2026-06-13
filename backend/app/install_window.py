"""First-launch installation progress window (PyObjC NSPanel).

Shown by ``app.desktop.main`` before uvicorn starts when any setup
phase is incomplete (certs / packages / source libraries / RAG indexes).

Four phase rows are displayed vertically:
  Phase 0 — Certificates      (mkcert -install + cert generation)
  Phase 1 — Python packages   (lazy pip install of heavy packages)
  Phase 2 — Source libraries  (shallow-clone submodules for RAG corpus)
  Phase 3 — RAG indexes       (chromadb + BM25 build from lib-sources)

UI updates from the worker thread MUST go through
``PyObjCTools.AppHelper.callAfter`` — Cocoa is single-threaded and
silently corrupts or crashes on cross-thread mutation. Every public
method here wraps the real work in ``callAfter``.
"""

from __future__ import annotations

from AppKit import (
    NSAlert,
    NSBackingStoreBuffered,
    NSColor,
    NSFont,
    NSProgressIndicator,
    NSProgressIndicatorStyleBar,
    NSScrollView,
    NSTextField,
    NSTextView,
    NSWindow,
    NSWindowStyleMaskTitled,
)
from Foundation import NSAttributedString
from PyObjCTools import AppHelper


_PHASE_NAMES = ["Certificates", "Python packages", "Source libraries", "RAG indexes"]
_WINDOW_W = 580
_WINDOW_H = 562

# Each phase row occupies 72px: name(20) + bar(16) + status(18) + padding.
# Rows are laid out top-down from below the header (Cocoa: bottom = 0).
_ROW_H = 72
_FIRST_ROW_Y = _WINDOW_H - 170  # == the historical first-run layout


class _PhaseRow:
    """One row: name label + progress bar + status label."""

    def __init__(self, cv, y: int, name: str) -> None:
        w = _WINDOW_W - 40

        # Name label — bold, carries the ◦/▶/✓/✗ prefix
        self._name = NSTextField.labelWithString_(f"◦  {name}")
        self._name.setFrame_(((20, y + 48), (w, 20)))
        self._name.setFont_(NSFont.boldSystemFontOfSize_(13))
        cv.addSubview_(self._name)

        # Progress bar
        self._bar = NSProgressIndicator.alloc().initWithFrame_(
            ((20, y + 26), (w, 16))
        )
        self._bar.setStyle_(NSProgressIndicatorStyleBar)
        self._bar.setIndeterminate_(False)
        self._bar.setMinValue_(0.0)
        self._bar.setMaxValue_(1.0)
        self._bar.setDoubleValue_(0.0)
        self._bar.setUsesThreadedAnimation_(True)
        cv.addSubview_(self._bar)

        # Status / detail text
        self._status = NSTextField.labelWithString_("Waiting…")
        self._status.setFrame_(((20, y + 4), (w, 18)))
        self._status.setFont_(NSFont.systemFontOfSize_(11))
        self._status.setTextColor_(NSColor.secondaryLabelColor())
        cv.addSubview_(self._status)

        self._raw_name = name

    # ---- state transitions -----------------------------------------------

    def activate(self, label: str = "Running…") -> None:
        self._name.setStringValue_(f"▶  {self._raw_name}")
        self._name.setTextColor_(NSColor.labelColor())
        self._status.setStringValue_(label)
        self._bar.setIndeterminate_(True)
        self._bar.startAnimation_(None)

    def update_progress(self, current: int, total: int, label: str) -> None:
        self._bar.stopAnimation_(None)
        self._bar.setIndeterminate_(False)
        self._bar.setMaxValue_(float(max(total, 1)))
        self._bar.setDoubleValue_(float(current))
        self._status.setStringValue_(label)

    def done(self, note: str = "Done") -> None:
        self._bar.stopAnimation_(None)
        self._bar.setIndeterminate_(False)
        self._bar.setMaxValue_(1.0)
        self._bar.setDoubleValue_(1.0)
        self._name.setStringValue_(f"✓  {self._raw_name}")
        self._name.setTextColor_(NSColor.systemGreenColor())
        self._status.setStringValue_(note)
        self._status.setTextColor_(NSColor.secondaryLabelColor())

    def skip(self, note: str = "Already up to date") -> None:
        self._bar.stopAnimation_(None)
        self._bar.setIndeterminate_(False)
        self._bar.setMaxValue_(1.0)
        self._bar.setDoubleValue_(1.0)
        self._name.setStringValue_(f"↷  {self._raw_name}")
        self._name.setTextColor_(NSColor.secondaryLabelColor())
        self._status.setStringValue_(note)

    def fail(self, reason: str) -> None:
        self._bar.stopAnimation_(None)
        self._name.setStringValue_(f"✗  {self._raw_name}")
        self._name.setTextColor_(NSColor.systemRedColor())
        self._status.setStringValue_(reason[:90])
        self._status.setTextColor_(NSColor.systemRedColor())


class InstallWindow:
    """Modal-style installer window with phase rows and a shared log.

    Window has only the title bar — no close/minimize/zoom buttons —
    so the user cannot dismiss it mid-setup. Defaults render the
    first-run setup; pass ``phases``/``title``/… for other flows
    (e.g. the voice-assistant lazy install).
    """

    def __init__(
        self,
        phases: list[str] | None = None,
        window_title: str = "Voitta — First-Run Setup",
        heading: str = "Setting up Voitta for the first time",
        subtitle_text: str = "This window will close automatically when setup completes.",
        footer_text: str = (
            "One-time setup, ~7 min on a fast connection.  "
            "Do not close this window."
        ),
    ) -> None:
        phase_names = phases if phases is not None else _PHASE_NAMES
        phase_y = [_FIRST_ROW_Y - _ROW_H * i for i in range(len(phase_names))]

        rect = ((0, 0), (_WINDOW_W, _WINDOW_H))
        self._w = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, NSWindowStyleMaskTitled, NSBackingStoreBuffered, False
        )
        self._w.setTitle_(window_title)
        self._w.setReleasedWhenClosed_(False)
        self._w.center()

        cv = self._w.contentView()

        # ---- title ----------------------------------------------------------
        title = NSTextField.labelWithString_(heading)
        title.setFrame_(((20, _WINDOW_H - 44), (_WINDOW_W - 40, 26)))
        title.setFont_(NSFont.boldSystemFontOfSize_(15))
        cv.addSubview_(title)

        subtitle = NSTextField.labelWithString_(subtitle_text)
        subtitle.setFrame_(((20, _WINDOW_H - 66), (_WINDOW_W - 40, 18)))
        subtitle.setFont_(NSFont.systemFontOfSize_(11))
        subtitle.setTextColor_(NSColor.secondaryLabelColor())
        cv.addSubview_(subtitle)

        # ---- separator line -------------------------------------------------
        sep = NSTextField.labelWithString_("")
        sep.setFrame_(((20, _WINDOW_H - 72), (_WINDOW_W - 40, 1)))
        sep.setBackgroundColor_(NSColor.separatorColor())
        sep.setDrawsBackground_(True)
        cv.addSubview_(sep)

        # ---- phase rows -----------------------------------------------------
        self._rows = [_PhaseRow(cv, y, name) for y, name in zip(phase_y, phase_names)]

        # ---- log text view --------------------------------------------------
        log_y = 50
        log_h = phase_y[-1] - log_y - 10
        scroll = NSScrollView.alloc().initWithFrame_(((20, log_y), (_WINDOW_W - 40, log_h)))
        scroll.setHasVerticalScroller_(True)
        scroll.setBorderType_(2)  # NSBezelBorder
        log = NSTextView.alloc().initWithFrame_(((0, 0), (_WINDOW_W - 40, log_h)))
        log.setEditable_(False)
        log.setFont_(NSFont.userFixedPitchFontOfSize_(10))
        log.setBackgroundColor_(NSColor.textBackgroundColor())
        log.setTextColor_(NSColor.labelColor())
        from AppKit import NSForegroundColorAttributeName
        self._log_attrs = {NSForegroundColorAttributeName: NSColor.labelColor()}
        log.setTypingAttributes_(self._log_attrs)
        scroll.setDocumentView_(log)
        cv.addSubview_(scroll)
        self._log = log

        # ---- footer ---------------------------------------------------------
        footer = NSTextField.labelWithString_(footer_text)
        footer.setFrame_(((20, 20), (_WINDOW_W - 40, 22)))
        footer.setFont_(NSFont.systemFontOfSize_(10))
        footer.setTextColor_(NSColor.tertiaryLabelColor())
        cv.addSubview_(footer)

    # ---- public API (all thread-safe via callAfter) ------------------------

    def show(self) -> None:
        AppHelper.callAfter(self._show_impl)

    def _show_impl(self) -> None:
        self._w.makeKeyAndOrderFront_(None)
        self._w.center()
        from AppKit import NSApp
        NSApp.activateIgnoringOtherApps_(True)

    def start_phase(self, phase: int, label: str = "Running…") -> None:
        AppHelper.callAfter(self._rows[phase].activate, label)

    def update_phase(self, phase: int, current: int, total: int, label: str) -> None:
        AppHelper.callAfter(self._rows[phase].update_progress, current, total, label)

    def finish_phase(self, phase: int, note: str = "Done") -> None:
        AppHelper.callAfter(self._rows[phase].done, note)

    def skip_phase(self, phase: int, note: str = "Already up to date") -> None:
        AppHelper.callAfter(self._rows[phase].skip, note)

    def fail_phase(self, phase: int, reason: str) -> None:
        AppHelper.callAfter(self._rows[phase].fail, reason)

    def log(self, line: str) -> None:
        AppHelper.callAfter(self._log_impl, line)

    def _log_impl(self, line: str) -> None:
        ts = self._log.textStorage()
        ts.appendAttributedString_(
            NSAttributedString.alloc().initWithString_attributes_(
                line + "\n", self._log_attrs,
            )
        )
        length = ts.length()
        self._log.scrollRangeToVisible_((length, 0))

    def add_continue_button(self, on_click) -> None:
        """Add a Continue button to the footer area (used after a
        non-fatal failure so the user can read the log, then proceed)."""
        AppHelper.callAfter(self._add_continue_impl, on_click)

    def _add_continue_impl(self, on_click) -> None:
        from AppKit import NSButton, NSBezelStyleRounded
        import objc

        class _Target(__import__("Foundation").NSObject):
            def initWithCallback_(self, cb):
                self = objc.super(_Target, self).init()
                if self is None:
                    return None
                self._cb = cb
                return self

            def clicked_(self, _sender):
                try:
                    self._cb()
                except Exception:
                    pass

        self._continue_target = _Target.alloc().initWithCallback_(on_click)
        btn = NSButton.alloc().initWithFrame_(((_WINDOW_W - 130, 14), (110, 30)))
        btn.setTitle_("Continue")
        btn.setBezelStyle_(NSBezelStyleRounded)
        btn.setTarget_(self._continue_target)
        btn.setAction_("clicked:")
        self._w.contentView().addSubview_(btn)

    def close(self) -> None:
        AppHelper.callAfter(self._w.orderOut_, None)


def show_error_alert(message: str) -> None:
    """Modal failure alert with a single Quit button.

    Called when the installer cannot recover. Blocks until dismissed.
    """
    alert = NSAlert.alloc().init()
    alert.setMessageText_("Voitta — setup failed")
    alert.setInformativeText_(message)
    alert.addButtonWithTitle_("Quit")
    alert.runModal()
