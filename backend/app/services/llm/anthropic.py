"""Anthropic adapter.

The normalised types already use Anthropic's block shape, so this is
mostly a pass-through. We pin the official endpoint explicitly because
the process may be launched from a shell where ``ANTHROPIC_BASE_URL`` is
set to a proxy (e.g. Claude Code's local relay), which would reject
user-supplied API keys with a 401.

Defence in depth:

  1. ``ANTHROPIC_BASE_URL`` is popped from ``os.environ`` at import time
     so no code path on the server side can read it.
  2. ``base_url=ANTHROPIC_BASE_URL`` is still passed explicitly to the
     client constructor — the SDK respects this regardless of env state.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from anthropic import AsyncAnthropic

from app.services.llm.base import (
    Message,
    NormalisedRequest,
    NormalisedResponse,
    Provider,
    Usage,
)


logger = logging.getLogger(__name__)


# Hardcoded to the official endpoint. Do not parameterise — this is the
# only URL the backend should ever talk to for Anthropic.
ANTHROPIC_BASE_URL = "https://api.anthropic.com"


# Strip Anthropic env-var overrides at import time. Idempotent: each
# `pop` returns None if the var wasn't set, no-op then.
for _var in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"):
    _stale = os.environ.pop(_var, None)
    if _stale is not None:
        # Surface this so it's obvious in the logs why connections might
        # have been failing under e.g. Claude Code's local relay.
        logger.info(
            "anthropic adapter: stripped %s from os.environ at import "
            "(was %r); using hardcoded base_url=%s",
            _var,
            (_stale[:24] + "…") if len(_stale) > 24 else _stale,
            ANTHROPIC_BASE_URL,
        )


class AnthropicProvider(Provider):
    id = "anthropic"

    def __init__(self, api_key: str) -> None:
        self._client = AsyncAnthropic(api_key=api_key, base_url=ANTHROPIC_BASE_URL)

    async def create_message(self, req: NormalisedRequest) -> NormalisedResponse:
        messages = [_message_to_dict(m) for m in req.messages]

        # Cache breakpoints — Claude Code-style sliding window. The Messages
        # API allows up to 4 `cache_control` markers per request; we use all
        # of them.
        #
        #   1. Last block of the system prompt — caches `tools + system`
        #      together (render order is tools → system, so a marker on the
        #      last system block covers both). This is the frozen prefix
        #      every request reuses.
        #   2-4. Last block of each of the three most recently appended
        #        messages. As the conversation (or tool-use loop) grows,
        #        the previous request's tail markers become this request's
        #        cache-read points; the new tail writes fresh. Three rolling
        #        markers also cover the 20-block lookback window for long
        #        agentic loops where a single user-turn spawns many
        #        tool_use/tool_result block pairs.
        for offset in range(1, min(4, len(messages) + 1)):
            blocks = messages[-offset].get("content")
            if blocks and isinstance(blocks[-1], dict):
                blocks[-1]["cache_control"] = {"type": "ephemeral"}

        kwargs: dict[str, Any] = dict(
            model=req.model,
            system=[
                {
                    "type": "text",
                    "text": req.system,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            max_tokens=req.max_tokens,
            messages=messages,
        )
        if req.tools:
            kwargs["tools"] = [
                {
                    "name": t.name,
                    "description": t.description,
                    "input_schema": t.input_schema,
                }
                for t in req.tools
            ]
        result = await self._client.messages.create(**kwargs)
        return _from_anthropic(result)


def _message_to_dict(m: Message) -> dict[str, Any]:
    return {"role": m.role, "content": [_strip_internal_keys(b) for b in m.content]}


def _strip_internal_keys(block: dict[str, Any]) -> dict[str, Any]:
    """Drop leading-underscore keys before sending to Anthropic.

    The chat orchestrator tags ``tool_result`` blocks with ``_name`` (used by
    the Gemini adapter to populate ``function_response.name``). Anthropic's
    Messages API rejects unknown fields with HTTP 400. Convention: any
    cross-provider sentinel field starts with ``_``; this filter keeps the
    Anthropic wire payload clean without coupling the adapters together.
    """

    return {k: v for k, v in block.items() if not (isinstance(k, str) and k.startswith("_"))}


def _from_anthropic(result: Any) -> NormalisedResponse:
    content: list[dict[str, Any]] = []
    for block in result.content or []:
        btype = getattr(block, "type", None)
        if btype == "text":
            content.append({"type": "text", "text": getattr(block, "text", "") or ""})
        elif btype == "tool_use":
            content.append(
                {
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": block.input,
                }
            )
        elif btype == "thinking":
            block_dict: dict[str, Any] = {
                "type": "thinking",
                "thinking": getattr(block, "thinking", "") or "",
            }
            sig = getattr(block, "signature", None)
            if sig is not None:
                block_dict["signature"] = sig
            content.append(block_dict)
        else:
            # Best-effort fall-through for any future block type.
            if hasattr(block, "model_dump"):
                content.append({k: v for k, v in block.model_dump().items() if v is not None})

    usage = Usage(
        input_tokens=getattr(result.usage, "input_tokens", 0) or 0,
        output_tokens=getattr(result.usage, "output_tokens", 0) or 0,
        cache_read_input_tokens=getattr(result.usage, "cache_read_input_tokens", 0) or 0,
        cache_creation_input_tokens=getattr(result.usage, "cache_creation_input_tokens", 0) or 0,
    )
    stop_reason = result.stop_reason or "end_turn"
    if stop_reason not in ("end_turn", "tool_use", "max_tokens", "stop_sequence"):
        stop_reason = "end_turn"
    return NormalisedResponse(
        content=content,
        stop_reason=stop_reason,
        usage=usage,
        model=result.model or "",
        raw=result,
    )
