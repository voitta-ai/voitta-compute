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
from chainlit.types import ThreadDict

from app.agent import run_turn
from app.plugins import for_host, load_all
from app.services.llm.base import Message as LlmMessage
from app.settings import load as load_user_settings
from app.tools.registry import ToolCtx
import app.tools.load  # noqa: F401  — registers core tools FIRST

logger = logging.getLogger(__name__)


# ── No-auth single-user resume_thread patch ───────────────────────────────
# Chainlit's resume_thread requires session.user AND checks that the thread's
# userIdentifier matches session.user.identifier. In no-auth mode both fail.
# We replace resume_thread entirely with a version that skips user checks —
# safe for a single-user local deployment.

import json as _json
import chainlit.socket as _cl_socket

async def _patched_resume_thread(session):
    from chainlit.data import get_data_layer
    from chainlit.socket import user_sessions
    data_layer = get_data_layer()
    if not data_layer or not session.thread_id_to_resume:
        return
    thread = await data_layer.get_thread(thread_id=session.thread_id_to_resume)
    if not thread:
        logger.warning("resume_thread: thread %s not found", session.thread_id_to_resume)
        return
    logger.info("resume_thread: resuming thread %s userIdentifier=%s", thread.get("id"), thread.get("userIdentifier"))
    metadata = thread.get("metadata") or {}
    if isinstance(metadata, str):
        metadata = _json.loads(metadata)
    user_sessions[session.id] = metadata.copy()
    if chat_profile := metadata.get("chat_profile"):
        session.chat_profile = chat_profile
    if chat_settings := metadata.get("chat_settings"):
        session.chat_settings = chat_settings

    # assistant_message steps are stored with a parentId pointing to a run-step
    # UUID that is not present in the resumed thread. The react-client's e$()
    # function tries to nest them under the missing parent and silently drops
    # them. Clear parentId on assistant/user message steps so they render as
    # top-level messages.
    for step in thread.get("steps") or []:
        if step.get("type") in ("assistant_message", "user_message"):
            step["parentId"] = None

    return thread

_cl_socket.resume_thread = _patched_resume_thread

def patch_sio_connect():
    """Patch sio 'connect' and 'connection_successful' handlers.

    - connect: update thread_id_to_resume on restored sessions from auth payload
    - connection_successful: replace resume_thread call with our no-auth version
      (patching _cl_socket.resume_thread doesn't work because connection_successful
      calls it as a bare local name captured at definition time)

    Must be called AFTER mount_chainlit. Called from main.py.
    """
    import sys
    import asyncio
    import chainlit.socket as cl_socket

    # ── patch connect ──────────────────────────────────────────────────────
    orig_connect = cl_socket.sio.handlers.get("/", {}).get("connect")
    if not orig_connect:
        logger.error("patch_sio_connect: no connect handler found")
        return

    async def _patched_connect(sid, environ, auth):
        from chainlit.session import WebsocketSession
        result = await orig_connect(sid, environ, auth)
        thread_id = (auth.get("threadId") or None) if auth else None
        session = WebsocketSession.get(sid)
        if session and thread_id and thread_id != session.thread_id_to_resume:
            session.thread_id_to_resume = thread_id
            session.thread_id = thread_id
        # Capture the authenticated email from the handshake cookie (server
        # mode). Stashed on the session so per-user data routing works for
        # this connection's threads, steps, uploads, and tool file writes.
        # In desktop/dev the guard is off and this is None — no isolation.
        if session is not None:
            try:
                from app.services import login_auth
                session.voitta_email = login_auth.email_from_cookie_header(
                    (environ or {}).get("HTTP_COOKIE")
                )
            except Exception:
                session.voitta_email = None
        return result

    cl_socket.sio.handlers["/"]["connect"] = _patched_connect

    # ── patch connection_successful ────────────────────────────────────────
    # Replace the whole handler so we can use _patched_resume_thread instead
    # of the module-local resume_thread that ignores our monkey-patch.
    orig_conn_successful = cl_socket.sio.handlers.get("/", {}).get("connection_successful")
    if not orig_conn_successful:
        logger.error("patch_sio_connect: no connection_successful handler found")
        return

    async def _patched_connection_successful(sid):
        import traceback as _traceback
        from chainlit.context import init_ws_context
        from chainlit.config import config
        from chainlit.chat_context import chat_context
        from chainlit.message import Message

        try:
            context = init_ws_context(sid)
            await context.emitter.task_end()
            await context.emitter.clear("clear_ask")
            await context.emitter.clear("clear_call_fn")

            # Check thread_id_to_resume FIRST — a reconnect with a new threadId must
            # always resume, even if the session was restored (same cookie) and
            # has_first_interaction is still False from the previous connection.
            if context.session.thread_id_to_resume and config.code.on_chat_resume:
                thread = await _patched_resume_thread(context.session)
                if thread:
                    context.session.has_first_interaction = True
                    await context.emitter.emit(
                        "first_interaction",
                        {"interaction": "resume", "thread_id": thread.get("id")},
                    )
                    await config.code.on_chat_resume(thread)
                    for step in thread.get("steps", []):
                        if "message" in step["type"]:
                            chat_context.add(Message.from_dict(step))
                    await context.emitter.resume_thread(thread)
                    return
                else:
                    logger.warning("resume: thread %s not found", context.session.thread_id_to_resume)
                    await context.emitter.send_resume_thread_error("Thread not found.")

            # Restored session with no prior interaction: just ensure on_chat_start ran.
            if context.session.restored and not context.session.has_first_interaction:
                if config.code.on_chat_start and not context.session.chat_started:
                    context.session.chat_started = True
                    task = asyncio.create_task(config.code.on_chat_start())
                    context.session.current_task = task
                return

            if config.code.on_chat_start and not context.session.chat_started:
                context.session.chat_started = True
                task = asyncio.create_task(config.code.on_chat_start())
                context.session.current_task = task
        except Exception:
            logger.error("connection_successful failed for sid=%s:\n%s", sid, _traceback.format_exc())

    cl_socket.sio.handlers["/"]["connection_successful"] = _patched_connection_successful
    logger.info("patch_sio_connect: installed")


# ── Persistence ────────────────────────────────────────────────────────────

# Per-user data layers, keyed by the resolved user data root. Each user gets
# their own conversations.sqlite + uploads/ under their folder, so threads,
# steps, and attachments are isolated. Desktop/dev (no current user) resolves
# to the plain USER_DATA_ROOT, identical to the previous single-layer setup.
_data_layers: dict[str, Any] = {}


def _layer_for_root(root) -> Any:
    from app.data.local_storage import LocalStorageClient
    from app.data.sqlite_layer import SQLiteDataLayer

    key = str(root)
    layer = _data_layers.get(key)
    if layer is None:
        upload_dir = root / "uploads"
        db_path = str(root / "conversations.sqlite")
        storage = LocalStorageClient(upload_dir=upload_dir)
        layer = SQLiteDataLayer(db_path=db_path, storage_provider=storage)
        _data_layers[key] = layer
    return layer


class _RoutingDataLayer:
    """Chainlit data layer that delegates to a per-user backend chosen at call
    time from the current-user contextvar. Chainlit calls the factory once and
    caches this single object; every method access routes to the right user's
    SQLite + uploads via :func:`app.services.current_user.user_data_root`."""

    def __getattr__(self, name: str):
        from app.services.current_user import data_root_for_email, get_current_email
        # Prefer the request/turn contextvar; fall back to the email captured
        # on the socket session at connect, so data-layer calls made outside a
        # turn (resume, background persistence) still route to the right user.
        email = get_current_email()
        if not email:
            try:
                email = getattr(cl.context.session, "voitta_email", None)
            except Exception:
                email = None
        return getattr(_layer_for_root(data_root_for_email(email)), name)


@cl.data_layer
def data_layer():
    """Return the routing data layer (created once, reused)."""
    return _RoutingDataLayer()

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


@cl.on_chat_resume
async def on_chat_resume(thread: ThreadDict) -> None:
    """Restore session state when the user reopens an existing thread.

    The UI already shows the stored steps from the database.  Here we
    reconstruct the in-memory LLM message list so the agent has the
    conversation context for the next user turn.
    """
    user_settings = load_user_settings()
    provider = user_settings.get("provider", "anthropic")
    api_keys = user_settings.get("api_keys") or {}
    models = user_settings.get("models") or {}
    cl.user_session.set("provider", provider)
    cl.user_session.set("api_key", api_keys.get(provider))
    cl.user_session.set("model", models.get(provider))

    messages: list[LlmMessage] = []
    for step in thread.get("steps") or []:
        step_type = step.get("type")
        output = (step.get("output") or "").strip()
        if not output:
            continue
        if step_type == "user_message":
            messages.append(
                LlmMessage(role="user", content=[{"type": "text", "text": output}])
            )
        elif step_type == "assistant_message":
            messages.append(
                LlmMessage(role="assistant", content=[{"type": "text", "text": output}])
            )

    cl.user_session.set("messages", messages)
    logger.info(
        "on_chat_resume: thread=%s restored %d messages", thread.get("id"), len(messages)
    )


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

    # Authenticated email captured at socket connect (server mode; None on
    # desktop/dev). Set it on the contextvar for the whole turn so per-user
    # data paths (python_storage, scripts, uploads) resolve under this user's
    # folder — including inside the compute sandbox, since asyncio.to_thread
    # copies the context into the worker thread.
    from app.services.current_user import set_current_email, reset_current_email
    try:
        email = getattr(cl.context.session, "voitta_email", None)
    except Exception:
        email = None
    _email_token = set_current_email(email)

    ctx = ToolCtx(session_id=session_id, host=host, email=email)

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
    finally:
        reset_current_email(_email_token)


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
