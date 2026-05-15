"""Anthropic adapter `stream()` normalisation tests.

Builds a fake Anthropic event sequence (matching `messages.stream()`'s
SSE schema) and asserts the adapter yields the correct sequence of
normalised StreamEvents.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.services.llm.anthropic import _AnthropicStreamCM
from app.services.llm.stream import (
    BlockStart,
    BlockDelta,
    BlockStop,
    MessageStop,
)


def _ev(**kwargs) -> SimpleNamespace:
    """Build a fake Anthropic event with attribute access."""
    return SimpleNamespace(**kwargs)


async def _aiter(events: list):
    for e in events:
        yield e


@pytest.mark.asyncio
async def test_text_only_turn():
    raw_events = [
        _ev(
            type="message_start",
            message=_ev(
                usage=_ev(
                    input_tokens=10,
                    output_tokens=0,
                    cache_read_input_tokens=0,
                    cache_creation_input_tokens=0,
                )
            ),
        ),
        _ev(type="content_block_start", index=0, content_block=_ev(type="text")),
        _ev(type="content_block_delta", index=0, delta=_ev(type="text_delta", text="Hello ")),
        _ev(type="content_block_delta", index=0, delta=_ev(type="text_delta", text="world")),
        _ev(type="content_block_stop", index=0),
        _ev(type="message_delta", delta=_ev(stop_reason="end_turn"), usage=_ev(output_tokens=2)),
        _ev(type="message_stop"),
    ]
    out = []
    async for ev in _AnthropicStreamCM._iter_events(_aiter(raw_events)):
        out.append(ev)

    assert [type(e).__name__ for e in out] == [
        "BlockStart", "BlockDelta", "BlockDelta", "BlockStop", "MessageStop",
    ]
    assert out[0] == BlockStart(block_index=0, kind="text")
    assert out[1].kind == "text" and out[1].text == "Hello "
    assert out[-1].stop_reason == "end_turn"
    assert out[-1].usage.input_tokens == 10
    assert out[-1].usage.output_tokens == 2


@pytest.mark.asyncio
async def test_tool_use_with_streaming_args():
    """The core UX case: tool name lands at content_block_start while
    args still stream in via input_json_delta chunks."""
    raw_events = [
        _ev(type="message_start", message=_ev(usage=_ev(input_tokens=20, output_tokens=0))),
        _ev(type="content_block_start", index=0,
            content_block=_ev(type="tool_use", id="toolu_1", name="search")),
        _ev(type="content_block_delta", index=0,
            delta=_ev(type="input_json_delta", partial_json='{"q":')),
        _ev(type="content_block_delta", index=0,
            delta=_ev(type="input_json_delta", partial_json='"hello"}')),
        _ev(type="content_block_stop", index=0),
        _ev(type="message_delta", delta=_ev(stop_reason="tool_use"), usage=_ev(output_tokens=5)),
        _ev(type="message_stop"),
    ]
    out = []
    async for ev in _AnthropicStreamCM._iter_events(_aiter(raw_events)):
        out.append(ev)

    assert isinstance(out[0], BlockStart)
    assert out[0].kind == "tool_use"
    assert out[0].tool_id == "toolu_1"
    assert out[0].tool_name == "search"
    args_deltas = [e for e in out if isinstance(e, BlockDelta) and e.kind == "tool_args"]
    assert "".join(d.text for d in args_deltas) == '{"q":"hello"}'
    assert isinstance(out[-1], MessageStop)
    assert out[-1].stop_reason == "tool_use"


@pytest.mark.asyncio
async def test_text_then_tool_use_in_one_turn():
    raw_events = [
        _ev(type="message_start", message=_ev(usage=_ev(input_tokens=15))),
        _ev(type="content_block_start", index=0, content_block=_ev(type="text")),
        _ev(type="content_block_delta", index=0, delta=_ev(type="text_delta", text="checking")),
        _ev(type="content_block_stop", index=0),
        _ev(type="content_block_start", index=1,
            content_block=_ev(type="tool_use", id="toolu_2", name="lookup")),
        _ev(type="content_block_delta", index=1,
            delta=_ev(type="input_json_delta", partial_json='{}')),
        _ev(type="content_block_stop", index=1),
        _ev(type="message_delta", delta=_ev(stop_reason="tool_use")),
        _ev(type="message_stop"),
    ]
    out = []
    async for ev in _AnthropicStreamCM._iter_events(_aiter(raw_events)):
        out.append(ev)

    kinds = [(type(e).__name__, getattr(e, "kind", None), getattr(e, "block_index", None))
             for e in out]
    assert kinds == [
        ("BlockStart", "text", 0),
        ("BlockDelta", "text", 0),
        ("BlockStop", None, 0),
        ("BlockStart", "tool_use", 1),
        ("BlockDelta", "tool_args", 1),
        ("BlockStop", None, 1),
        ("MessageStop", None, None),
    ]
