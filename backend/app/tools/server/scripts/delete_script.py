"""``delete_script(name)`` — irreversible removal.

Idempotent: deleting a missing slug returns ``{ok: true, removed:
false}`` rather than an error. The LLM can call this defensively
without poll-then-delete.
"""

from __future__ import annotations

from typing import Any

from app.reports import store
from app.reports.slug import InvalidSlug, validate_slug
from app.tools.registry import ToolCtx, ToolSpec, registry


async def _handler(args: dict[str, Any], _ctx: ToolCtx) -> dict[str, Any]:
    name = args.get("name") or ""
    try:
        validate_slug(name)
    except InvalidSlug as exc:
        return {"ok": False, "error": str(exc)}
    removed = store.delete_script(name)
    return {"ok": True, "name": name, "removed": removed}


registry.register(
    ToolSpec(
        name="delete_script",
        description=(
            "Delete a saved script (idempotent — a missing slug is "
            "reported as removed=false but ok=true). Only for removing a "
            "report the user no longer wants. NOT an editing mechanism: "
            "to change or rewrite an existing script use edit_script — "
            "delete + define_script loses the script's history."
        ),
        input_schema={
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
            "additionalProperties": False,
        },
        side="server",
        handler=_handler,
    )
)
