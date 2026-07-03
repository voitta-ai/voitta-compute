"""Render-event drain: FE POSTs render lifecycle signals here.

The FE calls this endpoint when:
* a report has mounted and rendered cleanly (``kind="ready"``)
* a render-time error happened (``kind="error"``)
* it has an inventory snapshot to share (``kind="inventory"``)

The route writes through to :mod:`app.reports.render_events` which is
in-process state; the ``run_script`` dispatcher awaits ``wait_for()``
on the matching slug.
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.reports import render_events
from app.reports.render_events import RenderEvent
from app.reports.slug import InvalidSlug, validate_slug
from app.services.report_pdf import ReportPdfError, render_report_pdf

router = APIRouter(prefix="/api")


def _safe_filename(raw: str | None, fallback: str) -> str:
    """Sanitise a user/title string into a bare PDF filename stem."""
    stem = (raw or "").strip() or fallback
    stem = re.sub(r"[^\w.\- ]+", "", stem).strip() or fallback
    return f"{stem[:80]}.pdf"


class RenderEventIn(BaseModel):
    name: str = Field(..., description="script slug")
    kind: str
    render_id: str = ""
    message: str = ""
    detail: dict[str, Any] = Field(default_factory=dict)
    inventory: dict[str, Any] | None = None


@router.post("/report-render-events")
async def post_render_event(payload: RenderEventIn) -> dict:
    try:
        validate_slug(payload.name)
    except InvalidSlug as exc:
        raise HTTPException(400, str(exc)) from exc
    if payload.kind not in {"ready", "error", "inventory", "info"}:
        raise HTTPException(400, f"unknown kind {payload.kind!r}")

    if payload.inventory is not None:
        render_events.record_inventory(payload.name, payload.inventory)

    render_events.record(
        RenderEvent(
            slug=payload.name,
            kind=payload.kind,
            render_id=payload.render_id,
            message=payload.message,
            detail=payload.detail,
        )
    )
    return {"ok": True}


@router.get("/report/export-pdf")
async def export_report_pdf(id: str, render_id: str, title: str | None = None) -> Response:
    """Render the cached report ``(id, render_id)`` to a text-based PDF download.

    A pure-Python HTML→PDF engine (xhtml2pdf) converts the report's HTML, so the
    text stays selectable/copy-pasteable. It doesn't run JavaScript — text,
    tables, CSS, and images render; JS-drawn charts won't. ``title`` only names
    the file.
    """
    try:
        validate_slug(id)
    except InvalidSlug as exc:
        raise HTTPException(400, str(exc)) from exc
    if not render_id or len(render_id) > 64:
        raise HTTPException(400, "bad render_id")

    try:
        pdf = await render_report_pdf(id, render_id)
    except ReportPdfError as exc:
        # 503: the report exists but the export engine couldn't produce it
        # (engine not installed yet, or a conversion failure) — a retry may work.
        raise HTTPException(503, str(exc)) from exc

    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{_safe_filename(title, id)}"'
        },
    )
