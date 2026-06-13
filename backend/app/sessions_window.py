"""Active-sessions window (PyObjC NSWindow) — the "tasks voitta" view.

A cmd-tab-style list of every live bookmarklet session: one row per
session showing the page title and the host/URL the widget is open on.
Clicking a row makes that session the active one (where voice input is
routed) and best-effort raises its browser tab.

Opened by the "tasks voitta" voice command (see app.services.voice) and
by the tray menu's "Active sessions…" item. Built and mutated only on
the Cocoa main thread — every public method hops there via
``AppHelper.callAfter``, mirroring app.install_window.
"""

from __future__ import annotations

from typing import Any, Callable

import objc
from AppKit import (
    NSBackingStoreBuffered,
    NSButton,
    NSColor,
    NSFont,
    NSMakeRect,
    NSScrollView,
    NSTextField,
    NSView,
    NSWindow,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskMiniaturizable,
    NSWindowStyleMaskTitled,
)
from Foundation import NSObject
from PyObjCTools import AppHelper

_WINDOW_W = 560
_WINDOW_H = 480
_ROW_H = 60
_PAD = 20


class _FlippedView(NSView):
    """Top-left origin so rows lay out top-down like a normal list."""

    def isFlipped(self) -> bool:  # noqa: N802 (Cocoa selector)
        return True


class _RowTarget(NSObject):
    """Click target carrying the session id + callback for one row."""

    def initWithSession_callback_(self, sid, cb):  # noqa: N802
        self = objc.super(_RowTarget, self).init()
        if self is None:
            return None
        self._sid = sid
        self._cb = cb
        return self

    def clicked_(self, _sender):  # noqa: N802
        try:
            self._cb(self._sid)
        except Exception:  # noqa: BLE001
            pass


def _short_url(info: dict[str, Any]) -> str:
    url = info.get("url") or info.get("host") or ""
    # Strip scheme for a tidier line; keep host+path.
    for prefix in ("https://", "http://"):
        if url.startswith(prefix):
            url = url[len(prefix):]
            break
    return url or "(unknown page)"


def _row_title(info: dict[str, Any]) -> str:
    title = (info.get("title") or "").strip()
    if title:
        return title
    host = info.get("host") or ""
    return host or "Untitled session"


class SessionsWindow:
    """Reusable window listing live bookmarklet sessions."""

    def __init__(self, on_select: Callable[[str], None]) -> None:
        self._on_select = on_select
        self._row_targets: list[_RowTarget] = []  # keep refs alive

        rect = NSMakeRect(0, 0, _WINDOW_W, _WINDOW_H)
        style = (
            NSWindowStyleMaskTitled
            | NSWindowStyleMaskClosable
            | NSWindowStyleMaskMiniaturizable
        )
        self._w = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, style, NSBackingStoreBuffered, False
        )
        self._w.setTitle_("Voitta — Active sessions")
        self._w.setReleasedWhenClosed_(False)
        self._w.center()

        cv = self._w.contentView()

        title = NSTextField.labelWithString_("Active sessions")
        title.setFrame_(((20, _WINDOW_H - 42), (_WINDOW_W - 40, 24)))
        title.setFont_(NSFont.boldSystemFontOfSize_(15))
        cv.addSubview_(title)

        self._subtitle = NSTextField.labelWithString_("")
        self._subtitle.setFrame_(((20, _WINDOW_H - 62), (_WINDOW_W - 40, 16)))
        self._subtitle.setFont_(NSFont.systemFontOfSize_(11))
        self._subtitle.setTextColor_(NSColor.secondaryLabelColor())
        cv.addSubview_(self._subtitle)

        sep = NSTextField.labelWithString_("")
        sep.setFrame_(((20, _WINDOW_H - 70), (_WINDOW_W - 40, 1)))
        sep.setBackgroundColor_(NSColor.separatorColor())
        sep.setDrawsBackground_(True)
        cv.addSubview_(sep)

        # Scrollable list area.
        list_y = 56
        list_h = (_WINDOW_H - 78) - list_y
        self._scroll = NSScrollView.alloc().initWithFrame_(
            ((20, list_y), (_WINDOW_W - 40, list_h))
        )
        self._scroll.setHasVerticalScroller_(True)
        self._scroll.setDrawsBackground_(False)
        self._scroll.setBorderType_(0)
        self._doc = _FlippedView.alloc().initWithFrame_(
            ((0, 0), (_WINDOW_W - 40, list_h))
        )
        self._scroll.setDocumentView_(self._doc)
        cv.addSubview_(self._scroll)
        self._list_w = _WINDOW_W - 40
        self._list_h = list_h

        # Footer: refresh button.
        self._refresh_cb: Callable[[], None] | None = None
        self._refresh_target = _RefreshTarget.alloc().initWithWindow_(self)
        btn = NSButton.alloc().initWithFrame_(((20, 14), (110, 30)))
        btn.setTitle_("Refresh")
        btn.setBezelStyle_(1)  # NSBezelStyleRounded
        btn.setTarget_(self._refresh_target)
        btn.setAction_("clicked:")
        cv.addSubview_(btn)

        self._hint = NSTextField.labelWithString_(
            "Click a session to focus it · say “tasks voitta” to reopen this"
        )
        self._hint.setFrame_(((140, 18), (_WINDOW_W - 160, 22)))
        self._hint.setFont_(NSFont.systemFontOfSize_(10))
        self._hint.setTextColor_(NSColor.tertiaryLabelColor())
        cv.addSubview_(self._hint)

    # ---- public API (thread-safe) ------------------------------------------

    def set_refresh_handler(self, fn: Callable[[], None]) -> None:
        self._refresh_cb = fn

    def is_visible(self) -> bool:
        try:
            return bool(self._w.isVisible())
        except Exception:  # noqa: BLE001
            return False

    def show(self, sessions: list[dict[str, Any]], active_id: str | None) -> None:
        AppHelper.callAfter(self._show_impl, sessions, active_id)

    def update(self, sessions: list[dict[str, Any]], active_id: str | None) -> None:
        AppHelper.callAfter(self._rebuild, sessions, active_id)

    # ---- impl (main thread) ------------------------------------------------

    def _show_impl(self, sessions, active_id) -> None:
        self._rebuild(sessions, active_id)
        self._w.makeKeyAndOrderFront_(None)
        self._w.center()
        from AppKit import NSApp
        NSApp.activateIgnoringOtherApps_(True)

    def _fire_refresh(self) -> None:
        if self._refresh_cb is not None:
            try:
                self._refresh_cb()
            except Exception:  # noqa: BLE001
                pass

    def _rebuild(self, sessions, active_id) -> None:
        # Drop old rows.
        for sub in list(self._doc.subviews()):
            sub.removeFromSuperview()
        self._row_targets = []

        n = len(sessions)
        self._subtitle.setStringValue_(
            "No bookmarklet sessions are open right now."
            if n == 0 else f"{n} session{'s' if n != 1 else ''} open"
        )

        content_h = max(self._list_h, n * _ROW_H)
        self._doc.setFrame_(((0, 0), (self._list_w, content_h)))

        if n == 0:
            empty = NSTextField.labelWithString_(
                "Open the bookmarklet on a page, then reopen this window."
            )
            empty.setFrame_(((12, 12), (self._list_w - 24, 20)))
            empty.setFont_(NSFont.systemFontOfSize_(12))
            empty.setTextColor_(NSColor.secondaryLabelColor())
            self._doc.addSubview_(empty)
            return

        for i, info in enumerate(sessions):
            self._add_row(i, info, info.get("session_id") == active_id)

    def _add_row(self, i: int, info: dict[str, Any], is_active: bool) -> None:
        y = i * _ROW_H
        w = self._list_w
        row = NSView.alloc().initWithFrame_(((0, y), (w, _ROW_H - 6)))
        if is_active:
            try:
                row.setWantsLayer_(True)
                tint = NSColor.selectedControlColor().colorWithAlphaComponent_(0.35)
                row.layer().setBackgroundColor_(tint.CGColor())
                row.layer().setCornerRadius_(6.0)
            except Exception:  # noqa: BLE001 — highlight is cosmetic
                pass

        marker = "●  " if is_active else ""
        title = NSTextField.labelWithString_(marker + _row_title(info))
        title.setFrame_(((12, 28), (w - 110, 20)))
        title.setFont_(NSFont.boldSystemFontOfSize_(13))
        if not info.get("connected", True):
            title.setTextColor_(NSColor.tertiaryLabelColor())
        row.addSubview_(title)

        sub = NSTextField.labelWithString_(_short_url(info))
        sub.setFrame_(((12, 8), (w - 110, 16)))
        sub.setFont_(NSFont.systemFontOfSize_(11))
        sub.setTextColor_(NSColor.secondaryLabelColor())
        row.addSubview_(sub)

        status = NSTextField.labelWithString_(
            "active" if is_active
            else ("connected" if info.get("connected", True) else "stale")
        )
        status.setFrame_(((w - 96, 18), (84, 16)))
        status.setFont_(NSFont.systemFontOfSize_(10))
        status.setTextColor_(
            NSColor.controlAccentColor() if is_active
            else NSColor.tertiaryLabelColor()
        )
        from AppKit import NSTextAlignmentRight
        status.setAlignment_(NSTextAlignmentRight)
        row.addSubview_(status)

        # Transparent full-row click button on top.
        sid = info.get("session_id")
        target = _RowTarget.alloc().initWithSession_callback_(sid, self._on_select)
        self._row_targets.append(target)
        hit = NSButton.alloc().initWithFrame_(((0, 0), (w, _ROW_H - 6)))
        hit.setTitle_("")
        hit.setBordered_(False)
        hit.setTransparent_(True)
        hit.setTarget_(target)
        hit.setAction_("clicked:")
        row.addSubview_(hit)

        self._doc.addSubview_(row)


class _RefreshTarget(NSObject):
    def initWithWindow_(self, win):  # noqa: N802
        self = objc.super(_RefreshTarget, self).init()
        if self is None:
            return None
        self._win = win
        return self

    def clicked_(self, _sender):  # noqa: N802
        self._win._fire_refresh()
