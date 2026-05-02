"""Helper used by hybrid tools to invoke a browser primitive via the bridge."""

from __future__ import annotations

import asyncio
from typing import Any

from app.bridge import ToolBridgeError, bridge
from app.tools.registry import ToolCtx


class BrowserToolError(RuntimeError):
    def __init__(self, kind: str, message: str, details: Any = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.details = details


# How long to wait for the browser to reconnect when the inbox is
# transiently down (tab woke up, backend restarted, network blip).
# We poll bridge.get(sid).connected every 250 ms — typical reconnect
# completes in <1 s once the tab is awake.
_RECONNECT_WAIT_S = 6.0
_RECONNECT_POLL_S = 0.25


async def _await_session_connected(sid: str, timeout_s: float) -> bool:
    """Wait up to ``timeout_s`` for the bridge to show this session
    as connected. Returns True if it became connected, False on timeout."""
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        s = bridge.get(sid)
        if s is not None and s.connected:
            return True
        await asyncio.sleep(_RECONNECT_POLL_S)
    return False


async def call_browser(
    name: str,
    args: dict[str, Any] | None,
    ctx: ToolCtx,
    timeout_ms: int = 15_000,
) -> Any:
    """Invoke a browser primitive on the session in ``ctx``.

    Self-repair: if the bridge says ``no_session`` (typical causes:
    backend restart wiped state, browser tab was suspended and the
    SSE dropped, transient network blip), we wait briefly for the
    browser's auto-reconnect + re-register to land, then retry the
    call once. Combined with ``bridge.register`` accepting unknown
    sids, this means the user no longer has to click the bookmarklet
    after a backend restart or a tab idle.

    Raises ``BrowserToolError`` on no-session (after retry), timeout,
    or primitive failure so the wrapping tool handler can decide how
    to surface it to the model.
    """

    if not ctx.session_id:
        raise BrowserToolError(
            "no_session",
            "no browser session is connected; ask the user to click the bookmarklet",
        )
    try:
        res = await bridge.call(ctx.session_id, name, args or {}, timeout_ms=timeout_ms)
    except ToolBridgeError as exc:
        if exc.kind == "no_session":
            # Wait for the browser to re-attach the inbox SSE + re-register.
            recovered = await _await_session_connected(ctx.session_id, _RECONNECT_WAIT_S)
            if not recovered:
                raise BrowserToolError(
                    exc.kind,
                    f"{exc} (waited {_RECONNECT_WAIT_S:.0f}s for browser reconnect; "
                    "bookmarklet may not be running, the tab may be unloaded, or "
                    "the network is down)",
                ) from exc
            try:
                res = await bridge.call(ctx.session_id, name, args or {}, timeout_ms=timeout_ms)
            except ToolBridgeError as exc2:
                raise BrowserToolError(exc2.kind, str(exc2)) from exc2
        else:
            raise BrowserToolError(exc.kind, str(exc)) from exc
    if not res.ok:
        err = res.error or {"kind": "unknown", "message": "browser tool failed"}
        raise BrowserToolError(
            err.get("kind") or "error",
            err.get("message") or "",
            err.get("details"),
        )
    return res.result
