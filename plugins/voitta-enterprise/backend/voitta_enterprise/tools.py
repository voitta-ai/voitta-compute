"""Voitta Enterprise portal tool registrations.

Placeholder — no tools registered yet. Add tools here following the
same pattern as the eBay and Google plugins: import ``registry`` and
call ``registry.register(ToolSpec(...))`` for each tool.

Example skeleton:

    from app.tools.registry import ToolCtx, ToolSpec, registry

    async def _get_page_context(args: dict, ctx: ToolCtx) -> dict:
        from app.tools.browser import call_browser
        return await call_browser("enterprise_get_page_context", args, ctx)

    registry.register(
        ToolSpec(
            name="enterprise_get_page_context",
            description="Return the current page type and URL on enterprise.voitta.ai.",
            parameters={"type": "object", "properties": {}, "required": []},
            handler=_get_page_context,
            host_pattern="enterprise.voitta.ai",
        )
    )
"""
