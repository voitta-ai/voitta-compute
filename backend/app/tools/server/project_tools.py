"""Project tools: durable memory + explicit cross-project reads.

``project_remember`` appends to the active project's PROJECT.md, which
is injected into the system prompt every turn — the mechanism that lets
knowledge accumulate across conversations instead of being re-derived.

``copy_from_project`` is the ONE sanctioned cross-project write: it
copies a script from another project into the active one (explicit
source, explicit action — same philosophy as the Google ``account``
arg: reads may name another scope, writes always land in the active
one).
"""

from __future__ import annotations

from typing import Any

from app.services import projects
from app.tools.registry import ToolCtx, ToolSpec, registry


def _project_roster() -> str:
    """Dynamic description suffix: the projects that exist right now."""
    try:
        items = projects.list_projects()
        active = projects.active_project()
    except Exception:
        return ""
    if len(items) < 2:
        return ""
    names = ", ".join(
        f"{p.slug}{' [active]' if p.slug == active else ''}" for p in items
    )
    return f"\n\nProjects: {names}."


# ---- project_remember -------------------------------------------------------


async def _remember(args: dict[str, Any], _ctx: ToolCtx) -> dict[str, Any]:
    text = (args.get("text") or "").strip()
    try:
        path = projects.append_memory(text)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return {
        "ok": True,
        "project": projects.active_project(),
        "path": str(path),
        "message": "Saved — this note is injected into every future "
                   "conversation in this project.",
    }


registry.register(ToolSpec(
    name="project_remember",
    description=(
        "Save a durable note to the active project's PROJECT.md. The "
        "note is injected into the system prompt of every future "
        "conversation in this project — use it for decisions, "
        "conventions, key file/spreadsheet IDs, and 'how this project "
        "works' facts the user would otherwise have to repeat. Keep "
        "notes short and factual; don't save transcript summaries."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "The fact to remember (one short paragraph).",
            },
        },
        "required": ["text"],
        "additionalProperties": False,
    },
    handler=_remember,
    side="server",
    dynamic_description=_project_roster,
))


# ---- copy_from_project --------------------------------------------------------


async def _copy_from_project(args: dict[str, Any], _ctx: ToolCtx) -> dict[str, Any]:
    source = (args.get("project") or "").strip()
    script = (args.get("script") or "").strip()
    new_name = (args.get("new_name") or "").strip() or script
    if not source or not script:
        return {"ok": False, "error": "`project` and `script` are required"}
    try:
        projects.get_project(source)
    except projects.UnknownProject as exc:
        return {"ok": False, "error": str(exc)}
    if source == projects.active_project():
        return {"ok": False, "error": "source project is already active — nothing to copy"}

    from app.reports import store
    from app.reports.slug import InvalidSlug, validate_slug

    try:
        validate_slug(new_name)
    except InvalidSlug as exc:
        return {"ok": False, "error": str(exc)}

    src_root = projects.project_dir(source) / "scripts"
    src_dir = src_root / script
    if not (src_dir / "code.py").is_file():
        # Foldered script in the source project?
        hits = list((src_root / "folders").glob(f"*/{script}/code.py")) if (
            src_root / "folders"
        ).is_dir() else []
        if not hits:
            return {
                "ok": False,
                "error": f"script {script!r} not found in project {source!r}",
            }
        src_dir = hits[0].parent

    if store.exists(new_name):
        return {
            "ok": False,
            "error": f"script {new_name!r} already exists in the active "
                     "project — pass new_name to copy under another name",
        }
    code = (src_dir / "code.py").read_text(encoding="utf-8")
    meta = store.write_script(new_name, code)
    # Carry the typed contract over (kind/effects/google pin) — the copy
    # writes the same places, so its gate state must come along.
    import json

    try:
        src_meta = json.loads((src_dir / "meta.json").read_text(encoding="utf-8"))
    except Exception:
        src_meta = {}
    patch = {
        k: v for k, v in src_meta.items()
        if k in ("kind", "effects", "google_account") and v
    }
    if patch:
        meta = store.update_meta(new_name, **patch)
    return {
        "ok": True,
        "copied_from": f"{source}/{script}",
        "name": meta.name,
        "kind": meta.kind,
        "effects": meta.effects,
        "note": "Copy is independent — edits here never touch the original.",
    }


registry.register(ToolSpec(
    name="copy_from_project",
    description=(
        "Copy a script from another project into the ACTIVE project "
        "(the one sanctioned cross-project write; the copy is fully "
        "independent afterwards). Use list_scripts with `project` to "
        "see what a source project has."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "project": {"type": "string", "description": "Source project slug."},
            "script": {"type": "string", "description": "Script name in the source project."},
            "new_name": {
                "type": "string",
                "description": "Name in the active project (default: same name).",
            },
        },
        "required": ["project", "script"],
        "additionalProperties": False,
    },
    handler=_copy_from_project,
    side="server",
    dynamic_description=_project_roster,
))
