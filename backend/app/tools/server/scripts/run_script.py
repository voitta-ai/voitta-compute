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

from app.reports import dispatch, script_typing, store
from app.reports.slug import InvalidSlug, validate_slug
from app.tools.registry import ToolCtx, ToolSpec, registry


def _confirm_gate(name: str, ctx: ToolCtx) -> str | None:
    """Return a needs-confirmation message when running ``name`` requires
    an explicit confirm, else None.

    Gate iff the script demonstrably writes to Google Sheets (sticky
    ``effects.writes_external``), OR it's an unclassified legacy script
    whose source mentions ``ctx.sheets`` (static check — until its first
    typed run records real effects, presence of the client is the only
    honest signal). Exemption: the script was defined/edited in this
    session within the last few minutes — the user just watched it being
    authored. The gate is deliberately soft (the model can pass
    confirm=true); its job is to force the ask-the-user round-trip, same
    trust model as sheets_write_range's confirm convention.
    """
    if script_typing.was_recently_edited(ctx.session_id, name):
        return None
    meta = store.read_meta(name)
    account = meta.extra.get("google_account") or "the default Google account"
    if (meta.effects or {}).get("writes_external"):
        return (
            f"script {name!r} writes to Google Sheets (as {account}). "
            "Re-running repeats the write — appends duplicate, overwrites "
            "clobber. Ask the user to confirm, then re-call with "
            "confirm: true."
        )
    if meta.kind is None:
        try:
            code = store.read_code(name)
        except Exception:
            return None
        if "ctx.sheets" in code:
            return (
                f"script {name!r} predates effect tracking and its source "
                "uses ctx.sheets — it MAY write to Google Sheets (as "
                f"{account}). Ask the user to confirm this first run, then "
                "re-call with confirm: true; the run will record its actual "
                "effects and read-only scripts won't be gated again."
            )
    return None


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
    if not args.get("confirm") and store.exists(name):
        gate_msg = _confirm_gate(name, _ctx)
        if gate_msg is not None:
            return {
                "ok": False,
                "status": "needs-confirmation",
                "error": gate_msg,
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
            "when the pane failed.\n"
            "\n"
            "Status='needs-confirmation': the script writes to Google "
            "Sheets (or is a legacy script that may) — re-running repeats "
            "the write. ASK THE USER before re-calling with confirm: true; "
            "never set confirm on your own initiative. Scripts you just "
            "defined/edited this session run without the confirm."
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
                "confirm": {
                    "type": "boolean",
                    "description": (
                        "Required true to run a script whose recorded "
                        "effects include external writes (Google Sheets). "
                        "Only pass after the user explicitly confirmed."
                    ),
                },
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        side="server",
        handler=_handler,
    )
)
