"""``define_script(name, code)`` — author a new script.

Smoke-tests ``build(ctx)`` before persisting. On smoke-test failure no
files land on disk; the traceback is returned so the LLM can fix and
retry without leaking half-written state.
"""

from __future__ import annotations

from typing import Any

from app.reports import sandbox, store
from app.reports.slug import InvalidSlug, validate_slug
from app.tools.registry import ToolCtx, ToolSpec, registry


async def _handler(args: dict[str, Any], _ctx: ToolCtx) -> dict[str, Any]:
    name = args.get("name") or ""
    code = args.get("code") or ""
    folder_name: str | None = args.get("folder_name") or None
    if not isinstance(code, str) or not code.strip():
        return {"ok": False, "error": "`code` must be a non-empty string"}
    try:
        validate_slug(name)
    except InvalidSlug as exc:
        return {"ok": False, "error": str(exc)}
    if store.exists(name):
        return {
            "ok": False,
            "error": (
                f"script {name!r} already exists — use edit_script to "
                "modify or delete_script first"
            ),
        }
    result = sandbox.smoke_test(name, code)
    if not result.ok:
        return {"ok": False, "error": result.error, "traceback": result.traceback}
    try:
        meta = store.write_script(name, code, folder_name=folder_name)
    except ValueError as exc:
        return {
            "ok": False,
            "error": str(exc),
            "hint": (
                f"Use create_folder(name={folder_name!r}) first, "
                "then retry define_script."
            ) if folder_name else None,
        }
    return {
        "ok": True,
        "name": meta.name,
        "folder_name": meta.folder_name,
        "created_at": meta.created_at,
        "smoke": {"log_lines": result.ctx.log_lines if result.ctx else []},
    }


registry.register(
    ToolSpec(
        name="define_script",
        description=(
            "Create a new script under scripts/<name>/code.py. The script "
            "must define `build(ctx)`. Returns ok=true only if `build` "
            "executes cleanly during a smoke-test; otherwise nothing is "
            "written and the traceback is returned."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "slug: lowercase letters, digits, underscore, hyphen (1..64)",
                },
                "code": {
                    "type": "string",
                    "description": "full Python source of the script",
                },
                "folder_name": {
                    "type": "string",
                    "description": "Workspace folder to place the script in. Create with create_folder first.",
                },
            },
            "required": ["name", "code"],
            "additionalProperties": False,

        },
        side="server",
        handler=_handler,
    )
)
