"""``browser_eval`` — execute arbitrary JavaScript in the user's tab."""

from __future__ import annotations

from typing import Any

from app.tools.browser import BrowserToolError, call_browser
from app.tools.registry import ToolCtx, ToolSpec, registry


async def _handler(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    js = args.get("js")
    if not isinstance(js, str) or not js.strip():
        return {"ok": False, "error": "bad_request", "message": "js is required"}
    await_ms = int(args.get("await_ms") or 30_000)
    await_ms = max(100, min(120_000, await_ms))
    try:
        result = await call_browser(
            "eval_js", {"js": js, "await_ms": await_ms}, ctx, timeout_ms=await_ms + 5_000,
        )
    except BrowserToolError as exc:
        return {"ok": False, "error": exc.kind, "message": str(exc)}
    return result


registry.register(
    ToolSpec(
        name="browser_eval",
        description=(
            "Execute arbitrary JavaScript in the user's currently-bookmarklet'd "
            "browser tab.\n"
            "\n"
            "Runs in the page's origin, with full access to: document/DOM, "
            "localStorage, sessionStorage, document.cookie (non-HttpOnly), "
            "fetch (with the page's credentials), window globals, performance APIs.\n"
            "\n"
            "The body is wrapped in an async function. Top-level `await` works. "
            "Whatever you `return` from the script is sent back as the `result` "
            "field. Console output (log/warn/error) is captured into the `logs` "
            "array regardless of success.\n"
            "\n"
            "Inputs:\n"
            "  js       (string, required)  — JavaScript source. Must `return` "
            "the value you want the LLM to receive.\n"
            "  await_ms (integer, optional) — Hard timeout for the script. "
            "Default 30000, capped at 120000.\n"
            "\n"
            "Returns on success: {ok: true, result, logs: [{level, args}], ms}\n"
            "Returns on script throw: {ok: false, error: 'eval_threw', message, stack, logs, ms}\n"
            "Returns on transport failure: {ok: false, error: <kind>, message}\n"
            "\n"
            "Use this tool when no narrower plugin-provided primitive exists. "
            "When a purpose-built tool (e.g. simr_get_token, ebay_scrape_search) "
            "is available for the task, prefer that — it's faster and the result "
            "shape is stable."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "js": {
                    "type": "string",
                    "description": "JavaScript source to execute. Must `return` the value you want back.",
                },
                "await_ms": {
                    "type": "integer",
                    "description": "Timeout in milliseconds. Default 30000, max 120000.",
                },
            },
            "required": ["js"],
            "additionalProperties": False,
        },
        side="hybrid",
        global_tool=True,
        handler=_handler,
    )
)
