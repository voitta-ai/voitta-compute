"""Smoke-test tool: returns ``document.title`` from the user's page.

Hybrid wrapper around the FE's ``get_page_title`` primitive.
"""

from __future__ import annotations

from typing import Any

from app.tools.browser import call_browser
from app.tools.registry import ToolCtx, ToolSpec, registry


async def _handler(args: dict[str, Any], ctx: ToolCtx) -> Any:
    return await call_browser("get_page_title", args, ctx)


registry.register(
    ToolSpec(
        name="get_page_title",
        description="Return the document.title of the page the user is currently viewing.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=_handler,
        side="hybrid",
    )
)
