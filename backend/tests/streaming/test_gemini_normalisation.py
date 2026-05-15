"""Gemini adapter `stream()` normalisation tests.

Builds fake `generate_content_stream` chunks and asserts the adapter
yields the correct normalised StreamEvent sequence. Key Gemini specifics:
- consecutive text Parts coalesce into one logical block
- function_call Parts arrive complete in one chunk → emit
  BlockStart + BlockDelta(tool_args) + BlockStop back-to-back
- terminal MessageStop always emitted with usage from `usage_metadata`
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.services.llm.gemini import _GeminiStreamCM
from app.services.llm.stream import (
    BlockStart,
    BlockDelta,
    BlockStop,
    MessageStop,
)


def _chunk(parts: list, finish_reason: str = "", usage: dict | None = None) -> SimpleNamespace:
    """Build a fake Gemini chunk."""
    return SimpleNamespace(
        candidates=[
            SimpleNamespace(
                content=SimpleNamespace(parts=[SimpleNamespace(**p) for p in parts]),
                finish_reason=finish_reason or None,
            )
        ],
        usage_metadata=SimpleNamespace(**usage) if usage else None,
    )


async def _aiter(chunks: list):
    for c in chunks:
        yield c


@pytest.mark.asyncio
async def test_text_only_single_chunk():
    chunks = [
        _chunk(
            parts=[{"text": "Hello world"}],
            finish_reason="STOP",
            usage={"prompt_token_count": 10, "candidates_token_count": 2, "cached_content_token_count": 0},
        ),
    ]
    out = []
    async for ev in _GeminiStreamCM._iter_events(_aiter(chunks)):
        out.append(ev)
    # Expected: BlockStart(text), BlockDelta(text), BlockStop, MessageStop
    assert [type(e).__name__ for e in out] == ["BlockStart", "BlockDelta", "BlockStop", "MessageStop"]
    assert out[1].text == "Hello world"
    assert out[-1].stop_reason == "end_turn"
    assert out[-1].usage.input_tokens == 10
    assert out[-1].usage.output_tokens == 2


@pytest.mark.asyncio
async def test_text_coalesces_across_chunks():
    chunks = [
        _chunk(parts=[{"text": "Hello "}]),
        _chunk(parts=[{"text": "world"}], finish_reason="STOP"),
    ]
    out = []
    async for ev in _GeminiStreamCM._iter_events(_aiter(chunks)):
        out.append(ev)
    # Both deltas land on block_index=0; only one BlockStart and one BlockStop.
    starts = [e for e in out if isinstance(e, BlockStart)]
    stops = [e for e in out if isinstance(e, BlockStop)]
    deltas = [e for e in out if isinstance(e, BlockDelta)]
    assert len(starts) == 1 and starts[0].block_index == 0 and starts[0].kind == "text"
    assert len(stops) == 1 and stops[0].block_index == 0
    assert len(deltas) == 2
    assert "".join(d.text for d in deltas) == "Hello world"


@pytest.mark.asyncio
async def test_function_call_in_one_chunk():
    """Per empirical probe, Gemini delivers function_call complete in one chunk."""
    fc_part = {
        "function_call": SimpleNamespace(name="create_report", args={"title": "T", "body": "B" * 1000})
    }
    # NOTE: SimpleNamespace inside a dict — _GeminiStreamCM uses getattr on parts.
    # Convert: the part itself must support getattr access.
    chunks = [
        SimpleNamespace(
            candidates=[
                SimpleNamespace(
                    content=SimpleNamespace(parts=[
                        SimpleNamespace(text=None, function_call=fc_part["function_call"]),
                    ]),
                    finish_reason="STOP",
                )
            ],
            usage_metadata=SimpleNamespace(
                prompt_token_count=100,
                candidates_token_count=5,
                cached_content_token_count=0,
            ),
        ),
    ]
    out = []
    async for ev in _GeminiStreamCM._iter_events(_aiter(chunks)):
        out.append(ev)
    # Expected: BlockStart(tool_use), BlockDelta(tool_args), BlockStop, MessageStop
    assert [type(e).__name__ for e in out] == ["BlockStart", "BlockDelta", "BlockStop", "MessageStop"]
    bs = out[0]
    assert bs.kind == "tool_use"
    assert bs.tool_name == "create_report"
    assert bs.tool_id and bs.tool_id.startswith("gemini_call_")
    bd = out[1]
    assert bd.kind == "tool_args"
    parsed = json.loads(bd.text)
    assert parsed == {"title": "T", "body": "B" * 1000}
    assert out[-1].stop_reason == "tool_use"


@pytest.mark.asyncio
async def test_text_then_function_call_in_same_chunk():
    """If a chunk contains [text, function_call], the text block must be
    closed before the tool_use block opens."""
    chunks = [
        SimpleNamespace(
            candidates=[
                SimpleNamespace(
                    content=SimpleNamespace(parts=[
                        SimpleNamespace(text="checking…", function_call=None),
                        SimpleNamespace(text=None, function_call=SimpleNamespace(name="x", args={"a": 1})),
                    ]),
                    finish_reason="STOP",
                )
            ],
            usage_metadata=None,
        ),
    ]
    out = []
    async for ev in _GeminiStreamCM._iter_events(_aiter(chunks)):
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
    assert out[-1].stop_reason == "tool_use"


@pytest.mark.asyncio
async def test_max_tokens_finish():
    chunks = [
        _chunk(parts=[{"text": "partial"}], finish_reason="MAX_TOKENS"),
    ]
    out = []
    async for ev in _GeminiStreamCM._iter_events(_aiter(chunks)):
        out.append(ev)
    assert out[-1].stop_reason == "max_tokens"
