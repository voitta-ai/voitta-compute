"""``get_script_errors(name, since_ts?, limit?)`` — read the error log.

Reads the FIFO on-disk JSONL log so the LLM can investigate errors
that happened after a "looks ready" render (user interactions, late
effects). Errors that just happened are also visible from the
in-memory ring; we union the two so the model doesn't miss anything.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from app.reports import render_events
from app.reports.slug import InvalidSlug, validate_slug
from app.tools.registry import ToolCtx, ToolSpec, registry


async def _handler(args: dict[str, Any], _ctx: ToolCtx) -> dict[str, Any]:
    name = args.get("name") or ""
    since_ts = args.get("since_ts")
    limit = int(args.get("limit") or 50)
    try:
        validate_slug(name)
    except InvalidSlug as exc:
        return {"ok": False, "error": str(exc)}

    # Union in-memory recent + on-disk log, dedup by (ts, kind, message).
    ring = render_events.recent(name, since_ts=since_ts, limit=limit)
    disk = render_events.read_log(name, limit=limit)
    seen: set[tuple[str, str, str]] = set()
    merged: list[dict[str, Any]] = []
    for e in (*disk, *ring):
        key = (e.ts, e.kind, e.message)
        if key in seen:
            continue
        if since_ts and e.ts <= since_ts:
            continue
        seen.add(key)
        merged.append(asdict(e))
    merged.sort(key=lambda d: d["ts"])
    return {"ok": True, "name": name, "events": merged[-limit:]}


registry.register(
    ToolSpec(
        name="get_script_errors",
        description=(
            "Read the render-event log for a script. Returns errors and "
            "info events the FE posted to /api/report-render-events, "
            "merged from in-memory ring + on-disk log."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "since_ts": {"type": "string", "description": "ISO-8601, exclusive lower bound"},
                "limit": {"type": "integer", "default": 50},
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        side="server",
        handler=_handler,
    )
)
