"""``get_active_report()`` — which report is currently mounted in the pane."""

from __future__ import annotations

from typing import Any

from app.tools.registry import ToolCtx, ToolSpec, registry


async def _handler(_args: dict[str, Any], _ctx: ToolCtx) -> dict[str, Any]:
    from app.routes.workspace import _active_tab
    if not _active_tab.get("tab"):
        return {"ok": True, "tab": None, "name": None, "title": None,
                "note": "No report is currently mounted in the pane."}
    return {"ok": True, **_active_tab}


registry.register(
    ToolSpec(
        name="get_active_report",
        description=(
            "Returns which report is currently visible in the report pane.\n\n"
            "  tab   — render_id of the active tab ('workspace' or a script slug)\n"
            "  name  — script name (slug), use with get_script / edit_script\n"
            "  title — human-readable tab title\n\n"
            "Returns tab: null if no report pane is open."
        ),
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        side="server",
        handler=_handler,
    )
)
