"""``verify_script(name)`` — return the last render's inventory.

The FE writes an inventory (dims, series counts, anything the
renderer's pane knows about itself) to ``INVENTORY_DIR/<slug>.json``
when it acknowledges a clean render. This tool exposes it to the LLM
so it can assert "the plot has 2 series, x-axis labelled 'time'"
without screenshotting.

If the script hasn't been run yet or the FE never acknowledged,
returns ``{ok: false, error: 'no inventory'}``.
"""

from __future__ import annotations

from typing import Any

from app.reports import render_events
from app.reports.slug import InvalidSlug, validate_slug
from app.tools.registry import ToolCtx, ToolSpec, registry


async def _handler(args: dict[str, Any], _ctx: ToolCtx) -> dict[str, Any]:
    name = args.get("name") or ""
    try:
        validate_slug(name)
    except InvalidSlug as exc:
        return {"ok": False, "error": str(exc)}
    inv = render_events.read_inventory(name)
    if inv is None:
        return {"ok": False, "error": f"no inventory for {name!r} — run the script first"}
    return {"ok": True, "name": name, "inventory": inv}


registry.register(
    ToolSpec(
        name="verify_script",
        description=(
            "Return the last-render inventory written by the FE for "
            "this script — dims, series counts, axis labels, etc. Lets "
            "you confirm what rendered without a screenshot."
        ),
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
