"""``delete_folder(name)`` — delete a workspace folder, orphaning its contents."""

from __future__ import annotations

from typing import Any

from app.tools.registry import ToolCtx, ToolSpec, registry


async def _handler(args: dict[str, Any], _ctx: ToolCtx) -> dict[str, Any]:
    name: str = args["name"]

    from app.services.python_storage import delete_folder as df_data
    from app.reports.store import delete_folder as df_scripts

    df_data(name)
    df_scripts(name)
    return {"ok": True, "name": name, "note": "Contents moved to root (not deleted)"}


registry.register(
    ToolSpec(
        name="delete_folder",
        description=(
            "Delete a workspace folder. Contents (snapshots and scripts) are\n"
            "moved back to root — they are NOT deleted.\n\n"
            "  name — folder name to delete\n\n"
            "If the folder doesn't exist, this is a no-op."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Folder name to delete"},
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        side="server",
        handler=_handler,
    )
)
