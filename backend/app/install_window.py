"""First-launch installation progress window (PyObjC NSPanel).

Shown by ``app.desktop.main`` before uvicorn starts when the heavy
package set isn't yet present in the user's site-packages dir.

UI updates from the worker thread MUST go through
``PyObjCTools.AppHelper.callAfter`` — Cocoa is single-threaded and
will silently corrupt or crash on cross-thread mutation. Every public
method here already wraps the real work in ``callAfter`` so callers
don't need to.
"""

from __future__ import annotations

from AppKit import (
    NSAlert,
    NSAlertFirstButtonReturn,
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

# NSMakeRect / NSMakeRange are inline C functions in <Foundation/NSGeometry.h>
# and <Foundation/NSRange.h> respectively. PyObjC re-exports them, but the
# resolution path goes through dlsym(<Foundation>, "NSMakeRange") which the
# kernel logs as a warning ("dlsym cannot find symbol NSMakeRange in
# CFBundle") on every launch. PyObjC accepts plain Python tuples wherever
# these structs are expected, so use tuples directly to skip the import
# entirely — quieter logs, one less PyObjC magic-binding to depend on.


class InstallWindow:
    """Progress window for the first-launch package install.

    Layout (top → bottom): bold title, status line, progress bar, log
    text view, footer note. Window has only the title bar — no close
    button — so the user can't dismiss it mid-install.
    """

    def __init__(self, total: int) -> None:
        self._total = max(total, 1)
        # PyObjC bridges NSRect as ``((origin_x, origin_y), (w, h))``,
        # NOT as a flat 4-tuple. Same shape for every setFrame_ /
        # initWithFrame_ call below.
        rect = ((0, 0), (560, 360))
        # NSWindowStyleMaskTitled only — no close/minimize/zoom buttons.
        # User can't accidentally dismiss the install.
        self._w = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, NSWindowStyleMaskTitled, NSBackingStoreBuffered, False
        )
        self._w.setTitle_("Voitta Bookmarklet — First-Run Setup")
        self._w.setReleasedWhenClosed_(False)
        self._w.center()

        cv = self._w.contentView()

        # ----- title -----
        title = NSTextField.labelWithString_("Installing required packages")
        title.setFrame_(((20, 310), (520, 28)))
        title.setFont_(NSFont.boldSystemFontOfSize_(15))
        cv.addSubview_(title)

        # ----- subtitle / status -----
        self._status = NSTextField.labelWithString_("Preparing…")
        self._status.setFrame_(((20, 280), (520, 22)))
        self._status.setFont_(NSFont.systemFontOfSize_(12))
        cv.addSubview_(self._status)

        # ----- progress bar -----
        self._progress = NSProgressIndicator.alloc().initWithFrame_(
            ((20, 250), (520, 20))
        )
        self._progress.setMinValue_(0.0)
        self._progress.setMaxValue_(float(self._total))
        self._progress.setStyle_(NSProgressIndicatorStyleBar)
        self._progress.setIndeterminate_(False)
        self._progress.setUsesThreadedAnimation_(True)
        cv.addSubview_(self._progress)

        # ----- log view -----
        scroll = NSScrollView.alloc().initWithFrame_(((20, 60), (520, 180)))
        scroll.setHasVerticalScroller_(True)
        scroll.setBorderType_(2)  # bezel
        log = NSTextView.alloc().initWithFrame_(((0, 0), (520, 180)))
        log.setEditable_(False)
        log.setFont_(NSFont.userFixedPitchFontOfSize_(11))
        # Use the macOS dynamic system colors so the text view tracks
        # Light/Dark mode automatically. ``textBackgroundColor`` /
        # ``labelColor`` are the canonical adaptive pair (white+black
        # in Light, near-black+near-white in Dark). Without an explicit
        # ``setTextColor_`` the view defaults to flat black, which is
        # unreadable on the dark-mode textBackgroundColor.
        log.setBackgroundColor_(NSColor.textBackgroundColor())
        log.setTextColor_(NSColor.labelColor())
        # Bake the foreground color into the typing attributes too, so
        # any unstyled strings we append to ``textStorage`` (the path
        # used by ``set_progress``) pick it up. Without this, an
        # NSAttributedString constructed via ``initWithString_`` has
        # no color attribute and falls back to a static black, which
        # defeats the dynamic ``setTextColor_`` we just set.
        from AppKit import NSForegroundColorAttributeName
        self._log_attrs = {NSForegroundColorAttributeName: NSColor.labelColor()}
        log.setTypingAttributes_(self._log_attrs)
        scroll.setDocumentView_(log)
        cv.addSubview_(scroll)
        self._log = log

        # ----- footer note -----
        note = NSTextField.labelWithString_(
            "One-time setup. ~5 minutes on a fast connection. "
            "The window closes automatically when done."
        )
        note.setFrame_(((20, 25), (520, 30)))
        note.setFont_(NSFont.systemFontOfSize_(11))
        note.setTextColor_(NSColor.secondaryLabelColor())
        cv.addSubview_(note)

    def show(self) -> None:
        AppHelper.callAfter(self._show_impl)

    def _show_impl(self) -> None:
        self._w.makeKeyAndOrderFront_(None)
        self._w.center()
        # Bring app to foreground so the panel pops above the user's
        # active window. Without this it can land behind the browser.
        from AppKit import NSApp
        NSApp.activateIgnoringOtherApps_(True)

    def set_progress(
        self, current: int, label: str, log_line: str | None = None
    ) -> None:
        AppHelper.callAfter(self._set_progress_impl, current, label, log_line)

    def _set_progress_impl(
        self, current: int, label: str, log_line: str | None
    ) -> None:
        self._status.setStringValue_(f"({current}/{self._total}) {label}")
        self._progress.setDoubleValue_(float(current))
        if log_line:
            ts = self._log.textStorage()
            # ``initWithString_attributes_`` carries an explicit
            # foreground color, so the line is readable in both
            # Light and Dark mode. ``initWithString_`` alone gives
            # an unattributed string that NSTextView renders in flat
            # black regardless of the view's textColor / appearance.
            ts.appendAttributedString_(
                NSAttributedString.alloc().initWithString_attributes_(
                    log_line + "\n", self._log_attrs,
                )
            )
            length = ts.length()
            self._log.scrollRangeToVisible_((length, 0))

    def close(self) -> None:
        AppHelper.callAfter(self._close_impl)

    def _close_impl(self) -> None:
        self._w.orderOut_(None)


def show_error_alert(message: str) -> None:
    """Modal failure alert with a single Quit button.

    Called when the installer can't recover (no network, repeated pip
    failures). Returns when the user dismisses; caller should follow
    with ``rumps.quit_application()``.
    """
    alert = NSAlert.alloc().init()
    alert.setMessageText_("Voitta Bookmarklet — setup failed")
    alert.setInformativeText_(message)
    alert.addButtonWithTitle_("Quit")
    alert.runModal()
