"""Bridge the in-process tool registry to the Claude Agent SDK.

The Agent SDK brain runs the *same tool suite* as the main agent loop. We
expose every registry tool the current host can see as an in-process SDK MCP
tool (``create_sdk_mcp_server`` + ``@tool``); the engine calls them by the
name ``mcp__voitta__<tool>`` and the call executes in *this* process — so the
live Chainlit context (needed by hybrid/browser tools) is available, exactly
as in :func:`app.agent.run_turn`.

The server is rebuilt per turn because the :class:`ToolCtx` (session/host/email)
is request-scoped; construction is cheap (no IPC, no subprocess).
"""

from __future__ import annotations

import json
import logging
from typing import Any

# Installed at runtime by app.installer — import defensively so app boot never
# depends on it. Only build_tool_server dereferences these, and it's reached
# only after run_agent_sdk_turn's ``query is None`` guard (SDK present).
try:
    from claude_agent_sdk import create_sdk_mcp_server, tool
except ImportError:  # SDK not installed yet
    create_sdk_mcp_server = None  # type: ignore
    tool = None  # type: ignore

from app.services.agent_sdk.config import MCP_SERVER_NAME
from app.tools.registry import ToolCtx, registry

logger = logging.getLogger(__name__)

# Sentinel keys the orchestrator strips before the model sees a tool result;
# we strip them here too and (for inline image forms) re-emit as MCP image
# content blocks. See app.services.llm.base for the convention.
_IMAGE_SENTINELS = ("_image", "_images", "_images_chat_only", "_images_stash")


def _result_to_mcp_content(payload: Any) -> list[dict[str, Any]]:
    """Convert a registry tool result into MCP content blocks.

    Inline image sentinels (``_image`` / ``_images``) become MCP ``image``
    blocks so screenshot-style tools remain visual under this brain. The
    stash-backed form (``_images_stash``) is summarised as text — full-size
    inlining via the BE stash is a main-loop concern and not reproduced here.
    """
    if not isinstance(payload, dict):
        return [{"type": "text", "text": str(payload)}]

    images: list[dict[str, Any]] = []
    rest = dict(payload)

    single = rest.pop("_image", None)
    if isinstance(single, dict) and isinstance(single.get("data"), str):
        images.append(single)
    for key in ("_images", "_images_chat_only"):
        many = rest.pop(key, None)
        if isinstance(many, list):
            images.extend(i for i in many if isinstance(i, dict) and isinstance(i.get("data"), str))
    stash = rest.pop("_images_stash", None)
    if isinstance(stash, list) and stash:
        rest["_images_note"] = f"{len(stash)} image(s) captured (not inlined under this brain)"

    blocks: list[dict[str, Any]] = [
        {"type": "text", "text": json.dumps(rest, ensure_ascii=False, default=str)}
    ]
    for img in images:
        mime = img.get("media_type") or img.get("mimeType") or "image/png"
        blocks.append({"type": "image", "data": img["data"], "mimeType": mime})
    return blocks


def _make_tool(spec, ctx: ToolCtx):
    """Wrap one registry ToolSpec as an SDK MCP tool bound to ``ctx``."""

    async def _handler(args: dict[str, Any]) -> dict[str, Any]:
        # The SDK passes the validated input dict; dispatch through the same
        # registry path the main loop uses so behaviour is identical.
        res = await registry.dispatch(spec.name, dict(args or {}), ctx)
        if res.ok:
            return {"content": _result_to_mcp_content(res.result)}
        err = res.error or {"kind": "error", "message": "tool failed"}
        return {
            "content": [{"type": "text", "text": json.dumps(err, ensure_ascii=False, default=str)}],
            "isError": True,
        }

    # ``@tool(name, description, input_schema)`` accepts a JSON-schema dict —
    # which is exactly what ToolSpec.input_schema already is.
    return tool(spec.name, spec.description, spec.input_schema)(_handler)


def build_tool_server(ctx: ToolCtx) -> "tuple[object, list[str]]":
    """Build the in-process MCP server for this turn.

    Returns the server config (to drop into ``ClaudeAgentOptions.mcp_servers``)
    and the fully-qualified ``mcp__voitta__<tool>`` names to allow.
    """
    specs = registry.visible_for_host(ctx.host)
    sdk_tools = [_make_tool(s, ctx) for s in specs]
    server = create_sdk_mcp_server(name=MCP_SERVER_NAME, tools=sdk_tools)
    allowed = [f"mcp__{MCP_SERVER_NAME}__{s.name}" for s in specs]
    logger.info(
        "agent_sdk bridge: host=%r exposing %d tools", ctx.host, len(allowed)
    )
    return server, allowed
