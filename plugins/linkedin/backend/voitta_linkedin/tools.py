"""LinkedIn tool registrations — thin shims over browser primitives
defined in plugins/linkedin/frontend/widget.ts.
"""

from __future__ import annotations

from typing import Any

from app.tools.browser import BrowserToolError, call_browser
from app.tools.registry import ToolCtx, ToolSpec, registry


# ---- linkedin_get_page_context -------------------------------------------


async def _get_page_context(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    try:
        info = await call_browser("linkedin_inspect_page", {}, ctx)
    except BrowserToolError as exc:
        return {"ok": False, "error": exc.kind, "message": str(exc)}
    return {"ok": True, **info}


registry.register(
    ToolSpec(
        name="linkedin_get_page_context",
        description=(
            "What page is the user looking at on linkedin.com? Returns "
            "{url, page_type, title, profile_id, company_slug, "
            "job_id, params}. "
            "page_type ∈ {feed, profile, company, jobs, job, "
            "messaging, mynetwork, notifications, search, other}.\n"
            "\n"
            "Call this first for any LinkedIn-flavoured task to learn "
            "which page the user is on."
        ),
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=_get_page_context,
        side="hybrid",
    )
)
