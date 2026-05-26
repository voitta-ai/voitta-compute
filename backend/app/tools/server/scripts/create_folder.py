"""``create_folder(name, kind?, description?)`` — create a workspace folder."""

from __future__ import annotations

from typing import Any

from app.tools.registry import ToolCtx, ToolSpec, registry


async def _handler(args: dict[str, Any], _ctx: ToolCtx) -> dict[str, Any]:
    name: str = args["name"]
    kind: str = args.get("kind", "both")
    description: str = args.get("description", "")

    from app.services.python_storage import create_folder as cf_data
    from app.reports.store import create_folder as cf_scripts

    created = []
    errors = []

    if kind in ("data", "both"):
        try:
            cf_data(name, description=description)
            created.append("data")
        except FileExistsError:
            pass
        except ValueError as e:
            errors.append(str(e))

    if kind in ("scripts", "both"):
        try:
            cf_scripts(name, description=description)
            created.append("scripts")
        except FileExistsError:
            pass
        except ValueError as e:
            errors.append(str(e))

    if errors:
        return {"ok": False, "error": "; ".join(errors)}
    return {"ok": True, "name": name, "created": created}


registry.register(
    ToolSpec(
        name="create_folder",
        description=(
            "Create a named folder in the Workspace for organising data snapshots\n"
            "and/or scripts.\n\n"
            "  name        — folder name: [a-z0-9_-], max 64 chars\n"
            "  kind        — 'data', 'scripts', or 'both' (default: 'both')\n"
            "  description — optional human-readable label\n\n"
            "Creating a folder that already exists is a no-op (not an error).\n"
            "Use move_to_folder to assign items after creation."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Folder name [a-z0-9_-], max 64 chars"},
                "kind": {"type": "string", "enum": ["data", "scripts", "both"], "description": "Which domain to create the folder in (default: both)"},
                "description": {"type": "string", "description": "Optional description"},
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        side="server",
        handler=_handler,
    )
)
