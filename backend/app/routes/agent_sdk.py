"""HTTP surface for the Claude (subscription) brain's history dropdown.

* ``GET  /api/agent_sdk/sessions``               — list this user's sessions
* ``GET  /api/agent_sdk/sessions/{id}/messages`` — read one session's transcript
* ``POST /api/agent_sdk/select``                 — arm the next turn to resume a
                                                   session (or start fresh)
* ``POST /api/agent_sdk/disconnect``             — clear the stored token

Per-user isolation holds because the SDK paths and credential store resolve
under the current-user contextvar, which the HTTP auth guard sets per request.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter
from pydantic import BaseModel

from app.services.agent_sdk import (
    get_brain_transcript,
    list_brain_sessions,
)
from app.services.agent_sdk.config import is_available
from app.services.agent_sdk.credentials import clear_token, has_token
from app.services.agent_sdk.selection import set_pending
from app.services.current_user import get_current_email

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agent_sdk")


@router.get("/sessions")
async def list_sessions() -> dict:
    if not is_available():
        return {"available": False, "has_token": False, "sessions": []}
    try:
        sessions = await list_brain_sessions()
    except Exception:
        logger.exception("list_brain_sessions failed")
        sessions = []
    return {"available": True, "has_token": has_token(), "sessions": sessions}


@router.get("/sessions/{session_id}/messages")
async def session_messages(session_id: str) -> dict:
    try:
        messages = await get_brain_transcript(session_id)
    except Exception:
        logger.exception("get_brain_transcript failed for %s", session_id)
        messages = []
    return {"session_id": session_id, "messages": messages}


class SelectBody(BaseModel):
    # Resume this session id; or ``new=True`` to start a fresh conversation.
    session_id: str | None = None
    new: bool = False


@router.post("/select")
async def select(body: SelectBody) -> dict:
    email = get_current_email()
    target = None if body.new else (body.session_id or None)
    set_pending(email, target)
    return {"ok": True, "selected": target}


@router.post("/disconnect")
async def disconnect() -> dict:
    clear_token()
    return {"ok": True, "has_token": False}
