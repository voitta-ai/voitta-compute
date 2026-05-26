"""Chainlit app: ``@cl.on_chat_start`` + ``@cl.on_message`` glue.

The agent loop lives in :mod:`app.agent`; this module is wiring only.
Importing it has side effects (cl decorator registration).
"""

from __future__ import annotations

import base64
import logging
from pathlib import Path
from typing import Any

import chainlit as cl

from app.agent import run_turn
from app.plugins import for_host, load_all
from app.services.llm.base import Message as LlmMessage
from app.settings import load as load_user_settings
from app.tools.registry import ToolCtx
import app.tools.load  # noqa: F401  — registers core tools FIRST

logger = logging.getLogger(__name__)

# Plugins are loaded AFTER core tools so plugin-contributed ToolSpecs
# can rely on the registry being initialised. Re-import (uvicorn
# --reload) picks up manifest edits without a manual restart.
load_all()


@cl.on_chat_start
async def on_chat_start() -> None:
    """Initialise per-session state.

    Phase 1: read provider/api_key/model from the user's settings.json
    once and stash on the session. The widget posts the page's host to
    ``cl.user_session`` via ``window_message`` so plugin selection can
    happen later in the conversation.
    """
    try:
        sid = cl.context.session.id  # type: ignore[attr-defined]
    except Exception:
        sid = "?"
    logger.info("on_chat_start: sid=%s", sid)

    user_settings = load_user_settings()
    provider = user_settings.get("provider", "anthropic")
    api_keys = user_settings.get("api_keys") or {}
    models = user_settings.get("models") or {}
    cl.user_session.set("messages", [])
    cl.user_session.set("provider", provider)
    cl.user_session.set("api_key", api_keys.get(provider))
    cl.user_session.set("model", models.get(provider))
    # host is set by on_window_message which can arrive before or after
    # on_chat_start — never overwrite it here.

    # Register with the per-session metadata registry that feeds the MCP
    # debugging endpoint. user_agent is pulled from the WSGI environ on
    # the underlying WebsocketSession.
    try:
        from app.services import cl_sessions
        ws = cl.context.session  # type: ignore[attr-defined]
        env = getattr(ws, "environ", None) or {}
        ua = env.get("HTTP_USER_AGENT") if isinstance(env, dict) else None
        cl_sessions.record_chat_start(ws.id, user_agent=ua)
    except Exception:
        pass

    if not api_keys.get(provider):
        await cl.Message(
            content="⚠️ No API key configured. Open ⚙ Settings to add one.",
        ).send()

    # Log connector + tool state at session start so we can debug
    # "why are MCP tools missing" from the log.
    try:
        import asyncio
        from app.services.mcp.registry import MCP_CONNECTORS, refresh_all
        from app.tools.registry import registry as _tool_registry
        for key, conn in MCP_CONNECTORS.items():
            logger.info(
                "on_chat_start: connector=%r status=%r owned=%r",
                key, conn.status, sorted(conn.owned_tool_names),
            )
        all_tools = list(_tool_registry._tools.keys())
        logger.info("on_chat_start: total registered tools=%d: %s", len(all_tools), all_tools)
        # Re-probe if connectors exist but none have synthesised tools.
        if MCP_CONNECTORS and not any(
            c.owned_tool_names for c in MCP_CONNECTORS.values()
        ):
            logger.info("on_chat_start: no MCP tools synthesised — triggering background refresh_all()")
            asyncio.create_task(refresh_all())
    except Exception:
        logger.exception("on_chat_start: MCP diagnostic failed")


@cl.on_window_message
async def on_window_message(message: str) -> None:
    """The bookmarklet posts ``key:value`` window-messages after mount.

    Recognised keys: ``host`` (drives plugin host-gating), ``url``,
    ``title``. Everything is forwarded to the session registry so the
    MCP debugging endpoint can show meaningful per-session metadata.
    """
    if not isinstance(message, str):
        return
    if message.startswith("host:"):
        host_val = message[len("host:"):].strip().lower() or None
        cl.user_session.set("host", host_val)
        try:
            sid = cl.context.session.id  # type: ignore[attr-defined]
        except Exception:
            sid = None
        logger.info("window_message: host=%r session=%s", host_val, sid)
    try:
        from app.services import cl_sessions
        cl_sessions.record_window_message(cl.context.session.id, message)
    except Exception:
        pass


@cl.on_message
async def on_message(user_msg: cl.Message) -> None:
    """One user turn → agent loop → streamed Chainlit primitives."""
    messages: list[LlmMessage] = cl.user_session.get("messages", [])
    snapshot = len(messages)

    # Build the user-turn content blocks: any attached image elements
    # prefix the text block (Anthropic's image-after-text vs image-before-text
    # is treated the same by the model; before keeps the upload visible
    # in transcripts).
    content: list[dict[str, Any]] = []
    for el in user_msg.elements or []:
        block = _element_to_image_block(el)
        if block is not None:
            content.append(block)
    content.append({"type": "text", "text": user_msg.content})

    messages.append(LlmMessage(role="user", content=content))

    # Re-read settings from disk on every turn so changes saved via the
    # in-pane SettingsView take effect without needing the user to reset
    # the session.
    fresh = load_user_settings()
    provider = fresh.get("provider", "anthropic")
    api_keys = fresh.get("api_keys") or {}
    models = fresh.get("models") or {}
    api_key = api_keys.get(provider)
    if not api_key:
        await cl.Message(
            content=f"⚠️ No API key for provider {provider!r}. Open ⚙ Settings to add one.",
        ).send()
        del messages[snapshot:]
        return
    cl.user_session.set("provider", provider)
    cl.user_session.set("api_key", api_key)
    cl.user_session.set("model", models.get(provider))

    # Compose the system prompt entirely from applicable plugins. The
    # default plugin (host_patterns: ["*"]) always contributes the base
    # Voitta prompt; host-specific plugins layer their addenda on top.
    host = cl.user_session.get("host")
    try:
        _sid = cl.context.session.id  # type: ignore[attr-defined]
    except Exception:
        _sid = None
    logger.info("on_message: host=%r session=%s", host, _sid)
    parts: list[str] = []
    for plugin in for_host(host):
        if plugin.system_prompt:
            parts.append(plugin.system_prompt.rstrip())
    system = "\n\n".join(parts)

    session_id: str | None
    try:
        session_id = cl.context.session.id  # type: ignore[attr-defined]
    except Exception:
        session_id = None
    ctx = ToolCtx(session_id=session_id, host=host)

    try:
        await run_turn(
            messages=messages,
            system=system,
            provider_id=provider,
            api_key=api_key,
            model=models.get(provider),
            ctx=ctx,
        )
        cl.user_session.set("messages", messages)
    except Exception as exc:  # surface to the user instead of crashing the socket
        logger.exception("on_message failed")
        del messages[snapshot:]
        cl.user_session.set("messages", messages)
        await cl.Message(content=f"⚠️ {type(exc).__name__}: {exc}").send()


# ----- helpers ------------------------------------------------------------


def _element_to_image_block(el: Any) -> dict[str, Any] | None:
    """Turn a Chainlit message element into an Anthropic image block.

    Only ``image/*`` mimes are supported here; everything else (PDFs,
    audio) is silently skipped for phase 1.
    """
    mime = getattr(el, "mime", None) or ""
    if not mime.startswith("image/"):
        return None
    path = getattr(el, "path", None)
    content = getattr(el, "content", None)

    data: bytes | None = None
    if path:
        try:
            data = Path(path).read_bytes()
        except Exception:
            logger.exception("could not read attachment at %s", path)
    elif isinstance(content, (bytes, bytearray)):
        data = bytes(content)
    elif isinstance(content, str):
        # Could be a data URI or already-encoded base64.
        if "," in content:
            content = content.split(",", 1)[1]
        try:
            data = base64.b64decode(content)
        except Exception:
            data = None
    if not data:
        return None

    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": mime,
            "data": base64.b64encode(data).decode("ascii"),
        },
    }
