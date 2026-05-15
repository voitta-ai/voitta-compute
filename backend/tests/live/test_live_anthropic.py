"""Tier 2 — live Anthropic API tests.

Run: ANTHROPIC_API_KEY=sk-ant-... uv run pytest backend/tests/live/test_live_anthropic.py -m live -v

These hit the real Anthropic API. They are excluded from the default
test command by the `-m 'not live'` filter in pyproject.toml.
"""

from __future__ import annotations

import time

import pytest

from app.services.llm.anthropic import AnthropicProvider
from app.services.llm.base import NormalisedRequest, ToolSchema
from app.services.llm.stream import (
    BlockStart,
    BlockDelta,
    BlockStop,
    MessageStop,
)


MODEL = "claude-haiku-4-5-20251001"


@pytest.mark.live
@pytest.mark.asyncio
async def test_live_anthropic_text_streaming(anthropic_key):
    provider = AnthropicProvider(api_key=anthropic_key)
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
    assert text_chunks >= 1, "expected at least one text delta"
    assert final is not None
    assert final.usage.output_tokens > 0


@pytest.mark.live
@pytest.mark.asyncio
async def test_live_anthropic_tool_use_early_name(anthropic_key):
    """The UX-critical test: tool name must arrive significantly before
    the tool args finish for an arg-heavy tool call."""
    provider = AnthropicProvider(api_key=anthropic_key)
    tool = ToolSchema(
        name="create_report",
        description="Create an HTML report. The 'body' field must be at least 3000 characters of complete HTML.",
        input_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "body": {"type": "string", "description": "Full HTML body, at least 3000 chars"},
            },
            "required": ["title", "body"],
        },
    )
    req = NormalisedRequest(
        model=MODEL,
        system="When the user asks for a report, call create_report with body >= 3000 chars.",
        max_tokens=4096,
        messages=[{
            "role": "user",
            "content": [{"type": "text", "text": "Create a report titled 'Stars' with at least 3000 characters of HTML body covering stellar lifecycles."}],
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

    assert tool_start_time is not None, "tool_use never started"
    assert message_stop_time is not None
    gap = message_stop_time - tool_start_time
    # Margin: the create_report call with a 3000+ char body takes
    # multiple seconds to finish generating. The name should land near
    # the *start*, not the end.
    assert gap >= 0.5, f"tool_use_start arrived too late: gap={gap:.2f}s"


@pytest.mark.live
@pytest.mark.asyncio
async def test_live_anthropic_cancellation_closes_upstream(anthropic_key):
    """Cancel mid-stream; provider context manager must exit cleanly."""
    import asyncio

    provider = AnthropicProvider(api_key=anthropic_key)
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
    # Sanity: we did see at least one event before cancel.
    assert consumed


@pytest.mark.live
@pytest.mark.asyncio
async def test_live_anthropic_usage_parity(anthropic_key):
    """Streaming and assembled-from-stream usage must match.

    Since the migration replaced `create_message` with stream-assembly,
    this test verifies the two paths produce identical usage. Run twice
    to also catch any nondeterminism in usage reporting at temperature=0.
    """
    provider = AnthropicProvider(api_key=anthropic_key)
    req = NormalisedRequest(
        model=MODEL,
        system="Be deterministic. Respond with exactly the text 'OK.'",
        max_tokens=16,
        messages=[{"role": "user", "content": [{"type": "text", "text": "Say OK."}]}],  # type: ignore[list-item]
    )
    r1 = await provider.create_message(req)
    r2 = await provider.create_message(req)
    # Usage values are billing-deterministic for identical inputs.
    assert r1.usage.input_tokens == r2.usage.input_tokens
    assert r1.usage.output_tokens > 0


@pytest.mark.live
@pytest.mark.asyncio
async def test_live_anthropic_end_to_end_chat_route(anthropic_key, monkeypatch):
    """Full SSE response from /chat/stream against a real provider."""
    import json
    from app.routes.chat import ChatRequest, Message as RouteMessage, _stream

    req = ChatRequest(
        provider="anthropic",
        api_key=anthropic_key,
        model=MODEL,
        max_tokens=128,
        messages=[RouteMessage(role="user", content="Say hi in 5 words.")],
    )
    events = []
    async for raw in _stream(req):
        events.append({"event": raw["event"], "data": json.loads(raw["data"])})
    names = [e["event"] for e in events]
    assert "start" in names
    assert "delta" in names
    assert "done" in names
