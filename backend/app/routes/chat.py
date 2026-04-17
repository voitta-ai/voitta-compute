"""Chat endpoint with provider-agnostic tool-use loop.

Tool-use is multi-iteration: each provider call returns a normalised
response with ``stop_reason``. When ``stop_reason == "tool_use"`` we
dispatch every requested tool in parallel through the registry, append
the results as ``tool_result`` blocks, and call the provider again. The
SSE response to the browser stays open across iterations.

SSE events:

* ``start { model, provider, tools }``      — first iteration begins
* ``delta { text }``                        — text from the model
* ``tool_use_start { id, name }``           — model wants to call a tool
* ``tool_use_end   { id, name, ok, latency_ms, error?, input, result_preview }``
* ``done  { stop_reason, usage, iterations }``
* ``error { message, type }``
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


class Message(BaseModel):
    role: Literal["user", "assistant"]
    content: str


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
    if not req.provider:
        yield _sse(
            "error",
            {
                "message": "no provider in chat request — open the Settings panel and pick one",
                "type": "provider_not_configured",
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
                "message": str(exc),
                "type": "provider_not_configured",
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

    # Host-aware tool exposure: tools with a host_pattern are only visible
    # when the bookmarklet's registered page.host matches. Avoids
    # confusing the model with site-specific tools (e.g. drive_*) on
    # unrelated pages.
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

    # Ambient python_storage context — appended to the system prompt
    # every turn. Lets the LLM use existing snapshot handles directly
    # without re-searching/re-downloading. See
    # app/services/python_storage_context.py for the rationale.
    from app.services import python_storage_context as _ps_ctx
    ambient = _ps_ctx.get_context_block()
    if ambient:
        system = (system or "").rstrip() + "\n\n" + ambient


    # The chat-route's ``Message`` carries plain string content. The
    # orchestrator works in block form so it can append ``tool_use`` /
    # ``tool_result`` blocks across iterations.
    messages: list[LlmMessage] = [
        LlmMessage(role=m.role, content=[{"type": "text", "text": m.content}])
        for m in req.messages
    ]

    yield _sse(
        "start",
        {"model": model, "provider": provider_id, "tools": registry.names()},
    )

    try:
        for iteration in range(max_iterations):
            response = await provider.create_message(
                NormalisedRequest(
                    model=model,
                    system=system,
                    max_tokens=max_tokens,
                    messages=messages,
                    tools=tools,
                )
            )

            for block in response.content:
                btype = block.get("type")
                if btype == "text":
                    text = block.get("text") or ""
                    if text:
                        yield _sse("delta", {"text": text})
                elif btype == "tool_use":
                    yield _sse(
                        "tool_use_start",
                        {"id": block["id"], "name": block["name"]},
                    )

            if response.stop_reason != "tool_use":
                yield _sse(
                    "done",
                    {
                        "stop_reason": response.stop_reason,
                        "usage": {
                            "input_tokens": response.usage.input_tokens,
                            "output_tokens": response.usage.output_tokens,
                            "cache_read_input_tokens": response.usage.cache_read_input_tokens,
                            "cache_creation_input_tokens": response.usage.cache_creation_input_tokens,
                        },
                        "iterations": iteration + 1,
                    },
                )
                return

            tool_uses = [b for b in response.content if b.get("type") == "tool_use"]
            if not tool_uses:
                yield _sse(
                    "error",
                    {
                        "message": "stop_reason=tool_use but no tool_use blocks present",
                        "type": "ProtocolError",
                    },
                )
                return

            dispatch_tasks = [
                registry.dispatch(tu["name"], dict(tu.get("input") or {}), ctx)
                for tu in tool_uses
            ]
            results = await asyncio.gather(*dispatch_tasks)

            tool_result_blocks: list[dict[str, Any]] = []
            for tu, res in zip(tool_uses, results):
                preview_payload = res.result if res.ok else {"error": res.error}
                # Some server-side tools (e.g. `run_compute`) collect
                # rich items (text/markdown blocks, images) during their
                # execution and surface them as `result.items`. Emit
                # those as `rich` SSE events here so they render inline
                # in the chat stream — same TurnItem path as the
                # browser-side rich-output sink. We also strip them
                # from the preview that travels to the model (the model
                # already gets ctx.text content as part of its working
                # state via the script's return value, and image URLs
                # would bloat the context to no purpose).
                rich_items = (
                    list(res.result.get("items") or [])
                    if res.ok and isinstance(res.result, dict)
                    else []
                )
                for item in rich_items:
                    if isinstance(item, dict):
                        yield _sse("rich", item)

                yield _sse(
                    "tool_use_end",
                    {
                        "id": tu["id"],
                        "name": tu["name"],
                        "ok": res.ok,
                        "latency_ms": res.latency_ms,
                        "error": res.error,
                        "input": dict(tu.get("input") or {}),
                        "result_preview": _preview_text(preview_payload),
                    },
                )
                content_payload = res.result if res.ok else {"error": res.error}
                # Image-bearing tools (e.g. `screenshot_report`) tag their
                # result with a private `_image: {media_type, data}` field
                # that we strip before serialising. For Anthropic the
                # tool_result.content is a list of blocks (text + image),
                # so the model literally SEES the screenshot. Other
                # providers (OpenAI / Gemini) take only string content
                # blocks here, so they get a textual note pointing at
                # the python_storage handle — the image still renders
                # in the chat pane via the rich item below.
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
                        # Also push it into the chat pane as a rich
                        # item so the user sees what the model sees.
                        # Prefer the backend HTTPS URL (CSP-safe under
                        # the host page's policy); fall back to a
                        # data: URL only if the producing tool didn't
                        # supply one — most host pages (Drive,
                        # Gmail) reject `data:` in
                        # `img-src`, so the data-URL fallback is
                        # mostly useful for local dev / harness pages.
                        rich_url = img.get("url") or (
                            f"data:{img['media_type']};base64,{img['data']}"
                        )
                        yield _sse(
                            "rich",
                            {
                                "kind": "image",
                                "url": rich_url,
                                "alt": (
                                    "Report screenshot: "
                                    f"{content_payload.get('report_id') or 'unnamed'}"
                                ),
                            },
                        )

                if image_block is not None and provider_id == "anthropic":
                    # Multi-block tool_result content (Anthropic only).
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
                        # Non-Anthropic provider: surface a textual
                        # pointer so the model knows the screenshot
                        # exists even though it can't see it.
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
                            # `_name` is internal — the Gemini adapter uses it to
                            # populate `function_response.name`. The Anthropic
                            # adapter ignores it; OpenAI uses `tool_use_id` as
                            # `call_id` directly so it doesn't need the name.
                            "_name": tu["name"],
                        }
                    )

            messages.append(LlmMessage(role="assistant", content=response.content))
            messages.append(LlmMessage(role="user", content=tool_result_blocks))
            # loop into next iteration

        yield _sse(
            "error",
            {
                "message": f"tool-use loop exceeded {max_iterations} iterations",
                "type": "IterationLimit",
            },
        )
    except Exception as exc:
        logger.exception("chat stream failed")
        yield _sse("error", {"message": str(exc), "type": type(exc).__name__})


@router.post("/chat/stream")
async def chat_stream(req: ChatRequest) -> EventSourceResponse:
    return EventSourceResponse(_stream(req), ping=15)
