"""``get_script(name)`` — return source + meta (+ optional bundle).

Phase 1 returns just source + meta. R2 will extend to include the
last-run inventory + recent errors (the "debug bundle" option discussed
in the plan) — that's an additive change to the return shape, so
clients won't break.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from app.reports import store
from app.reports.slug import InvalidSlug, validate_slug
from app.tools.registry import ToolCtx, ToolSpec, registry


async def _handler(args: dict[str, Any], _ctx: ToolCtx) -> dict[str, Any]:
    name = args.get("name") or ""
    source = (args.get("project") or "").strip() or None
    try:
        validate_slug(name)
    except InvalidSlug as exc:
        return {"ok": False, "error": str(exc)}

    if source:
        # Explicit cross-project READ (writes never take a project arg).
        from app.services import projects

        try:
            projects.get_project(source)
        except projects.UnknownProject as exc:
            return {"ok": False, "error": str(exc)}
        src_root = projects.project_dir(source) / "scripts"
        src_dir = src_root / name
        if not (src_dir / "code.py").is_file():
            hits = list((src_root / "folders").glob(f"*/{name}/code.py")) if (
                src_root / "folders"
            ).is_dir() else []
            if not hits:
                return {
                    "ok": False,
                    "error": f"script {name!r} not found in project {source!r}",
                }
            src_dir = hits[0].parent
        return {
            "ok": True,
            "project": source,
            "name": name,
            "code": (src_dir / "code.py").read_text(encoding="utf-8"),
            "meta": asdict(store.read_meta(name, root=src_dir.parent)),
        }

    if not store.exists(name):
        return {"ok": False, "error": f"script {name!r} does not exist"}
    return {
        "ok": True,
        "name": name,
        "code": store.read_code(name),
        "meta": asdict(store.read_meta(name)),
    }


registry.register(
    ToolSpec(
        name="get_script",
        description=(
            "Return the source code and metadata for a saved script. "
            "Pass `project` to read a script from another project "
            "(read-only — use copy_from_project to bring it over)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "project": {
                    "type": "string",
                    "description": "Another project's slug to read from instead of the active one.",
                },
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        side="server",
        handler=_handler,
    )
)
