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
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from anthropic import AsyncAnthropic

from app.services.llm.base import (
    BaseProvider,
    Message,
    NormalisedRequest,
    Usage,
)
from app.services.llm.stream import (
    BlockDelta,
    BlockStart,
    BlockStop,
    MessageStop,
    StreamEvent,
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


class AnthropicProvider(BaseProvider):
    id = "anthropic"

    def __init__(self, api_key: str) -> None:
        self._client = AsyncAnthropic(api_key=api_key, base_url=ANTHROPIC_BASE_URL)

    def _build_kwargs(self, req: NormalisedRequest) -> dict[str, Any]:
        messages = [_message_to_dict(m) for m in req.messages]

        # Cache breakpoints — Claude Code-style sliding window. See the
        # pre-streaming version of this adapter for the full rationale: the
        # Messages API allows 4 `cache_control` markers; we put one on the
        # system tail and three on the three most recently appended messages.
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
            # `eager_input_streaming: true` opts each tool into Anthropic's
            # fine-grained tool streaming. Without this flag the server
            # buffers tool args for schema validation and ships them as
            # a single late burst (observed ~30s gap mid-block for large
            # JSON inputs); with it, partial_json fragments stream as the
            # model generates them. Trade-off: mid-stream JSON may be
            # invalid — we already only parse on BlockStop, and fall back
            # to {"_raw": joined} if json.loads fails (chat.py).
            kwargs["tools"] = [
                {
                    "name": t.name,
                    "description": t.description,
                    "input_schema": t.input_schema,
                    "eager_input_streaming": True,
                }
                for t in req.tools
            ]
        return kwargs

    def stream(self, req: NormalisedRequest):
        return _AnthropicStreamCM(self._client, self._build_kwargs(req))


class _AnthropicStreamCM:
    """Async context manager wrapping `client.messages.stream()`.

    Maps Anthropic SSE events to our normalised `StreamEvent`s.
    """

    def __init__(self, client: AsyncAnthropic, kwargs: dict[str, Any]) -> None:
        self._client = client
        self._kwargs = kwargs
        self._cm = None
        self._stream_obj = None

    async def __aenter__(self) -> AsyncIterator[StreamEvent]:
        self._cm = self._client.messages.stream(**self._kwargs)
        self._stream_obj = await self._cm.__aenter__()
        return self._iter_events(self._stream_obj)

    async def __aexit__(self, exc_type, exc, tb):
        if self._cm is not None:
            return await self._cm.__aexit__(exc_type, exc, tb)
        return False

    @staticmethod
    async def _iter_events(stream_obj: Any) -> AsyncIterator[StreamEvent]:
        usage = Usage()
        stop_reason: str = "end_turn"

        async for event in stream_obj:
            etype = getattr(event, "type", None)
            if etype == "content_block_start":
                idx = getattr(event, "index", 0)
                block = getattr(event, "content_block", None)
                btype = getattr(block, "type", None) if block is not None else None
                if btype == "text":
                    yield BlockStart(block_index=idx, kind="text")
                elif btype == "tool_use":
                    yield BlockStart(
                        block_index=idx,
                        kind="tool_use",
                        tool_id=getattr(block, "id", "") or "",
                        tool_name=getattr(block, "name", "") or "",
                    )
                # `thinking` blocks are not surfaced to the orchestrator as
                # streamable blocks — they're internal to Claude's reasoning
                # and we keep the current behaviour of ignoring them on the
                # wire. They still arrive in the final message via the
                # MessageStop path below if the caller needs them.
            elif etype == "content_block_delta":
                idx = getattr(event, "index", 0)
                delta = getattr(event, "delta", None)
                dtype = getattr(delta, "type", None) if delta is not None else None
                if dtype == "text_delta":
                    text = getattr(delta, "text", "") or ""
                    if text:
                        yield BlockDelta(block_index=idx, kind="text", text=text)
                elif dtype == "input_json_delta":
                    partial = getattr(delta, "partial_json", "") or ""
                    if partial:
                        yield BlockDelta(block_index=idx, kind="tool_args", text=partial)
            elif etype == "content_block_stop":
                idx = getattr(event, "index", 0)
                yield BlockStop(block_index=idx)
            elif etype == "message_delta":
                delta = getattr(event, "delta", None)
                if delta is not None:
                    sr = getattr(delta, "stop_reason", None)
                    if sr:
                        stop_reason = sr
                u = getattr(event, "usage", None)
                if u is not None:
                    # `message_delta` usage is cumulative output_tokens.
                    out = getattr(u, "output_tokens", None)
                    if out is not None:
                        usage.output_tokens = out
            elif etype == "message_start":
                msg = getattr(event, "message", None)
                if msg is not None:
                    u = getattr(msg, "usage", None)
                    if u is not None:
                        usage.input_tokens = getattr(u, "input_tokens", 0) or 0
                        usage.output_tokens = getattr(u, "output_tokens", 0) or 0
                        usage.cache_read_input_tokens = (
                            getattr(u, "cache_read_input_tokens", 0) or 0
                        )
                        usage.cache_creation_input_tokens = (
                            getattr(u, "cache_creation_input_tokens", 0) or 0
                        )
            elif etype == "message_stop":
                if stop_reason not in ("end_turn", "tool_use", "max_tokens", "stop_sequence"):
                    stop_reason = "end_turn"
                yield MessageStop(stop_reason=stop_reason, usage=usage)  # type: ignore[arg-type]


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


