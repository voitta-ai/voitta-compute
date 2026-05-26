"""``move_to_folder(id, folder_name?)`` — move a snapshot or script into a folder."""

from __future__ import annotations

from typing import Any

from app.tools.registry import ToolCtx, ToolSpec, registry


async def _handler(args: dict[str, Any], _ctx: ToolCtx) -> dict[str, Any]:
    item_id: str = args["id"]
    folder_name: str | None = args.get("folder_name")  # None = move to root

    # Determine if this is a data handle (py_xxxxxxxx) or a script slug
    from app.services.python_storage import move_to_folder as data_move, get as ps_get
    from app.reports.store import move_script_to_folder, exists as script_exists

    is_handle = item_id.startswith("py_") and ps_get(item_id) is not None
    is_slug = not is_handle and script_exists(item_id)

    if not is_handle and not is_slug:
        return {"ok": False, "error": f"No snapshot or script found with id {item_id!r}"}

    try:
        if is_handle:
            ok = data_move(item_id, folder_name)
        else:
            ok = move_script_to_folder(item_id, folder_name)
    except ValueError as e:
        return {"ok": False, "error": str(e)}

    if not ok:
        return {"ok": False, "error": f"Item {item_id!r} not found"}

    dest = f"folder '{folder_name}'" if folder_name else "root (no folder)"
    return {"ok": True, "id": item_id, "moved_to": dest}


registry.register(
    ToolSpec(
        name="move_to_folder",
        description=(
            "Move a data snapshot or script into a folder (or back to root).\n\n"
            "  id           — snapshot handle (e.g. 'py_71e1c5b2') or script slug\n"
            "  folder_name  — target folder name; omit or pass null to move to root\n\n"
            "The folder must already exist — call create_folder first if needed.\n"
            "Use list_data or list_scripts to discover available ids."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Snapshot handle or script slug"},
                "folder_name": {"type": "string", "description": "Destination folder name. Omit to move to root."},
            },
            "required": ["id"],
            "additionalProperties": False,
        },
        side="server",
        handler=_handler,
    )
)
