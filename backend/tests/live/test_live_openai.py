"""Tier 2 — live OpenAI API tests.

Run: OPENAI_API_KEY=sk-... uv run pytest backend/tests/live/test_live_openai.py -m live -v
"""

from __future__ import annotations

import time

import pytest

from app.services.llm.base import NormalisedRequest, ToolSchema
from app.services.llm.openai import OpenAIProvider
from app.services.llm.stream import (
    BlockStart,
    BlockDelta,
    MessageStop,
)


MODEL = "gpt-4.1-mini"


@pytest.mark.live
@pytest.mark.asyncio
async def test_live_openai_text_streaming(openai_key):
    provider = OpenAIProvider(api_key=openai_key)
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
async def test_live_openai_tool_use_early_name(openai_key):
    provider = OpenAIProvider(api_key=openai_key)
    tool = ToolSchema(
        name="create_report",
        description="Create a long HTML report. body MUST be at least 3000 chars.",
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
        system="When asked for a report, call create_report with body >= 3000 chars.",
        max_tokens=4096,
        messages=[{
            "role": "user",
            "content": [{"type": "text", "text": "Create a report titled 'Stars' with at least 3000 characters of HTML body."}],
        }],  # type: ignore[list-item]
        tools=[tool],
    )

    t0 = time.monotonic()
    tool_start_time = None
    message_stop_time = None
    async with provider.stream(req) as events:
        async for ev in events:
            if isinstance(ev, BlockStart) and ev.kind == "tool_use":
                tool_start_time = time.monotonic() - t0
            elif isinstance(ev, MessageStop):
                message_stop_time = time.monotonic() - t0

    assert tool_start_time is not None
    assert message_stop_time is not None
    gap = message_stop_time - tool_start_time
    assert gap >= 0.5, f"tool_use_start arrived too late: gap={gap:.2f}s"


@pytest.mark.live
@pytest.mark.asyncio
async def test_live_openai_cancellation_closes_upstream(openai_key):
    import asyncio

    provider = OpenAIProvider(api_key=openai_key)
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
async def test_live_openai_usage_parity(openai_key):
    provider = OpenAIProvider(api_key=openai_key)
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
async def test_live_openai_end_to_end_chat_route(openai_key):
    import json
    from app.routes.chat import ChatRequest, Message as RouteMessage, _stream

    req = ChatRequest(
        provider="openai",
        api_key=openai_key,
        model=MODEL,
        max_tokens=128,
        messages=[RouteMessage(role="user", content="Say hi in 5 words.")],
    )
    events = []
    async for raw in _stream(req):
        events.append({"event": raw["event"], "data": json.loads(raw["data"])})
    names = [e["event"] for e in events]
    assert "start" in names and "delta" in names and "done" in names
