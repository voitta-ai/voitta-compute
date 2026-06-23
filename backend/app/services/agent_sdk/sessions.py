"""List and read Claude Agent SDK sessions for the history dropdown.

The SDK's read APIs (``list_sessions`` / ``get_session_messages`` /
``get_session_info``) resolve their storage location from
``os.environ["CLAUDE_CONFIG_DIR"]`` and have no per-call override. To list a
*specific* user's sessions in server mode we set that env var around the call.
Because the process env is global, the set/read/restore is serialised under an
``asyncio.Lock`` and the (sync, disk-bound) SDK call runs in a worker thread.

This is correct on a single box. A load-balanced, multi-instance server should
instead back the brain with an external ``SessionStore`` so listing is explicit
rather than disk-local — out of scope here.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from typing import Any, Iterator

# Installed at runtime by app.installer — import defensively. The list/read
# routes guard on is_available() and try/except, so a missing SDK degrades to
# an empty session list rather than a crash.
try:
    from claude_agent_sdk import (
        get_session_info,
        get_session_messages,
        list_sessions,
    )
except ImportError:  # SDK not installed yet
    get_session_info = get_session_messages = list_sessions = None  # type: ignore

from app.services.agent_sdk.config import config_dir, workspace_dir

logger = logging.getLogger(__name__)

_env_lock = asyncio.Lock()


@contextlib.contextmanager
def _config_env(path: str) -> Iterator[None]:
    prior = os.environ.get("CLAUDE_CONFIG_DIR")
    os.environ["CLAUDE_CONFIG_DIR"] = path
    try:
        yield
    finally:
        if prior is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = prior


def _info_to_dict(info: Any) -> dict[str, Any]:
    return {
        "session_id": getattr(info, "session_id", None),
        "title": getattr(info, "custom_title", None) or getattr(info, "summary", None) or "",
        "summary": getattr(info, "summary", None),
        "first_prompt": getattr(info, "first_prompt", None),
        "last_modified": getattr(info, "last_modified", None),
        "created_at": getattr(info, "created_at", None),
        "tag": getattr(info, "tag", None),
        "git_branch": getattr(info, "git_branch", None),
    }


async def list_brain_sessions(limit: int = 100) -> list[dict[str, Any]]:
    """Sessions for the current user's pinned workspace, newest-first."""
    cfg = str(config_dir())
    cwd = str(workspace_dir())

    def _call() -> list[Any]:
        with _config_env(cfg):
            return list_sessions(directory=cwd, limit=limit)

    async with _env_lock:
        infos = await asyncio.to_thread(_call)
    out = [_info_to_dict(i) for i in infos]
    out.sort(key=lambda r: r.get("last_modified") or "", reverse=True)
    return out


async def get_brain_session_info(session_id: str) -> dict[str, Any] | None:
    cfg = str(config_dir())
    cwd = str(workspace_dir())

    def _call() -> Any:
        with _config_env(cfg):
            return get_session_info(session_id, directory=cwd)

    async with _env_lock:
        info = await asyncio.to_thread(_call)
    return _info_to_dict(info) if info is not None else None


def _message_to_transcript(sm: Any) -> dict[str, Any] | None:
    """Project one stored SessionMessage into a {role, text} transcript row.

    Only user/assistant text is surfaced for display; tool-call internals are
    omitted (the dropdown shows the conversation, not the full event log).
    """
    raw = getattr(sm, "message", None)
    if not isinstance(raw, dict):
        return None
    role = raw.get("role")
    if role not in ("user", "assistant"):
        return None
    content = raw.get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = "\n".join(
            str(b.get("text", "")) for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    else:
        text = ""
    text = text.strip()
    if not text:
        return None
    return {"role": role, "text": text}


async def get_brain_transcript(session_id: str, limit: int | None = None) -> list[dict[str, Any]]:
    """Display transcript (user/assistant text rows) for one session."""
    cfg = str(config_dir())
    cwd = str(workspace_dir())

    def _call() -> list[Any]:
        with _config_env(cfg):
            return get_session_messages(session_id, directory=cwd, limit=limit)

    async with _env_lock:
        msgs = await asyncio.to_thread(_call)
    rows: list[dict[str, Any]] = []
    for sm in msgs:
        row = _message_to_transcript(sm)
        if row is not None:
            rows.append(row)
    return rows
