"""Chat endpoint with provider-agnostic tool-use loop.

Tool-use is multi-iteration: each provider stream yields normalised
block events; when ``stop_reason == "tool_use"`` we dispatch every
requested tool in parallel, append the results as ``tool_result``
blocks, and re-open the provider stream for the next iteration. The
SSE response to the browser stays open across iterations.

See streaming-migration.md for the protocol contract.

SSE events (additive fields beyond the original shape: ``seq``,
``iteration``, ``block_index``, ``parent_block_index``, ``sub_seq``,
``partial``):

* ``start          { model, provider, tools, seq, iteration }``
* ``delta          { seq, iteration, block_index, text }``
* ``tool_use_start { seq, iteration, block_index, id, name }``
* ``rich           { seq, iteration, parent_block_index, sub_seq, … }``
* ``tool_use_end   { seq, iteration, block_index, id, name, ok,
                     latency_ms, error?, input, result_preview }``
* ``done  { seq, stop_reason, usage, iterations }``
* ``error { seq, iteration, type, message, partial,
            during_block_index }``
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator, Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from app.config import settings
from app.services.llm import (
    Message as LlmMessage,
    NormalisedRequest,
    ProviderId,
    ProviderNotConfigured,
    ToolSchema,
    default_model_for,
    get_provider,
)
from app.services.llm.stream import (
    BlockDelta,
    BlockStart,
    BlockStop,
    MessageStop,
    StreamError,
)
from app.tools import registry
from app.tools.registry import ToolCtx


router = APIRouter()
logger = logging.getLogger(__name__)


# Cap on a single tool result's serialised size before it goes back to the
# model. Specific tools have their own slimmers (see _trim.py); this is the
# orchestrator-level final-resort cap.
MAX_TOOL_RESULT_BYTES = 32_000

# Cap on the *UI preview* shipped with each tool_use_end SSE event.
MAX_PREVIEW_BYTES = 4_000


class TextBlock(BaseModel):
    type: Literal["text"]
    text: str


class ImageSource(BaseModel):
    type: Literal["base64"]
    media_type: str
    data: str


class ImageBlock(BaseModel):
    type: Literal["image"]
    source: ImageSource


ContentBlock = TextBlock | ImageBlock


class Message(BaseModel):
    role: Literal["user", "assistant"]
    # Either a plain string (text-only, the historical shape) or a list
    # of typed content blocks. Frontend emits blocks only when an
    # attachment is present — text-only turns stay strings for
    # backward compatibility with any older client.
    content: str | list[ContentBlock]


class ChatRequest(BaseModel):
    messages: list[Message] = Field(default_factory=list)
    session_id: str | None = None  # bridge session for browser-side tools
    provider: ProviderId | None = None
    model: str | None = None
    system: str | None = None
    max_tokens: int | None = None
    max_tool_iterations: int | None = None
    # Provider API key, supplied by the in-pane Settings view. Never logged,
    # never persisted on the server. There is no .env fallback — an empty
    # value yields a `provider_not_configured` error event.
    api_key: str | None = None


def _sse(event: str, data: dict) -> dict:
    return {"event": event, "data": json.dumps(data, ensure_ascii=False)}


def _preview_text(value: Any) -> str:
    text = (
        value
        if isinstance(value, str)
        else json.dumps(value, ensure_ascii=False, default=str, indent=2)
    )
    if len(text) <= MAX_PREVIEW_BYTES:
        return text
    return text[:MAX_PREVIEW_BYTES] + f"\n…[preview truncated: {len(text)} bytes]"


def _tool_result_content(result: Any) -> str:
    """JSON-encode and clamp to MAX_TOOL_RESULT_BYTES so a runaway response
    can't blow the LLM's context window."""

    text = (
        result
        if isinstance(result, str)
        else json.dumps(result, ensure_ascii=False, default=str)
    )
    if len(text) <= MAX_TOOL_RESULT_BYTES:
        return text
    head = text[:MAX_TOOL_RESULT_BYTES]
    return (
        head
        + "\n\n…[result truncated: "
        + str(len(text))
        + f" bytes, hard cap {MAX_TOOL_RESULT_BYTES}. "
        + "Use a *_from_buffer or fetch_to_buffer tool for the full data.]"
    )


async def _stream(req: ChatRequest) -> AsyncIterator[dict]:
    seq = _SeqCounter()

    if not req.provider:
        yield _sse(
            "error",
            {
                "seq": seq.next(),
                "iteration": 0,
                "type": "provider_not_configured",
                "message": "no provider in chat request — open the Settings panel and pick one",
                "partial": False,
                "during_block_index": None,
                "provider": None,
            },
        )
        return
    provider_id: ProviderId = req.provider
    try:
        provider = get_provider(provider_id, req.api_key)
    except ProviderNotConfigured as exc:
        yield _sse(
            "error",
            {
                "seq": seq.next(),
                "iteration": 0,
                "type": "provider_not_configured",
                "message": str(exc),
                "partial": False,
                "during_block_index": None,
                "provider": exc.provider,
            },
        )
        return

    model = req.model or default_model_for(provider_id)
    max_tokens = req.max_tokens or settings.max_tokens
    max_iterations = min(
        req.max_tool_iterations or settings.max_tool_iterations,
        settings.max_tool_iterations_ceiling,
    )
    ctx = ToolCtx(session_id=req.session_id)

    from app.bridge import bus as _bridge_bus
    host: str | None = None
    if req.session_id:
        s = _bridge_bus.bridge.get(req.session_id)
        if s is not None:
            page = (s.identification or {}).get("page") or {}
            host = page.get("host")
    tools = [
        ToolSchema(name=s.name, description=s.description, input_schema=s.input_schema)
        for s in registry.visible_for_host(host)
    ]

    from app.config import VOITTA_SYSTEM_PROMPT
    system = req.system or VOITTA_SYSTEM_PROMPT

    # Append host-scoped plugin addenda. Each loaded plugin can ship
    # a ``system_prompt`` file (declared in its manifest); it's pulled
    # in only when the user's page host matches one of the plugin's
    # ``host_patterns``. This keeps third-party rules out of the core
    # prompt — the plugin owns its own LLM contract.
    from app.tools.providers import plugins_for_host
    for plugin in plugins_for_host(host):
        addendum = plugin.get("system_prompt")
        if addendum:
            system = system.rstrip() + "\n\n" + addendum.rstrip()

    from app.services import python_storage_context as _ps_ctx
    ambient = _ps_ctx.get_context_block()
    if ambient:
        system = (system or "").rstrip() + "\n\n" + ambient

    messages: list[LlmMessage] = []
    for m in req.messages:
        if isinstance(m.content, str):
            blocks: list[dict[str, Any]] = [{"type": "text", "text": m.content}]
        else:
            blocks = [b.model_dump() for b in m.content]
        messages.append(LlmMessage(role=m.role, content=blocks))

    yield _sse(
        "start",
        {
            "seq": seq.next(),
            "iteration": 0,
            "model": model,
            "provider": provider_id,
            "tools": registry.names(),
        },
    )

    total_usage = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }
    iteration = 0
    current_block_index: int | None = None

    try:
        for iteration in range(max_iterations):
            # Per-iteration block assembly state.
            blocks_by_index: dict[int, dict[str, Any]] = {}
            text_buf: dict[int, list[str]] = {}
            args_buf: dict[int, list[str]] = {}
            iter_stop_reason: str = "end_turn"

            async with provider.stream(
                NormalisedRequest(
                    model=model,
                    system=system,
                    max_tokens=max_tokens,
                    messages=messages,
                    tools=tools,
                )
            ) as events:
                async for ev in events:
                    if isinstance(ev, BlockStart):
                        current_block_index = ev.block_index
                        if ev.kind == "text":
                            blocks_by_index[ev.block_index] = {"type": "text", "text": ""}
                            text_buf[ev.block_index] = []
                        else:
                            blocks_by_index[ev.block_index] = {
                                "type": "tool_use",
                                "id": ev.tool_id or "",
                                "name": ev.tool_name or "",
                                "input": {},
                            }
                            args_buf[ev.block_index] = []
                            # Emit tool_use_start as soon as we see it — this
                            # is the whole point of streaming.
                            yield _sse(
                                "tool_use_start",
                                {
                                    "seq": seq.next(),
                                    "iteration": iteration,
                                    "block_index": ev.block_index,
                                    "id": ev.tool_id or "",
                                    "name": ev.tool_name or "",
                                },
                            )
                    elif isinstance(ev, BlockDelta):
                        if ev.kind == "text":
                            text_buf.setdefault(ev.block_index, []).append(ev.text)
                        else:
                            args_buf.setdefault(ev.block_index, []).append(ev.text)
                    elif isinstance(ev, BlockStop):
                        block = blocks_by_index.get(ev.block_index)
                        if block is None:
                            continue
                        if block["type"] == "text":
                            # Buffering policy: flush text as a single
                            # `delta` SSE event per block. Future
                            # per-token rendering flips this to emit on
                            # each BlockDelta(kind="text") instead. See
                            # streaming-migration.md §10.
                            joined = "".join(text_buf.get(ev.block_index, []))
                            block["text"] = joined
                            if joined:
                                yield _sse(
                                    "delta",
                                    {
                                        "seq": seq.next(),
                                        "iteration": iteration,
                                        "block_index": ev.block_index,
                                        "text": joined,
                                    },
                                )
                        else:
                            joined = "".join(args_buf.get(ev.block_index, []))
                            if joined:
                                try:
                                    block["input"] = json.loads(joined)
                                except json.JSONDecodeError:
                                    block["input"] = {"_raw": joined}
                    elif isinstance(ev, MessageStop):
                        iter_stop_reason = ev.stop_reason
                        total_usage["input_tokens"] += ev.usage.input_tokens
                        total_usage["output_tokens"] += ev.usage.output_tokens
                        total_usage["cache_read_input_tokens"] += ev.usage.cache_read_input_tokens
                        total_usage["cache_creation_input_tokens"] += ev.usage.cache_creation_input_tokens
                    elif isinstance(ev, StreamError):
                        yield _sse(
                            "error",
                            {
                                "seq": seq.next(),
                                "iteration": iteration,
                                "type": ev.type,
                                "message": ev.message,
                                "partial": ev.partial,
                                "during_block_index": current_block_index,
                            },
                        )
                        return

            # Iteration complete. Assemble the assistant message from
            # blocks in block_index order.
            assistant_content: list[dict[str, Any]] = []
            for idx in sorted(blocks_by_index.keys()):
                b = blocks_by_index[idx]
                if b["type"] == "text" and not b.get("text"):
                    continue
                assistant_content.append(b)

            if iter_stop_reason != "tool_use":
                yield _sse(
                    "done",
                    {
                        "seq": seq.next(),
                        "stop_reason": iter_stop_reason,
                        "usage": total_usage,
                        "iterations": iteration + 1,
                    },
                )
                return

            # tool_use path — dispatch tools, emit rich + tool_use_end,
            # append assistant + tool_result blocks, loop.
            tool_uses = [
                (idx, blocks_by_index[idx])
                for idx in sorted(blocks_by_index.keys())
                if blocks_by_index[idx]["type"] == "tool_use"
            ]
            if not tool_uses:
                yield _sse(
                    "error",
                    {
                        "seq": seq.next(),
                        "iteration": iteration,
                        "type": "ProtocolError",
                        "message": "stop_reason=tool_use but no tool_use blocks present",
                        "partial": True,
                        "during_block_index": current_block_index,
                    },
                )
                return

            dispatch_tasks = [
                registry.dispatch(tu["name"], dict(tu.get("input") or {}), ctx)
                for _, tu in tool_uses
            ]
            results = await asyncio.gather(*dispatch_tasks)

            tool_result_blocks: list[dict[str, Any]] = []
            for (block_idx, tu), res in zip(tool_uses, results):
                preview_payload = res.result if res.ok else {"error": res.error}
                rich_items = (
                    list(res.result.get("items") or [])
                    if res.ok and isinstance(res.result, dict)
                    else []
                )
                sub_seq = 0
                for item in rich_items:
                    if isinstance(item, dict):
                        yield _sse(
                            "rich",
                            {
                                "seq": seq.next(),
                                "iteration": iteration,
                                "parent_block_index": block_idx,
                                "sub_seq": sub_seq,
                                **item,
                            },
                        )
                        sub_seq += 1

                content_payload = res.result if res.ok else {"error": res.error}
                # Image-bearing tools tag results with `_image`. See the
                # pre-streaming version of this function (in git history)
                # for the full rationale on per-provider image handling.
                image_block: dict[str, Any] | None = None
                if isinstance(content_payload, dict) and "_image" in content_payload:
                    img = content_payload.pop("_image", None)
                    if (
                        isinstance(img, dict)
                        and isinstance(img.get("data"), str)
                        and isinstance(img.get("media_type"), str)
                    ):
                        image_block = {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": img["media_type"],
                                "data": img["data"],
                            },
                        }
                        rich_url = img.get("url") or (
                            f"data:{img['media_type']};base64,{img['data']}"
                        )
                        yield _sse(
                            "rich",
                            {
                                "seq": seq.next(),
                                "iteration": iteration,
                                "parent_block_index": block_idx,
                                "sub_seq": sub_seq,
                                "kind": "image",
                                "url": rich_url,
                                "alt": (
                                    "Report screenshot: "
                                    f"{content_payload.get('report_id') or 'unnamed'}"
                                ),
                            },
                        )
                        sub_seq += 1

                yield _sse(
                    "tool_use_end",
                    {
                        "seq": seq.next(),
                        "iteration": iteration,
                        "block_index": block_idx,
                        "id": tu["id"],
                        "name": tu["name"],
                        "ok": res.ok,
                        "latency_ms": res.latency_ms,
                        "error": res.error,
                        "input": dict(tu.get("input") or {}),
                        "result_preview": _preview_text(preview_payload),
                    },
                )

                if image_block is not None and provider_id == "anthropic":
                    text_part = _tool_result_content(content_payload)
                    tool_result_blocks.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tu["id"],
                            "content": [
                                {"type": "text", "text": text_part},
                                image_block,
                            ],
                            "is_error": not res.ok,
                            "_name": tu["name"],
                        }
                    )
                else:
                    if image_block is not None:
                        if isinstance(content_payload, dict):
                            content_payload = dict(content_payload)
                            content_payload["_image_note"] = (
                                "screenshot stored in python_storage; "
                                "current provider doesn't accept inline "
                                "images in tool results — switch to "
                                "Anthropic to view it."
                            )
                    tool_result_blocks.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tu["id"],
                            "content": _tool_result_content(content_payload),
                            "is_error": not res.ok,
                            "_name": tu["name"],
                        }
                    )

            messages.append(LlmMessage(role="assistant", content=assistant_content))
            messages.append(LlmMessage(role="user", content=tool_result_blocks))

        yield _sse(
            "error",
            {
                "seq": seq.next(),
                "iteration": iteration,
                "type": "IterationLimit",
                "message": f"tool-use loop exceeded {max_iterations} iterations",
                "partial": True,
                "during_block_index": current_block_index,
            },
        )
    except asyncio.CancelledError:
        # Stop: client disconnected. Do NOT emit further SSE — the
        # connection is gone and the upstream `async with provider.stream`
        # is already unwinding. Re-raise so sse_starlette / asyncio sees
        # the cancellation propagate cleanly.
        logger.info(
            "chat.stream cancelled iteration=%d block_index=%s",
            iteration,
            current_block_index,
        )
        raise
    except Exception as exc:
        logger.exception("chat stream failed")
        yield _sse(
            "error",
            {
                "seq": seq.next(),
                "iteration": iteration,
                "type": type(exc).__name__,
                "message": str(exc),
                "partial": True,
                "during_block_index": current_block_index,
            },
        )


class _SeqCounter:
    """Monotonic per-turn sequence counter for SSE events."""

    __slots__ = ("_n",)

    def __init__(self) -> None:
        self._n = -1

    def next(self) -> int:
        self._n += 1
        return self._n


@router.post("/chat/stream")
async def chat_stream(req: ChatRequest) -> EventSourceResponse:
    return EventSourceResponse(_stream(req), ping=15)
