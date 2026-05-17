"""Orchestrator (_stream) tests against a scripted FakeStreamingProvider.

Exercises:
- start → delta → done event sequence
- tool_use_start emitted on BlockStart, before tool dispatch
- seq monotonicity
- multi-iteration tool-use loop
- usage accumulation across iterations
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from app.routes.chat import ChatRequest, Message as RouteMessage, _stream
from app.services.llm.base import Usage
from app.services.llm.stream import (
    BlockStart,
    BlockDelta,
    BlockStop,
    MessageStop,
)
from app.tools import registry
from app.tools.registry import ToolSpec, ToolResult
from tests.streaming.fake_provider import FakeStreamingProvider


# ---- helpers ----------------------------------------------------------


def _install_fake_provider(monkeypatch, provider: FakeStreamingProvider, provider_id: str = "anthropic"):
    """Patch get_provider so the orchestrator receives our fake."""
    import app.routes.chat as chat_mod

    def _fake_get_provider(pid, key):
        return provider

    monkeypatch.setattr(chat_mod, "get_provider", _fake_get_provider)


async def _drain(req: ChatRequest) -> list[dict]:
    """Drain the SSE generator into a list of parsed events."""
    out: list[dict] = []
    async for raw in _stream(req):
        out.append({"event": raw["event"], "data": json.loads(raw["data"])})
    return out


def _make_request(messages_text: str = "hi") -> ChatRequest:
    return ChatRequest(
        provider="anthropic",
        api_key="test-key",
        model="claude-sonnet-test",
        messages=[RouteMessage(role="user", content=messages_text)],
    )


# ---- tests ------------------------------------------------------------


@pytest.mark.asyncio
async def test_text_only_turn(monkeypatch):
    fake = FakeStreamingProvider(events=[
        BlockStart(block_index=0, kind="text"),
        BlockDelta(block_index=0, kind="text", text="Hello there."),
        BlockStop(block_index=0),
        MessageStop(stop_reason="end_turn", usage=Usage(input_tokens=10, output_tokens=3)),
    ])
    _install_fake_provider(monkeypatch, fake)

    events = await _drain(_make_request())
    names = [e["event"] for e in events]
    assert names == ["start", "delta", "done"]

    # seq monotonic
    seqs = [e["data"]["seq"] for e in events]
    assert seqs == sorted(seqs)
    assert seqs == [0, 1, 2]

    # delta has correct shape
    delta = events[1]
    assert delta["data"]["text"] == "Hello there."
    assert delta["data"]["block_index"] == 0
    assert delta["data"]["iteration"] == 0

    # done has accumulated usage
    done = events[2]
    assert done["data"]["usage"]["input_tokens"] == 10
    assert done["data"]["usage"]["output_tokens"] == 3
    assert done["data"]["iterations"] == 1


@pytest.mark.asyncio
async def test_tool_use_start_fires_before_dispatch(monkeypatch):
    """tool_use_start must appear in SSE order BEFORE the tool dispatch
    starts (i.e. before tool_use_end). This is the core UX win."""

    # Register a dummy tool.
    dispatched: list[str] = []

    async def handler(args, ctx):
        dispatched.append("dispatched")
        return {"result": "ok"}

    spec = ToolSpec(
        name="_test_tool",
        description="test",
        input_schema={"type": "object", "properties": {}, "required": []},
        handler=handler,
    )
    registry.register(spec)
    try:
        # Iter 0: emits tool_use, asks for tool. Iter 1: emits text, done.
        scripts = iter([
            [
                BlockStart(block_index=0, kind="tool_use", tool_id="t1", tool_name="_test_tool"),
                BlockDelta(block_index=0, kind="tool_args", text="{}"),
                BlockStop(block_index=0),
                MessageStop(stop_reason="tool_use", usage=Usage(input_tokens=5, output_tokens=2)),
            ],
            [
                BlockStart(block_index=0, kind="text"),
                BlockDelta(block_index=0, kind="text", text="done."),
                BlockStop(block_index=0),
                MessageStop(stop_reason="end_turn", usage=Usage(input_tokens=8, output_tokens=1)),
            ],
        ])

        fake = FakeStreamingProvider(script_factory=lambda req: next(scripts))
        _install_fake_provider(monkeypatch, fake)

        events = await _drain(_make_request())
        names = [e["event"] for e in events]
        # Expected: start, tool_use_start, tool_args_delta, tool_use_end, delta, done
        assert names == [
            "start",
            "tool_use_start",
            "tool_args_delta",
            "tool_use_end",
            "delta",
            "done",
        ]

        # tool_use_start strictly precedes tool_use_end
        idx_start = names.index("tool_use_start")
        idx_end = names.index("tool_use_end")
        assert idx_start < idx_end

        # tool_args_delta carries cumulative char count
        targs = events[names.index("tool_args_delta")]["data"]
        assert targs["chars"] == len("{}")
        assert targs["id"] == "t1"
        assert targs["block_index"] == 0

        # Usage is accumulated across both iterations
        done = events[-1]["data"]
        assert done["usage"]["input_tokens"] == 13   # 5 + 8
        assert done["usage"]["output_tokens"] == 3   # 2 + 1
        assert done["iterations"] == 2

        # Tool was actually dispatched
        assert dispatched == ["dispatched"]
    finally:
        registry._tools.pop("_test_tool", None)


@pytest.mark.asyncio
async def test_seq_monotonic_across_events(monkeypatch):
    fake = FakeStreamingProvider(events=[
        BlockStart(block_index=0, kind="text"),
        BlockDelta(block_index=0, kind="text", text="a"),
        BlockStop(block_index=0),
        BlockStart(block_index=1, kind="text"),
        BlockDelta(block_index=1, kind="text", text="b"),
        BlockStop(block_index=1),
        MessageStop(stop_reason="end_turn", usage=Usage()),
    ])
    _install_fake_provider(monkeypatch, fake)
    events = await _drain(_make_request())
    seqs = [e["data"]["seq"] for e in events]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs)  # unique


@pytest.mark.asyncio
async def test_empty_text_blocks_not_emitted_as_delta(monkeypatch):
    """A text block that received no BlockDelta should NOT produce a
    `delta` SSE event."""
    fake = FakeStreamingProvider(events=[
        BlockStart(block_index=0, kind="text"),
        BlockStop(block_index=0),
        MessageStop(stop_reason="end_turn", usage=Usage()),
    ])
    _install_fake_provider(monkeypatch, fake)
    events = await _drain(_make_request())
    names = [e["event"] for e in events]
    assert names == ["start", "done"]
