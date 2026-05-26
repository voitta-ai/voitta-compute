"""Render-event drain: the FE → BE channel for "report rendered" and
"report errored after render" signals.

The FE posts to ``/api/report-render-events`` (route lives in
:mod:`app.routes.reports`, added in R2). This module:

* keeps an in-memory FIFO ring per slug for the ``show_report`` tool to
  ``await`` immediately;
* persists the same events to an append-only JSONL log under
  ``ERROR_LOGS_DIR/<slug>.jsonl``, capped at ``MAX_LOG_LINES``;
* exposes ``record_inventory()`` / ``read_inventory()`` for the
  "verify" path — one file per slug, latest-wins.

The R1 spike below is the storage + read API. R2 wires up the HTTP
route and the ``await``-able waiters; we don't add them yet to keep
each phase contained.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Deque, Optional

from app.reports.paths import ERROR_LOGS_DIR, INVENTORY_DIR
from app.reports.slug import validate_slug

# Per-slug ring of recent events. Bounded so a misbehaving script
# can't grow it without limit; older events spill to the on-disk log.
RING_SIZE = 64
MAX_LOG_LINES = 200


@dataclass
class RenderEvent:
    slug: str
    kind: str             # "ready" | "error" | "inventory" | "info"
    render_id: str = ""
    message: str = ""
    detail: dict[str, Any] = field(default_factory=dict)
    ts: str = ""

    def __post_init__(self) -> None:
        if not self.ts:
            self.ts = datetime.now(timezone.utc).isoformat()


# In-memory state. One ring + waiter set per slug. Module-global is
# fine because the BE is a single process; tests reset via
# :func:`_reset_for_tests`.
_rings: dict[str, Deque[RenderEvent]] = {}
_waiters: dict[str, set[asyncio.Future[RenderEvent]]] = {}


def _ring(slug: str) -> Deque[RenderEvent]:
    return _rings.setdefault(slug, deque(maxlen=RING_SIZE))


def record(event: RenderEvent) -> None:
    """Record a render event.

    Pushes onto the in-memory ring, wakes any awaiters, and appends to
    the on-disk log (best-effort — disk errors don't take down the
    in-memory channel).

    Consecutive identical events (same ``slug``, ``kind``, ``message``)
    are deduped: a counter is bumped on the previous ring entry instead
    of appending a new one. A misbehaving script firing the same error
    each animation frame can't blow out the log this way.
    """
    validate_slug(event.slug)
    ring = _ring(event.slug)
    if _matches_last(ring, event):
        last = ring[-1]
        last.detail = {**last.detail, "count": int(last.detail.get("count", 1)) + 1}
        last.ts = event.ts
        # Still wake waiters — a duplicate "ready" right after the first
        # would be silly, but ``wait_for`` is the right callback to fire.
        _wake_waiters(event.slug, last)
        # Skip disk append for dedup-eligible repeats — the in-memory
        # counter is the source of truth.
        return

    ring.append(event)
    _wake_waiters(event.slug, event)
    _append_log(event)


def _matches_last(ring: Deque[RenderEvent], event: RenderEvent) -> bool:
    if not ring:
        return False
    prev = ring[-1]
    return (
        prev.slug == event.slug
        and prev.kind == event.kind
        and prev.message == event.message
    )


def _wake_waiters(slug: str, event: RenderEvent) -> None:
    waiters = _waiters.get(slug)
    if not waiters:
        return
    for fut in list(waiters):
        if not fut.done():
            fut.set_result(event)
    waiters.clear()


def recent(slug: str, *, since_ts: Optional[str] = None, limit: int = 50) -> list[RenderEvent]:
    """In-memory events for ``slug``, optionally newer than ``since_ts``."""
    validate_slug(slug)
    items = list(_ring(slug))
    if since_ts:
        items = [e for e in items if e.ts > since_ts]
    if limit > 0:
        items = items[-limit:]
    return items


def read_log(slug: str, *, limit: int = 50) -> list[RenderEvent]:
    """Read the persistent JSONL log (most-recent ``limit`` lines).

    Used by ``get_script_errors`` so the model can see errors that
    happened after the in-memory ring rotated out.
    """
    validate_slug(slug)
    path = _log_path(slug)
    if not path.is_file():
        return []
    out: list[RenderEvent] = []
    for raw in path.read_text().splitlines()[-limit:]:
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
            out.append(RenderEvent(**data))
        except Exception:
            continue
    return out


def record_inventory(slug: str, inventory: dict[str, Any]) -> None:
    """Latest-wins per-slug snapshot for the ``verify_report`` path."""
    validate_slug(slug)
    INVENTORY_DIR.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(INVENTORY_DIR / f"{slug}.json", json.dumps(inventory, indent=2))


def read_inventory(slug: str) -> Optional[dict[str, Any]]:
    validate_slug(slug)
    path = INVENTORY_DIR / f"{slug}.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


async def wait_for(slug: str, *, timeout: float) -> Optional[RenderEvent]:
    """Block until the next event for ``slug`` arrives, or timeout.

    Used by ``run_script`` in R2: after firing the ``show_report``
    ``call_fn``, the orchestrator awaits ``wait_for(slug, timeout=...)``
    to know whether the pane rendered cleanly.
    """
    validate_slug(slug)
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[RenderEvent] = loop.create_future()
    _waiters.setdefault(slug, set()).add(fut)
    try:
        return await asyncio.wait_for(fut, timeout=timeout)
    except asyncio.TimeoutError:
        return None
    finally:
        _waiters.get(slug, set()).discard(fut)


# ---- internal --------------------------------------------------------


def _log_path(slug: str) -> Path:
    return ERROR_LOGS_DIR / f"{slug}.jsonl"


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def _append_log(event: RenderEvent) -> None:
    path = _log_path(event.slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(asdict(event), ensure_ascii=False)
    # Append, then truncate from the head if we exceeded MAX_LOG_LINES.
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    lines = path.read_text(encoding="utf-8").splitlines()
    if len(lines) > MAX_LOG_LINES:
        _atomic_write_text(path, "\n".join(lines[-MAX_LOG_LINES:]) + "\n")


def _reset_for_tests() -> None:
    """Test-only helper. Wipes module state."""
    _rings.clear()
    _waiters.clear()
