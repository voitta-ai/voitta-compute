"""Project management routes — backing the chat-header project chip.

* ``GET    /api/projects``            — list + active slug
* ``POST   /api/projects``            — create {name}; does NOT switch
* ``POST   /api/projects/active``     — switch {slug}
* ``PATCH  /api/projects/{slug}``     — rename {name} (display name only)
* ``DELETE /api/projects/{slug}``     — archive into legacy/_archived/ (never destroys)

Server mode: these run under the auth guard, so all paths resolve in
the requesting user's tree automatically.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from app.services import projects
from app.services.projects import UnknownProject

router = APIRouter(prefix="/api/projects")


def _listing() -> dict:
    return {
        "ok": True,
        "active": projects.active_project(),
        "projects": [
            {
                "slug": p.slug,
                "name": p.name,
                "created_at": p.created_at,
                "is_legacy": p.is_legacy,
            }
            for p in projects.list_projects()
        ],
    }


@router.get("")
async def list_projects() -> dict:
    return _listing()


async def _json_body(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be an object")
    return body


@router.post("")
async def create_project(request: Request) -> dict:
    body = await _json_body(request)
    try:
        p = projects.create_project(str(body.get("name") or ""))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"created": p.slug, **_listing()}


@router.post("/active")
async def switch_project(request: Request) -> dict:
    body = await _json_body(request)
    try:
        projects.set_active_project(str(body.get("slug") or ""))
    except UnknownProject as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return _listing()


@router.patch("/{slug}")
async def rename_project(slug: str, request: Request) -> dict:
    body = await _json_body(request)
    try:
        projects.rename_project(slug, str(body.get("name") or ""))
    except UnknownProject as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _listing()


@router.delete("/{slug}")
async def delete_project(slug: str) -> dict:
    try:
        projects.delete_project(slug)
    except UnknownProject as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _listing()
