"""Generic DOM-access tools — available on every host.

Thin ToolSpec wrappers around the global browser primitives defined in
``frontend/src/lib/primitives.ts`` (``read_dom``, ``get_page_dump``).
No host gate, so any plugin / any page benefits.
"""

from __future__ import annotations

from typing import Any

from app.tools.browser import BrowserToolError, call_browser
from app.tools.registry import ToolCtx, ToolSpec, registry


# ---- read_dom -------------------------------------------------------------


async def _read_dom(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    selector = (args.get("selector") or "").strip()
    if not selector:
        return {"ok": False, "error": "invalid_args", "message": "selector is required"}
    kind = "html" if args.get("kind") == "html" else "text"
    try:
        info = await call_browser("read_dom", {"selector": selector, "kind": kind}, ctx)
    except BrowserToolError as exc:
        return {"ok": False, "error": exc.kind, "message": str(exc)}
    return {"ok": True, **info}


registry.register(
    ToolSpec(
        name="read_dom",
        description=(
            "Read a single element from the active tab's DOM. "
            "``selector`` is any CSS selector (uses "
            "``document.querySelector`` — first match only). "
            "``kind='text'`` (default) returns ``innerText``; "
            "``kind='html'`` returns ``outerHTML``.\n"
            "\n"
            "Returns {value, kind}. Errors: ``not_found`` (no match), "
            "``invalid_selector`` (bad CSS), ``too_large`` (>200 KB — "
            "use ``get_page_dump`` for whole-page reads)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "selector": {"type": "string", "description": "CSS selector"},
                "kind": {"type": "string", "enum": ["text", "html"], "default": "text"},
            },
            "required": ["selector"],
            "additionalProperties": False,
        },
        handler=_read_dom,
        side="hybrid",
    )
)


# ---- get_page_dump --------------------------------------------------------


async def _get_page_dump(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    try:
        info = await call_browser("get_page_dump", {}, ctx, timeout_ms=30_000)
    except BrowserToolError as exc:
        return {"ok": False, "error": exc.kind, "message": str(exc)}
    return {"ok": True, **info}


registry.register(
    ToolSpec(
        name="eval_js",
        description=(
            "Run arbitrary JavaScript in the active tab. The source is "
            "wrapped in an async function so ``await`` is allowed and "
            "the final expression's value is returned. Use this to "
            "drive the page: type into inputs, click buttons, dispatch "
            "events, navigate, read computed state, etc.\n"
            "\n"
            "Returns {ok, value, console, elapsed_ms, timed_out, error}. "
            "``value`` is JSON-safe-serialised (DOM nodes become "
            "``{__type:'Element', tagName, outerHTML, ...}``; cycles, "
            "Maps, Sets, Dates are preserved). ``console`` captures any "
            "log/info/warn/error/debug emitted during the eval.\n"
            "\n"
            "``await_ms`` (default 30000) bounds how long the script may "
            "run before being aborted with ``timed_out: true``.\n"
            "\n"
            "Examples:\n"
            "  • Click a button: ``document.querySelector('button[aria-label=\"Search\"]').click()``\n"
            "  • Drive a search input: set ``.value``, dispatch "
            "    ``input`` + ``change`` events, then submit the form.\n"
            "  • Scroll: ``window.scrollTo(0, document.body.scrollHeight)``"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "js": {"type": "string", "description": "JS source; runs as async function body"},
                "await_ms": {
                    "type": "integer", "minimum": 100, "maximum": 120_000, "default": 30_000,
                    "description": "Max time the script may run before abort.",
                },
            },
            "required": ["js"],
            "additionalProperties": False,
        },
        handler=lambda args, ctx: _eval_js(args, ctx),
        side="hybrid",
    )
)


async def _eval_js(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    src = (args.get("js") or "").strip()
    if not src:
        return {"ok": False, "error": "invalid_args", "message": "js is required"}
    payload: dict[str, Any] = {"js": src}
    if args.get("await_ms") is not None:
        payload["await_ms"] = int(args["await_ms"])
    try:
        info = await call_browser("eval_js", payload, ctx, timeout_ms=120_000)
    except BrowserToolError as exc:
        return {"ok": False, "error": exc.kind, "message": str(exc)}
    return info if isinstance(info, dict) else {"ok": True, "value": info}


registry.register(
    ToolSpec(
        name="get_page_dump",
        description=(
            "Dump the active tab's full HTML: returns {url, title, "
            "pathname, search, hash, html, user_agent, ts}. ``html`` is "
            "``document.documentElement.outerHTML`` — uncapped, can be "
            "large. Prefer ``read_dom`` with a CSS selector when you "
            "know which region you need."
        ),
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=_get_page_dump,
        side="hybrid",
    )
)
