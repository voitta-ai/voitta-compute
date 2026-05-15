"""BaseProvider.create_message() reconstructs a NormalisedResponse from
the stream — single source of truth."""

from __future__ import annotations

import pytest

from app.services.llm.base import NormalisedRequest, Usage
from app.services.llm.stream import (
    BlockStart,
    BlockDelta,
    BlockStop,
    MessageStop,
)
from tests.streaming.fake_provider import FakeStreamingProvider


@pytest.mark.asyncio
async def test_create_message_assembles_text_only():
    provider = FakeStreamingProvider(events=[
        BlockStart(block_index=0, kind="text"),
        BlockDelta(block_index=0, kind="text", text="Hello "),
        BlockDelta(block_index=0, kind="text", text="world"),
        BlockStop(block_index=0),
        MessageStop(stop_reason="end_turn", usage=Usage(input_tokens=10, output_tokens=2)),
    ])
    req = NormalisedRequest(model="x", system="", max_tokens=1024)
    resp = await provider.create_message(req)
    assert resp.content == [{"type": "text", "text": "Hello world"}]
    assert resp.stop_reason == "end_turn"
    assert resp.usage.input_tokens == 10
    assert resp.usage.output_tokens == 2


@pytest.mark.asyncio
async def test_create_message_assembles_text_then_tool_use():
    provider = FakeStreamingProvider(events=[
        BlockStart(block_index=0, kind="text"),
        BlockDelta(block_index=0, kind="text", text="I'll check."),
        BlockStop(block_index=0),
        BlockStart(block_index=1, kind="tool_use", tool_id="t1", tool_name="search"),
        BlockDelta(block_index=1, kind="tool_args", text='{"q":"'),
        BlockDelta(block_index=1, kind="tool_args", text='hello"}'),
        BlockStop(block_index=1),
        MessageStop(stop_reason="tool_use", usage=Usage(input_tokens=20, output_tokens=5)),
    ])
    req = NormalisedRequest(model="x", system="", max_tokens=1024)
    resp = await provider.create_message(req)
    assert resp.content == [
        {"type": "text", "text": "I'll check."},
        {"type": "tool_use", "id": "t1", "name": "search", "input": {"q": "hello"}},
    ]
    assert resp.stop_reason == "tool_use"


@pytest.mark.asyncio
async def test_create_message_drops_empty_text_blocks():
    provider = FakeStreamingProvider(events=[
        BlockStart(block_index=0, kind="text"),
        BlockStop(block_index=0),
        BlockStart(block_index=1, kind="tool_use", tool_id="t1", tool_name="x"),
        BlockStop(block_index=1),
        MessageStop(stop_reason="tool_use", usage=Usage()),
    ])
    req = NormalisedRequest(model="x", system="", max_tokens=1024)
    resp = await provider.create_message(req)
    # Empty text block elided; tool_use kept.
    assert [b["type"] for b in resp.content] == ["tool_use"]


@pytest.mark.asyncio
async def test_create_message_malformed_args_fallback():
    provider = FakeStreamingProvider(events=[
        BlockStart(block_index=0, kind="tool_use", tool_id="t1", tool_name="x"),
        BlockDelta(block_index=0, kind="tool_args", text='{"q":'),  # truncated JSON
        BlockStop(block_index=0),
        MessageStop(stop_reason="tool_use", usage=Usage()),
    ])
    req = NormalisedRequest(model="x", system="", max_tokens=1024)
    resp = await provider.create_message(req)
    # Fallback: raw string captured under _raw so dispatch can still see it.
    assert resp.content[0]["input"] == {"_raw": '{"q":'}
