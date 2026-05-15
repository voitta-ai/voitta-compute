"""OpenAI adapter `stream()` normalisation tests.

The OpenAI Responses API emits semantic events:
  response.output_item.added            (new item: message or function_call)
  response.output_text.delta            (text fragment)
  response.function_call_arguments.delta (JSON arg fragment)
  response.output_item.done             (item closed)
  response.completed                    (terminal; carries usage)

Tests assert our adapter assigns `next_block_index` correctly and maps
the per-item index map back to our block_index.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.llm.openai import _OpenAIStreamCM
from app.services.llm.stream import (
    BlockStart,
    BlockDelta,
    BlockStop,
    MessageStop,
)


def _ev(**kwargs) -> SimpleNamespace:
    return SimpleNamespace(**kwargs)


async def _aiter(events):
    for e in events:
        yield e


@pytest.mark.asyncio
async def test_text_only_message():
    raw = [
        _ev(type="response.output_item.added", output_index=0, item=_ev(type="message")),
        _ev(type="response.output_text.delta", output_index=0, delta="Hello "),
        _ev(type="response.output_text.delta", output_index=0, delta="world"),
        _ev(type="response.output_item.done", output_index=0),
        _ev(
            type="response.completed",
            response=_ev(
                usage=_ev(input_tokens=10, output_tokens=2,
                          input_tokens_details=_ev(cached_tokens=0)),
                incomplete_details=None,
            ),
        ),
    ]
    out = []
    async for ev in _OpenAIStreamCM._iter_events(_aiter(raw)):
        out.append(ev)
    assert [type(e).__name__ for e in out] == [
        "BlockStart", "BlockDelta", "BlockDelta", "BlockStop", "MessageStop",
    ]
    assert out[0].kind == "text" and out[0].block_index == 0
    assert "".join(e.text for e in out if isinstance(e, BlockDelta)) == "Hello world"
    assert out[-1].stop_reason == "end_turn"
    assert out[-1].usage.input_tokens == 10
    assert out[-1].usage.output_tokens == 2


@pytest.mark.asyncio
async def test_tool_use_args_stream_incrementally():
    raw = [
        _ev(
            type="response.output_item.added",
            output_index=0,
            item=_ev(type="function_call", call_id="call_1", name="search"),
        ),
        _ev(type="response.function_call_arguments.delta", output_index=0, delta='{"q":'),
        _ev(type="response.function_call_arguments.delta", output_index=0, delta='"hi"}'),
        _ev(type="response.output_item.done", output_index=0),
        _ev(
            type="response.completed",
            response=_ev(
                usage=_ev(input_tokens=5, output_tokens=1,
                          input_tokens_details=_ev(cached_tokens=0)),
                incomplete_details=None,
            ),
        ),
    ]
    out = []
    async for ev in _OpenAIStreamCM._iter_events(_aiter(raw)):
        out.append(ev)
    assert isinstance(out[0], BlockStart)
    assert out[0].kind == "tool_use"
    assert out[0].tool_id == "call_1"
    assert out[0].tool_name == "search"
    args_text = "".join(e.text for e in out if isinstance(e, BlockDelta))
    assert args_text == '{"q":"hi"}'
    assert out[-1].stop_reason == "tool_use"


@pytest.mark.asyncio
async def test_text_then_tool_use():
    """Two output items: message (output_index=0) then function_call (output_index=1).
    Our block_index counter should map them to 0 and 1 respectively."""
    raw = [
        _ev(type="response.output_item.added", output_index=0, item=_ev(type="message")),
        _ev(type="response.output_text.delta", output_index=0, delta="ok"),
        _ev(type="response.output_item.done", output_index=0),
        _ev(
            type="response.output_item.added",
            output_index=1,
            item=_ev(type="function_call", call_id="call_2", name="x"),
        ),
        _ev(type="response.function_call_arguments.delta", output_index=1, delta="{}"),
        _ev(type="response.output_item.done", output_index=1),
        _ev(type="response.completed", response=_ev(usage=None, incomplete_details=None)),
    ]
    out = []
    async for ev in _OpenAIStreamCM._iter_events(_aiter(raw)):
        out.append(ev)
    starts = [e for e in out if isinstance(e, BlockStart)]
    assert [(s.block_index, s.kind) for s in starts] == [(0, "text"), (1, "tool_use")]
    assert out[-1].stop_reason == "tool_use"


@pytest.mark.asyncio
async def test_max_output_tokens_incomplete():
    raw = [
        _ev(type="response.output_item.added", output_index=0, item=_ev(type="message")),
        _ev(type="response.output_text.delta", output_index=0, delta="partial"),
        _ev(type="response.output_item.done", output_index=0),
        _ev(
            type="response.completed",
            response=_ev(
                usage=None,
                incomplete_details=_ev(reason="max_output_tokens"),
            ),
        ),
    ]
    out = []
    async for ev in _OpenAIStreamCM._iter_events(_aiter(raw)):
        out.append(ev)
    assert out[-1].stop_reason == "max_tokens"
