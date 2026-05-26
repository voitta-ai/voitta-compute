"""``get_script(name)`` — return source + meta (+ optional bundle).

Phase 1 returns just source + meta. R2 will extend to include the
last-run inventory + recent errors (the "debug bundle" option discussed
in the plan) — that's an additive change to the return shape, so
clients won't break.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from app.reports import store
from app.reports.slug import InvalidSlug, validate_slug
from app.tools.registry import ToolCtx, ToolSpec, registry


async def _handler(args: dict[str, Any], _ctx: ToolCtx) -> dict[str, Any]:
    name = args.get("name") or ""
    try:
        validate_slug(name)
    except InvalidSlug as exc:
        return {"ok": False, "error": str(exc)}
    if not store.exists(name):
        return {"ok": False, "error": f"script {name!r} does not exist"}
    return {
        "ok": True,
        "name": name,
        "code": store.read_code(name),
        "meta": asdict(store.read_meta(name)),
    }


registry.register(
    ToolSpec(
        name="get_script",
        description="Return the source code and metadata for a saved script.",
        input_schema={
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
            "additionalProperties": False,
        },
        side="server",
        handler=_handler,
    )
)
