"""``list_scripts(project?)`` — list saved scripts.

Returns minimal metadata. Source is not included (use ``get_script``).
Default scope is the ACTIVE project; pass ``project`` to read another
project's list explicitly (reads may cross projects, writes never do —
use ``copy_from_project`` to bring a script over).
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from app.reports import store
from app.services import projects
from app.tools.registry import ToolCtx, ToolSpec, registry


async def _handler(args: dict[str, Any], _ctx: ToolCtx) -> dict[str, Any]:
    source = (args.get("project") or "").strip() or None
    if source and source != projects.active_project():
        try:
            projects.get_project(source)
        except projects.UnknownProject as exc:
            return {"ok": False, "error": str(exc)}
        metas = store.list_scripts(root=projects.project_dir(source) / "scripts")
        scope = source
    else:
        metas = store.list_scripts()
        scope = projects.active_project()
    return {
        "ok": True,
        "project": scope,
        "count": len(metas),
        "scripts": [asdict(m) for m in metas],
    }


registry.register(
    ToolSpec(
        name="list_scripts",
        description=(
            "List every saved script in the active project with its "
            "metadata (kind, effects, google_account pin, run history). "
            "Pass `project` to list another project's scripts instead "
            "(read-only — use copy_from_project to bring one over)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "project": {
                    "type": "string",
                    "description": "Another project's slug to list instead of the active one.",
                },
            },
            "additionalProperties": False,
        },
        side="server",
        handler=_handler,
    )
)
