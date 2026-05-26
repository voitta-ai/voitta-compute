"""GET /api/html-report — serves a cached HTML-report body.

The body lives in :mod:`app.reports.renderers.html`'s in-process LRU
cache, keyed by ``(slug, render_id)``. The FE iframe pulls from this
route; the renderer populates the cache before the FE is told to
load it.

Same origin as the rest of the backend so the screenshot shim inside
the served HTML can talk to ``/api/report-render-events`` and friends
without CORS preflights.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse

from app.reports.renderers.html import get_cached
from app.reports.slug import InvalidSlug, validate_slug

router = APIRouter(prefix="/api")


@router.get("/html-report", response_class=HTMLResponse)
async def get_html_report(id: str, render_id: str) -> HTMLResponse:
    try:
        validate_slug(id)
    except InvalidSlug as exc:
        raise HTTPException(400, str(exc)) from exc
    if not render_id or len(render_id) > 64:
        raise HTTPException(400, "bad render_id")
    body = get_cached(id, render_id)
    if body is None:
        raise HTTPException(
            404,
            f"no cached html-report for id={id!r} render_id={render_id!r} "
            f"— re-run the script",
        )
    return HTMLResponse(body)
