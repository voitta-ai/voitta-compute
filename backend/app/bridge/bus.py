"""In-memory bus for the server↔browser tool channel.

One process-wide ``ToolBridge`` instance owns a dict of sessions. Each
session has an ``asyncio.Queue`` (events the SSE inbox stream pulls from)
and a dict of pending ``Future``s (one per outstanding browser tool call).

A server-side hybrid tool calls ``await bridge.call(session_id, name, args)``
which:

  1. allocates a ``call_id`` + Future,
  2. enqueues an ``event: call`` for the inbox,
  3. waits on the Future with a timeout,
  4. on timeout / cancellation enqueues ``event: cancel`` and raises.

The browser POSTs to ``/tools/result`` which calls
``bridge.deliver_result(...)`` to resolve the matching Future.
"""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ToolBridgeError(Exception):
    """Base error class for bridge problems (no session, timeout, etc.)."""

    def __init__(self, kind: str, message: str = "") -> None:
        super().__init__(message or kind)
        self.kind = kind


@dataclass
class ToolCallResult:
    ok: bool
    result: Any = None
    error: dict[str, Any] | None = None
    latency_ms: int = 0


@dataclass
class _Session:
    sid: str
    inbox: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=256))
    pending: dict[str, asyncio.Future] = field(default_factory=dict)
    capabilities: list[str] = field(default_factory=list)
    identification: dict[str, Any] = field(default_factory=dict)
    connected: bool = False
    inbox_streams: int = 0
    created_at: datetime = field(default_factory=_now)
    last_seen: datetime = field(default_factory=_now)

    def summary(self) -> dict[str, Any]:
        return {
            "session_id": self.sid,
            "connected": self.connected,
            "inbox_streams": self.inbox_streams,
            "capabilities": self.capabilities,
            "pending_calls": list(self.pending.keys()),
            "identification": self.identification,
            "created_at": self.created_at.isoformat(),
            "last_seen": self.last_seen.isoformat(),
        }


class ToolBridge:
    def __init__(self) -> None:
        self._sessions: dict[str, _Session] = {}
        self._lock = asyncio.Lock()

    # ---- session bookkeeping -------------------------------------------------

    async def get_or_create(self, sid: str) -> _Session:
        async with self._lock:
            s = self._sessions.get(sid)
            if s is None:
                s = _Session(sid=sid)
                self._sessions[sid] = s
            return s

    def get(self, sid: str) -> _Session | None:
        return self._sessions.get(sid)

    def list_sessions(self) -> list[dict[str, Any]]:
        return [s.summary() for s in self._sessions.values()]

    async def disconnect(self, sid: str) -> None:
        s = self._sessions.get(sid)
        if not s:
            return
        s.inbox_streams = max(0, s.inbox_streams - 1)
        if s.inbox_streams == 0:
            s.connected = False
            for cid, fut in list(s.pending.items()):
                if not fut.done():
                    fut.set_exception(ToolBridgeError("session_closed"))
                s.pending.pop(cid, None)

    # ---- registration --------------------------------------------------------

    def register(
        self,
        sid: str,
        capabilities: list[str],
        identification: dict[str, Any],
    ) -> None:
        """Record / refresh a session's capabilities + identification.

        Accepts unknown ``sid`` by **recreating** an empty session
        bucket. This makes post-restart recovery automatic: when the
        backend wipes its in-memory state (uvicorn restart, crash,
        autoreload), the browser's next register lands on a clean
        bucket and the bridge becomes usable again as soon as the
        next /tools/inbox SSE attaches. Without this, the browser
        would have to discard its session_id and pick a new one.

        ``connected`` stays False on re-creation; flipped to True by
        the inbox EventSource handler the next time the SSE attaches.
        """
        s = self._sessions.get(sid)
        if s is None:
            s = _Session(sid=sid)
            self._sessions[sid] = s
        s.capabilities = capabilities
        s.identification = identification
        s.last_seen = _now()

    # ---- calling browser tools ----------------------------------------------

    async def call(
        self,
        sid: str,
        name: str,
        args: dict[str, Any] | None = None,
        timeout_ms: int = 15_000,
    ) -> ToolCallResult:
        s = self._sessions.get(sid)
        if s is None or not s.connected:
            raise ToolBridgeError("no_session", f"no connected inbox for {sid}")
        if name not in s.capabilities:
            raise ToolBridgeError(
                "unknown_primitive",
                f"primitive {name!r} not in session capabilities {s.capabilities}",
            )

        loop = asyncio.get_running_loop()
        call_id = "c-" + secrets.token_hex(8)
        fut: asyncio.Future = loop.create_future()
        s.pending[call_id] = fut

        started = loop.time()
        await s.inbox.put(
            {
                "event": "call",
                "data": {
                    "call_id": call_id,
                    "name": name,
                    "args": args or {},
                    "timeout_ms": timeout_ms,
                },
            }
        )

        try:
            envelope: dict[str, Any] = await asyncio.wait_for(fut, timeout=timeout_ms / 1000)
        except asyncio.TimeoutError:
            with _suppress():
                await s.inbox.put({"event": "cancel", "data": {"call_id": call_id}})
            raise ToolBridgeError("timeout", f"{name} timed out after {timeout_ms}ms") from None
        except asyncio.CancelledError:
            with _suppress():
                await s.inbox.put({"event": "cancel", "data": {"call_id": call_id}})
            raise
        finally:
            s.pending.pop(call_id, None)

        latency_ms = int((loop.time() - started) * 1000)
        if envelope.get("ok"):
            return ToolCallResult(ok=True, result=envelope.get("result"), latency_ms=latency_ms)
        return ToolCallResult(
            ok=False,
            error=envelope.get("error") or {"kind": "unknown", "message": "no error payload"},
            latency_ms=latency_ms,
        )

    def deliver_result(
        self,
        sid: str,
        call_id: str,
        ok: bool,
        result: Any = None,
        error: dict[str, Any] | None = None,
    ) -> bool:
        s = self._sessions.get(sid)
        if s is None:
            return False
        s.last_seen = _now()
        fut = s.pending.get(call_id)
        if fut is None or fut.done():
            return False
        fut.set_result({"ok": ok, "result": result, "error": error})
        return True


class _suppress:
    """Tiny context manager to swallow exceptions during best-effort cleanup."""

    def __enter__(self) -> "_suppress":
        return self

    def __exit__(self, *_exc: object) -> bool:
        return True


bridge = ToolBridge()
