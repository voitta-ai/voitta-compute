"""Tier 2 — live Gemini API tests.

Run: GEMINI_API_KEY=AIza... uv run pytest backend/tests/live/test_live_gemini.py -m live -v

Note: Gemini's tool-use timing assertion is INVERTED vs Anthropic/OpenAI —
function_call lands complete in one chunk (verified empirically), so
BlockStart and BlockStop fire close together for tool_use blocks. The
streaming win for Gemini is text streaming + cancellation hygiene, not
early tool name.
"""

from __future__ import annotations

import time

import pytest

from app.services.llm.base import NormalisedRequest, ToolSchema
from app.services.llm.gemini import GeminiProvider
from app.services.llm.stream import (
    BlockStart,
    BlockDelta,
    BlockStop,
    MessageStop,
)


MODEL = "gemini-2.5-flash"


@pytest.mark.live
@pytest.mark.asyncio
async def test_live_gemini_text_streaming(gemini_key):
    provider = GeminiProvider(api_key=gemini_key)
    req = NormalisedRequest(
        model=MODEL,
        system="Be terse.",
        max_tokens=256,
        messages=[{"role": "user", "content": [{"type": "text", "text": "Count to 5."}]}],  # type: ignore[list-item]
    )
    text_chunks = 0
    final = None
    async with provider.stream(req) as events:
        async for ev in events:
            if isinstance(ev, BlockDelta) and ev.kind == "text":
                text_chunks += 1
            if isinstance(ev, MessageStop):
                final = ev
    assert text_chunks >= 1
    assert final is not None
    assert final.usage.output_tokens > 0


@pytest.mark.live
@pytest.mark.asyncio
async def test_live_gemini_tool_use_arrives_atomically(gemini_key):
    """Inverted assertion: BlockStart and BlockStop for a tool_use block
    fire within the same chunk window (≤ a few ms apart in our process,
    even for arg-heavy calls)."""
    provider = GeminiProvider(api_key=gemini_key)
    tool = ToolSchema(
        name="create_report",
        description="Create an HTML report. body must be at least 3000 chars.",
        input_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "body": {"type": "string"},
            },
            "required": ["title", "body"],
        },
    )
    req = NormalisedRequest(
        model=MODEL,
        system="Call create_report with body >= 3000 chars when asked for a report.",
        max_tokens=4096,
        messages=[{
            "role": "user",
            "content": [{"type": "text", "text": "Create a report titled 'Stars' with at least 3000 characters of HTML body."}],
        }],  # type: ignore[list-item]
        tools=[tool],
    )

    start_ts = None
    stop_ts = None
    async with provider.stream(req) as events:
        async for ev in events:
            if isinstance(ev, BlockStart) and ev.kind == "tool_use":
                start_ts = time.monotonic()
            elif isinstance(ev, BlockStop) and start_ts is not None and stop_ts is None:
                stop_ts = time.monotonic()
                break

    assert start_ts is not None and stop_ts is not None
    # Atomic chunk: BlockStart and matching BlockStop within ~50ms.
    assert (stop_ts - start_ts) < 0.05


@pytest.mark.live
@pytest.mark.asyncio
async def test_live_gemini_cancellation_closes_upstream(gemini_key):
    import asyncio

    provider = GeminiProvider(api_key=gemini_key)
    req = NormalisedRequest(
        model=MODEL,
        system="Write a long essay.",
        max_tokens=2000,
        messages=[{"role": "user", "content": [{"type": "text", "text": "Write a 500-word essay on tides."}]}],  # type: ignore[list-item]
    )

    seen_first = asyncio.Event()
    consumed = []

    async def consume():
        async with provider.stream(req) as events:
            async for ev in events:
                consumed.append(ev)
                seen_first.set()

    task = asyncio.create_task(consume())
    await seen_first.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert consumed


@pytest.mark.live
@pytest.mark.asyncio
async def test_live_gemini_usage_parity(gemini_key):
    provider = GeminiProvider(api_key=gemini_key)
    req = NormalisedRequest(
        model=MODEL,
        system="Respond with exactly 'OK.'",
        max_tokens=16,
        messages=[{"role": "user", "content": [{"type": "text", "text": "Say OK."}]}],  # type: ignore[list-item]
    )
    r1 = await provider.create_message(req)
    r2 = await provider.create_message(req)
    assert r1.usage.input_tokens == r2.usage.input_tokens
    assert r1.usage.output_tokens > 0


@pytest.mark.live
@pytest.mark.asyncio
async def test_live_gemini_end_to_end_chat_route(gemini_key):
    import json
    from app.routes.chat import ChatRequest, Message as RouteMessage, _stream

    req = ChatRequest(
        provider="gemini",
        api_key=gemini_key,
        model=MODEL,
        max_tokens=128,
        messages=[RouteMessage(role="user", content="Say hi in 5 words.")],
    )
    events = []
    async for raw in _stream(req):
        events.append({"event": raw["event"], "data": json.loads(raw["data"])})
    names = [e["event"] for e in events]
    assert "start" in names and "delta" in names and "done" in names
