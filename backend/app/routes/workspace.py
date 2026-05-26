"""Workspace browser API — scripts + data snapshots + folders.

GET  /api/workspace                              → merged list with folders
GET  /api/workspace/active                       → currently mounted report tab
POST /api/workspace/active                       → FE reports which tab is visible
GET  /api/workspace/data/{handle}/files/{name}  → serve a file from a snapshot
POST /api/workspace/folders                      → create folder
DELETE /api/workspace/folders/{name}             → delete folder (orphans contents)
PATCH /api/workspace/data/{handle}               → {folder_name: str|null}
PATCH /api/workspace/scripts/{slug}              → {folder_name: str|null}
DELETE /api/workspace/scripts/{slug}
DELETE /api/workspace/data/{handle}
POST   /api/workspace/scripts/{slug}/run
"""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.reports.slug import InvalidSlug, validate_slug

router = APIRouter(prefix="/api/workspace")

# In-memory store for the currently active report tab (last reported by FE)
_active_tab: dict[str, Any] = {}


# ─── active tab ──────────────────────────────────────────────────────────────

class ActiveTabPayload(BaseModel):
    tab: str | None = None
    name: str | None = None
    title: str | None = None


@router.get("/active")
async def get_active_tab() -> dict:
    return {"ok": True, **_active_tab}


@router.post("/active")
async def set_active_tab(payload: ActiveTabPayload) -> dict:
    _active_tab.clear()
    _active_tab.update({"tab": payload.tab, "name": payload.name, "title": payload.title})
    return {"ok": True}


# ─── item serialisers ─────────────────────────────────────────────────────────

def _script_item(meta) -> dict:
    slug = meta.name
    label = slug.replace("-", " ").replace("_", " ")
    return {
        "kind": "script",
        "id": slug,
        "name": label,
        "slug": slug,
        "last_run_at": meta.last_run_at,
        "last_ok": meta.last_ok,
        "last_kind": meta.last_kind,
        "title": getattr(meta, "title", None) or label,
        "folder_name": getattr(meta, "folder_name", None),
    }


def _data_item(snap: dict) -> dict:
    meta = snap.get("meta") or {}
    files = meta.get("files") or []
    origin = meta.get("origin") or {}
    extra = origin.get("extra") or {}
    total_bytes = sum(f.get("bytes", 0) for f in files) if files else 0
    return {
        "kind": "data",
        "id": snap["handle"],
        "name": meta.get("label") or meta.get("original_name") or snap["handle"],
        "handle": snap["handle"],
        "bytes": total_bytes,
        "file_count": len(files),
        "files": [
            {"name": f.get("name", "?"), "bytes": f.get("bytes", 0), "variant": f.get("variant")}
            for f in files
        ],
        "created_at": meta.get("created_at"),
        "source": origin.get("source") or meta.get("source"),
        "asset": extra.get("asset"),
        "data_kind": meta.get("kind"),
        "corrupt": snap.get("corrupt", False),
        "folder_name": snap.get("folder_name"),
    }


# ─── list ─────────────────────────────────────────────────────────────────────

@router.get("")
async def list_workspace() -> dict:
    from app.reports.store import list_folders as ls_folders, list_scripts
    from app.services.python_storage import list_all, list_folders as ld_folders

    scripts = [_script_item(m) for m in list_scripts()]
    data = [_data_item(s) for s in list_all()]

    # Merge folder metadata from both domains
    data_folders = {f["name"]: f for f in ld_folders()}
    script_folders = {f["name"]: f for f in ls_folders()}
    all_names = sorted(set(data_folders) | set(script_folders))
    folders = []
    for name in all_names:
        df = data_folders.get(name, {})
        sf = script_folders.get(name, {})
        folders.append({
            "name": name,
            "description": df.get("description") or sf.get("description") or "",
            "color": df.get("color") or sf.get("color") or "",
            "created_at": df.get("created_at") or sf.get("created_at"),
            "data_count": df.get("snapshot_count", 0),
            "script_count": sf.get("script_count", 0),
        })

    return {"scripts": scripts, "data": data, "folders": folders}


# ─── folders ──────────────────────────────────────────────────────────────────

class FolderPayload(BaseModel):
    name: str
    description: str = ""
    color: str = ""


@router.post("/folders")
async def create_folder(payload: FolderPayload) -> dict:
    from app.services.python_storage import create_folder as cf_data
    from app.reports.store import create_folder as cf_scripts
    try:
        cf_data(payload.name, description=payload.description, color=payload.color)
    except FileExistsError:
        pass  # already exists in data side — fine
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    try:
        cf_scripts(payload.name, description=payload.description, color=payload.color)
    except FileExistsError:
        pass
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "name": payload.name}


@router.delete("/folders/{name}")
async def delete_folder(name: str) -> dict:
    from app.services.python_storage import delete_folder as df_data
    from app.reports.store import delete_folder as df_scripts
    df_data(name)
    df_scripts(name)
    return {"ok": True}


# ─── move items ───────────────────────────────────────────────────────────────

class MovePayload(BaseModel):
    folder_name: str | None = None


@router.patch("/data/{handle}")
async def move_data(handle: str, payload: MovePayload) -> dict:
    from app.services.python_storage import move_to_folder
    try:
        ok = move_to_folder(handle, payload.folder_name)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not ok:
        raise HTTPException(404, f"snapshot {handle!r} not found")
    return {"ok": True}


@router.patch("/scripts/{slug}")
async def move_script(slug: str, payload: MovePayload) -> dict:
    from app.reports.store import move_script_to_folder
    try:
        ok = move_script_to_folder(slug, payload.folder_name)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not ok:
        raise HTTPException(404, f"script {slug!r} not found")
    return {"ok": True}


# ─── file serving ─────────────────────────────────────────────────────────────

@router.get("/data/{handle}/files/{filename}")
async def serve_snapshot_file(handle: str, filename: str) -> FileResponse:
    from app.services.python_storage import get as ps_get
    rec = ps_get(handle)
    if rec is None:
        raise HTTPException(404, f"snapshot {handle!r} not found")
    snap_dir = Path(rec["path"])
    try:
        resolved = (snap_dir / filename).resolve()
        resolved.relative_to(snap_dir.resolve())
    except (ValueError, OSError):
        raise HTTPException(400, "invalid filename")
    if not resolved.is_file():
        raise HTTPException(404, f"file {filename!r} not found in snapshot")
    mime, _ = mimetypes.guess_type(filename)
    return FileResponse(resolved, media_type=mime or "application/octet-stream")


# ─── delete ───────────────────────────────────────────────────────────────────

@router.delete("/scripts/{slug}")
async def delete_script(slug: str) -> dict:
    try:
        validate_slug(slug)
    except InvalidSlug as exc:
        raise HTTPException(400, str(exc)) from exc
    from app.reports.store import delete_script as _del
    if not _del(slug):
        raise HTTPException(404, f"script {slug!r} not found")
    return {"ok": True}


@router.delete("/data/{handle}")
async def delete_snapshot(handle: str) -> dict:
    from app.services.python_storage import delete
    if not delete(handle):
        raise HTTPException(404, f"snapshot {handle!r} not found")
    return {"ok": True}


# ─── run script ───────────────────────────────────────────────────────────────

@router.post("/scripts/{slug}/run")
async def run_script(slug: str) -> dict:
    try:
        validate_slug(slug)
    except InvalidSlug as exc:
        raise HTTPException(400, str(exc)) from exc
    from app.reports.dispatch import run_and_dispatch
    result = await run_and_dispatch(slug)
    if not result.ok:
        raise HTTPException(500, result.error or result.status)
    resp: dict = {"ok": True, "status": result.status}
    if result.inventory:
        resp.update(result.inventory)
    return resp
