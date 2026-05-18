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
* ``tool_args_delta{ seq, iteration, block_index, id, chars }``
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
    # Three accepted shapes:
    #  1. plain string (text-only, the historical shape)
    #  2. list of typed content blocks (TextBlock | ImageBlock) — when
    #     the FE attaches an image to a user turn
    #  3. list of opaque block dicts — used when the FE replays a prior
    #     assistant turn whose blocks include ``tool_use`` / ``tool_result``
    #     entries. We keep these untyped because their shape is
    #     provider-specific (Anthropic) and we just pass them through to
    #     the provider. Validation would either duplicate Anthropic's
    #     schema or reject legitimate replays, so we accept any dict
    #     here and let the provider be the source of truth.
    content: str | list[ContentBlock] | list[dict[str, Any]]


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
    # NOTE: ambient is NOT appended to ``system`` because its body
    # includes relative timestamps ("2m ago") that drift every turn,
    # which would invalidate the system+tools cache prefix on every
    # request. Instead we append it as a text block at the START of
    # the most-recent user message below, where the new turn's cache
    # write happens anyway and the volatility costs nothing extra.

    messages: list[LlmMessage] = []
    for m in req.messages:
        if isinstance(m.content, str):
            blocks: list[dict[str, Any]] = [{"type": "text", "text": m.content}]
        else:
            # Mix of typed pydantic blocks (TextBlock/ImageBlock) and
            # opaque dicts (tool_use / tool_result replayed by the FE).
            blocks = [
                b.model_dump() if hasattr(b, "model_dump") else dict(b)
                for b in m.content
            ]
        messages.append(LlmMessage(role=m.role, content=blocks))

    # Inject the volatile python_storage ambient block at the start of
    # the most recent user-turn (if any). Putting it on the new tail
    # rather than in the system prompt preserves prefix invariance for
    # the cached tools+system block; the user-tail is where the cache
    # write happens anyway, so the drift cost stays where it's free.
    if ambient and messages:
        for m in reversed(messages):
            if m.role == "user":
                m.content = [
                    {"type": "text", "text": ambient}
                ] + list(m.content)
                break

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

    # Tracks reports touched by tool calls in this stream so we can drain
    # post-render errors into the next user message. Closes the "LLM
    # saw ready, user saw red" gap (see services.render_log_drain).
    from app.services.render_log_drain import RenderDrain, format_reminder
    render_drain = RenderDrain()

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
                            if ev.text:
                                yield _sse(
                                    "delta",
                                    {
                                        "seq": seq.next(),
                                        "iteration": iteration,
                                        "block_index": ev.block_index,
                                        "text": ev.text,
                                    },
                                )
                        else:
                            args_buf.setdefault(ev.block_index, []).append(ev.text)
                            tu_block = blocks_by_index.get(ev.block_index)
                            if tu_block is not None and tu_block.get("type") == "tool_use":
                                chars = sum(len(s) for s in args_buf[ev.block_index])
                                logger.info(
                                    "tool_args_delta name=%s block=%d frag_len=%d chars=%d",
                                    tu_block.get("name") or "?",
                                    ev.block_index,
                                    len(ev.text),
                                    chars,
                                )
                                yield _sse(
                                    "tool_args_delta",
                                    {
                                        "seq": seq.next(),
                                        "iteration": iteration,
                                        "block_index": ev.block_index,
                                        "id": tu_block.get("id") or "",
                                        "chars": chars,
                                    },
                                )
                    elif isinstance(ev, BlockStop):
                        block = blocks_by_index.get(ev.block_index)
                        if block is None:
                            continue
                        if block["type"] == "text":
                            # Text is streamed as individual `delta` SSE
                            # events on each BlockDelta above; here we
                            # just finalise the buffered assistant block
                            # for the next iteration's history.
                            block["text"] = "".join(text_buf.get(ev.block_index, []))
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
                        # Per-iteration cache log. Each agent-loop
                        # iteration is its OWN Anthropic API call with
                        # its own cache_read / cache_creation counters,
                        # so aggregating across iterations hides which
                        # one is burning. Logged unaggregated here.
                        if provider_id == "anthropic":
                            from app.services.cache_monitor import (
                                record_iteration as _cm_record_iter,
                            )
                            try:
                                _cm_record_iter(
                                    conv_id=req.session_id,
                                    model=model,
                                    iteration=iteration,
                                    usage={
                                        "input_tokens": ev.usage.input_tokens,
                                        "output_tokens": ev.usage.output_tokens,
                                        "cache_read_input_tokens": ev.usage.cache_read_input_tokens,
                                        "cache_creation_input_tokens": ev.usage.cache_creation_input_tokens,
                                    },
                                )
                            except Exception:
                                logger.exception(
                                    "cache_monitor.record_iteration raised"
                                )
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
                # Final assistant turn (no follow-up dispatch). Strip
                # ANY tool_use blocks from the persisted snapshot:
                # they never got dispatched (we exit the loop here),
                # so replaying them on the next turn would send an
                # orphan tool_use with no matching tool_result and
                # Anthropic would 400 with "tool_use ids without
                # tool_result blocks immediately after".
                #
                # This case fires when the model is mid-tool_use and
                # hits ``max_tokens`` / ``stop_sequence`` — the partial
                # tool_use accumulates in ``blocks_by_index`` but the
                # stop_reason isn't "tool_use". The user-facing fix is
                # to raise ``max_tokens`` (the partial tool_use never
                # produced anything useful anyway), so dropping the
                # block here is the safe choice.
                persistable = [
                    b for b in assistant_content if b.get("type") != "tool_use"
                ]
                yield _sse(
                    "turn_persist",
                    {
                        "seq": seq.next(),
                        "iteration": iteration,
                        "assistant_blocks": persistable,
                        "tool_result_blocks": [],
                    },
                )
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
                # Track report_ids the conversation touches so we can
                # drain post-render errors into the next user message.
                render_drain.note_tool_result(tu.get("name") or "", content_payload)
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

            # Drain any post-render errors logged since the last turn.
            # If any are present, prepend them as a system-reminder text
            # block so the next LLM turn knows about them without having
            # to call get_report_render_errors.
            drained = render_drain.drain()
            reminder = format_reminder(drained)
            if reminder:
                tool_result_blocks.insert(0, {"type": "text", "text": reminder})

            messages.append(LlmMessage(role="user", content=tool_result_blocks))

            # Tell the frontend exactly what we just stuffed into
            # ``messages`` for this iteration, so it can store the
            # blocks verbatim on its ChatMessage and replay them on
            # the next POST. Without this, the FE only sees the
            # assistant text and the model loses every memory of its
            # own tool calls after one turn — symptom: model
            # "pretends" to make edits in subsequent turns.
            #
            # Strip provider-internal keys (``_name`` etc.) before
            # sending; the FE doesn't need them and they're not part
            # of Anthropic's tool_result schema.
            yield _sse(
                "turn_persist",
                {
                    "seq": seq.next(),
                    "iteration": iteration,
                    "assistant_blocks": assistant_content,
                    "tool_result_blocks": [
                        {k: v for k, v in b.items() if not k.startswith("_")}
                        for b in tool_result_blocks
                    ],
                },
            )

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
    finally:
        # Record this turn's usage into the cache monitor. Fires on
        # every exit (done, iteration-limit error, provider error,
        # client cancel) so a misbehaving turn that returns garbage
        # still surfaces in the cache log. Provider-only — the
        # monitor itself decides whether to escalate to WARNING.
        if provider_id == "anthropic":
            from app.services.cache_monitor import record as _cm_record
            try:
                _cm_record(
                    conv_id=req.session_id,
                    model=model,
                    usage=total_usage,
                    iterations=iteration + 1,
                )
            except Exception:
                logger.exception("cache_monitor.record raised")


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
