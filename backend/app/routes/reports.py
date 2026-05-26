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

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.reports import render_events
from app.reports.render_events import RenderEvent
from app.reports.slug import InvalidSlug, validate_slug

router = APIRouter(prefix="/api")


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
