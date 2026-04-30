"""Page-context tool: a thin wrapper over the ``get_url`` browser primitive.

Returns the SPA's current URL components. Useful when the model needs to
decide whether the user is on a workitem page, the report editor, etc.
The id-parsing logic itself lives client-side (the JS counterpart in
``the original plugin/lib/tools/context.js`` extracts ``workitemId`` /
``reportId`` from the path); for now we expose the raw components and let
the model pattern-match.
"""

from __future__ import annotations

from typing import Any

from app.tools.browser import call_browser
from app.tools.registry import ToolCtx, ToolSpec, registry


async def _get_page_context(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    url = await call_browser("get_url", {}, ctx)
    selection = await call_browser("read_selection", {}, ctx)
    return {
        "url": url,
        "selection": selection.get("text", "") if isinstance(selection, dict) else "",
    }


registry.register(
    ToolSpec(
        name="get_page_context",
        description=(
            "Return the SPA's current URL components (href / pathname / "
            "search / hash / title) and any text the user has currently "
            "selected. Use this when you need to know what page the user "
            "is looking at or which entity id is in the path."
        ),
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=_get_page_context,
        side="hybrid",
    )
)
