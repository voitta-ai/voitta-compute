"""``run_script(name, args?, wait_s?)`` — execute and dispatch.

The dispatcher figures out where output goes:
* renderable → ``call_fn`` to the FE, await render-event
* inline-only → ``cl.Message`` blocks in the current turn
* pure-compute → JSON in the tool result

Returns ``{ok, status, kind?, elapsed_ms, inventory?, errors?}``. The
``status`` field is what the model checks:
* ``"ready"`` — renderable mounted cleanly
* ``"no-render"`` — script produced inline only, all good
* ``"errored"`` — pane mounted but the FE reported a runtime error
* ``"timeout"`` — pane didn't acknowledge in time
* ``"error"``  — script failed before producing output
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from app.reports import dispatch
from app.reports.slug import InvalidSlug, validate_slug
from app.tools.registry import ToolCtx, ToolSpec, registry


async def _handler(args: dict[str, Any], _ctx: ToolCtx) -> dict[str, Any]:
    name = args.get("name") or ""
    script_args = args.get("args") or {}
    wait_s = float(args.get("wait_s") or 8.0)
    try:
        validate_slug(name)
    except InvalidSlug as exc:
        return {"ok": False, "status": "error", "error": str(exc)}
    if not isinstance(script_args, dict):
        return {
            "ok": False,
            "status": "error",
            "error": "`args` must be an object (or omitted)",
        }
    # Forward the page host to the script ctx so ``ctx.theme()`` /
    # ``ctx.get_theme()`` / ``ctx.apply_theme(layout)`` default to the
    # current plugin's palette without the LLM having to pass a host
    # arg explicitly. ``ctx.host`` comes from ``@cl.on_window_message``.
    result = await dispatch.run_and_dispatch(
        name,
        args=script_args,
        title=args.get("title"),
        wait_s=wait_s,
        host=_ctx.host,
    )
    return asdict(result)


registry.register(
    ToolSpec(
        name="run_script",
        description=(
            "Execute a saved script. The result is dispatched: a "
            "matplotlib/plotly figure mounts in the report pane; "
            "ctx.text/image/json emissions land inline in the chat; "
            "a plain return value lands in this tool's result. "
            "Status='ready' on a clean render; 'no-render' when the "
            "script only emitted inline content; 'errored'/'timeout' "
            "when the pane failed."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "args": {
                    "type": "object",
                    "description": "Forwarded to ctx.args inside the script",
                    "additionalProperties": True,
                },
                "title": {"type": "string"},
                "wait_s": {
                    "type": "number",
                    "description": "Render-event timeout (default 8s)",
                },
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        side="server",
        handler=_handler,
    )
)
