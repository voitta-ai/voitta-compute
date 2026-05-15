"""Render-event store for HoloViz Panel reports.

Bokeh's client-side init can fail AFTER ``build(ctx)`` has returned
successfully (SlickGrid stylesheet race, lazy widget timeouts, JS
exceptions inside CustomJS), so the smoke test in ``services/scripts``
can't see those failures. This module is the missing feedback channel:

  • The shim injected into every report iframe (``/api/_panel_shim.js``)
    captures ``window.error``, ``unhandledrejection``, and Bokeh's
    "Error rendering Bokeh items" console messages, plus a ``ready``
    signal when the document finishes initialising. Each event is
    posted up to the parent ChatPane via ``window.parent.postMessage``.

  • The parent ChatPane forwards every event to ``/api/report-render-events``
    (``record(...)`` below).

  • ``show_holoviz_report`` mints a per-show ``render_id``, embeds it in
    the iframe URL, and ``await_render(render_id, timeout)``s for either
    a ``ready`` or the first ``error``. The tool result includes the
    outcome so the LLM can self-correct on the SAME turn.

  • ``get_report_render_errors(report_id)`` reads the persisted log so
    the LLM can pull errors that surfaced AFTER the show returned (e.g.
    after user interaction).

In-memory state for awaits is keyed by ``render_id`` and cleared shortly
after the await completes (or via TTL). Persistent log is per-script
under ``scripts/reports/<slug>/render_log.json`` (capped, FIFO trim).
"""

from __future__ import annotations

import asyncio
import json
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


PROJECT_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_REPORTS = PROJECT_ROOT / "scripts" / "reports"

# Cap the on-disk log so a chatty report can't grow it without bound.
LOG_MAX_ENTRIES = 200

# How long an await keeps the in-memory render-state around after it
# completes — gives a little slop for late-arriving duplicates.
TTL_AFTER_COMPLETE_S = 30.0


EventKind = Literal["ready", "error"]


@dataclass
class RenderEvent:
    render_id: str
    report_id: str
    kind: EventKind
    ts: float
    message: str | None = None
    stack: str | None = None
    source: str | None = None  # 'window.error' | 'unhandledrejection' | 'console.error' | 'bokeh' | ...
    url: str | None = None  # script URL where the error fired
    line: int | None = None
    col: int | None = None

    def to_dict(self) -> dict:
        return {
            "render_id": self.render_id,
            "report_id": self.report_id,
            "kind": self.kind,
            "ts": self.ts,
            "message": self.message,
            "stack": self.stack,
            "source": self.source,
            "url": self.url,
            "line": self.line,
            "col": self.col,
        }


@dataclass
class _RenderState:
    """Per-render in-memory bookkeeping for awaits."""
    report_id: str
    started_at: float
    events: list[RenderEvent] = field(default_factory=list)
    ready: asyncio.Event | None = None  # set when ANY event lands
    # Loop captured at begin_await() time. We can't rely on
    # ``ready._loop`` — Python 3.10+ doesn't bind asyncio.Event to a
    # loop at construction; ``_loop`` stays None until ``wait()`` is
    # called. If we wait until then to look it up, ``record()`` paths
    # that fire BEFORE the awaiter has called wait() (the normal race
    # for fast server-side errors) drop the signal.
    ready_loop: asyncio.AbstractEventLoop | None = None
    completed_at: float | None = None


_states: dict[str, _RenderState] = {}
_lock = threading.Lock()


# ---- public API ----------------------------------------------------------


def new_render_id() -> str:
    """Mint a short, URL-safe id used to correlate iframe events with a
    pending ``show_holoviz_report`` await."""
    return secrets.token_hex(6)


def begin_await(render_id: str, report_id: str) -> asyncio.Event:
    """Register interest in events for ``render_id``. Returns the
    ``asyncio.Event`` that ``record()`` will set on each delivery.

    Created lazily on the running loop. Must be called from the same
    asyncio loop that will later ``await`` the event.
    """
    loop = asyncio.get_running_loop()
    with _lock:
        st = _states.get(render_id)
        if st is None:
            st = _RenderState(report_id=report_id, started_at=time.time())
            _states[render_id] = st
        if st.ready is None:
            st.ready = asyncio.Event()
        # Capture the running loop so record() can signal across thread
        # boundaries even when the awaiter hasn't called wait() yet.
        st.ready_loop = loop
        # If events arrived BEFORE the await registered (race), surface them
        # immediately by pre-setting the event.
        if st.events:
            loop.call_soon_threadsafe(st.ready.set)
        return st.ready


def record(
    *,
    render_id: str,
    report_id: str,
    kind: EventKind,
    message: str | None = None,
    stack: str | None = None,
    source: str | None = None,
    url: str | None = None,
    line: int | None = None,
    col: int | None = None,
) -> RenderEvent:
    """Persist + signal an event from the iframe.

    Two effects:

    1. Append to the per-script ``render_log.json`` (FIFO-trimmed).
    2. If a ``begin_await`` registered the render_id, store the event in
       memory and set the asyncio.Event so the awaiting tool wakes up.

    Returns the constructed RenderEvent.
    """
    ev = RenderEvent(
        render_id=render_id,
        report_id=report_id,
        kind=kind,
        ts=time.time(),
        message=_clip(message, 4000),
        stack=_clip(stack, 6000),
        source=source,
        url=url,
        line=line,
        col=col,
    )
    _append_log(report_id, ev)

    with _lock:
        st = _states.get(render_id)
        if st is None:
            # Event arrived BEFORE begin_await() registered (or no await
            # was ever started). Hold it for a TTL window so a slightly
            # late begin_await still surfaces it via its pre-await check.
            st = _RenderState(report_id=report_id, started_at=ev.ts)
            _states[render_id] = st
        st.events.append(ev)
        if kind in ("ready", "error") and st.completed_at is None:
            st.completed_at = ev.ts
        if st.ready is not None:
            # Signal across thread boundaries via the loop the awaiter
            # registered on. Capturing at begin_await() time (not now)
            # is what makes the race work — see _RenderState.ready_loop.
            loop = st.ready_loop
            if loop is not None and not loop.is_closed():
                loop.call_soon_threadsafe(st.ready.set)
    _gc_states()
    return ev


def collect(render_id: str) -> tuple[str | None, list[RenderEvent]]:
    """Return ``(report_id, events_so_far)`` for ``render_id`` from
    in-memory state. Call after ``begin_await()`` has fired (or timed
    out) to read what arrived.

    Returns ``(None, [])`` if the render_id is unknown.
    """
    with _lock:
        st = _states.get(render_id)
        if st is None:
            return None, []
        return st.report_id, list(st.events)


def end_await(render_id: str) -> None:
    """Mark an await as complete. State is GC'd ~TTL_AFTER_COMPLETE_S
    later. Idempotent."""
    with _lock:
        st = _states.get(render_id)
        if st is None:
            return
        if st.completed_at is None:
            st.completed_at = time.time()


def list_recent_for_report(
    report_id: str,
    *,
    since_ts: float | None = None,
    kinds: tuple[EventKind, ...] = ("error",),
    limit: int = 50,
) -> list[dict]:
    """Read the persisted log for ``report_id``, oldest→newest.

    Defaults filter to error kinds only and cap at 50 entries; pass
    ``since_ts=time.time() - 600`` to scope to the last 10 minutes, etc.
    """
    path = _log_path(report_id)
    if not path.exists():
        return []
    try:
        entries = json.loads(path.read_text())
    except Exception:
        return []
    if not isinstance(entries, list):
        return []
    out: list[dict] = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        if since_ts is not None and float(e.get("ts") or 0) < since_ts:
            continue
        if e.get("kind") not in kinds:
            continue
        out.append(e)
    return out[-limit:]


# ---- internals -----------------------------------------------------------


def _clip(s: str | None, n: int) -> str | None:
    if s is None:
        return None
    s = str(s)
    if len(s) <= n:
        return s
    return s[: n - 12] + "…[truncated]"


def _log_path(report_id: str) -> Path:
    # Slug used by report scripts is the same as the directory name.
    return SCRIPTS_REPORTS / report_id / "render_log.json"


def _append_log(report_id: str, ev: RenderEvent) -> None:
    path = _log_path(report_id)
    if not path.parent.exists():
        # Don't auto-create the script dir — if the report doesn't exist
        # we just drop the log entry rather than make a ghost folder.
        return
    try:
        existing: list = []
        if path.exists():
            try:
                existing = json.loads(path.read_text())
            except Exception:
                existing = []
        if not isinstance(existing, list):
            existing = []
        existing.append(ev.to_dict())
        if len(existing) > LOG_MAX_ENTRIES:
            existing = existing[-LOG_MAX_ENTRIES:]
        path.write_text(json.dumps(existing, ensure_ascii=False, indent=2))
    except Exception:
        # Logging shouldn't crash the request handler — swallow.
        pass


def _gc_states() -> None:
    """Drop completed render states older than the TTL. Cheap pass; called
    on every record()."""
    cutoff = time.time() - TTL_AFTER_COMPLETE_S
    with _lock:
        stale = [
            rid
            for rid, st in _states.items()
            if st.completed_at is not None and st.completed_at < cutoff
        ]
        for rid in stale:
            _states.pop(rid, None)
