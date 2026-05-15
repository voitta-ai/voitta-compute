"""Cancellation tests with deterministic pause points.

Uses FakeStreamingProvider's `pause_events` to suspend the stream at a
specific event index, then cancels the orchestrator's consumer task.
Asserts: the provider's `__aexit__` was called (clean upstream close),
no events emitted after cancel point, and CancelledError propagates.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app.routes.chat import ChatRequest, Message as RouteMessage, _stream
from app.services.llm.base import Usage
from app.services.llm.stream import (
    BlockStart,
    BlockDelta,
    BlockStop,
    MessageStop,
)
from tests.streaming.fake_provider import FakeStreamingProvider


def _install_fake(monkeypatch, fake):
    import app.routes.chat as chat_mod
    monkeypatch.setattr(chat_mod, "get_provider", lambda pid, key: fake)


def _request() -> ChatRequest:
    return ChatRequest(
        provider="anthropic",
        api_key="test-key",
        model="x",
        messages=[RouteMessage(role="user", content="hi")],
    )


async def _consume_until_event(gen, event_name: str, max_events: int = 50):
    """Drain `gen` until we see an SSE event of the named type; return
    that event and a list of preceding ones."""
    preceding: list[dict] = []
    async for raw in gen:
        ev = {"event": raw["event"], "data": json.loads(raw["data"])}
        if ev["event"] == event_name:
            return ev, preceding
        preceding.append(ev)
        if len(preceding) > max_events:
            pytest.fail(f"didn't see {event_name!r} within {max_events} events")
    pytest.fail(f"stream ended without {event_name!r}")


@pytest.mark.asyncio
async def test_cancellation_mid_text_calls_provider_aexit(monkeypatch):
    # Script: open text block, one delta, then pause forever, then we'd
    # have stopped — but we cancel before the BlockStop.
    script = [
        BlockStart(block_index=0, kind="text"),
        BlockDelta(block_index=0, kind="text", text="hello"),
        BlockStop(block_index=0),  # gated; we cancel before this fires
        MessageStop(stop_reason="end_turn", usage=Usage()),
    ]
    pauses = [asyncio.Event() for _ in script]
    # Release events 0 and 1; gate the rest.
    pauses[0].set()
    pauses[1].set()

    fake = FakeStreamingProvider(events=script, pause_events=pauses)
    _install_fake(monkeypatch, fake)

    gen = _stream(_request())
    received: list[dict] = []

    async def consumer():
        async for raw in gen:
            received.append({"event": raw["event"], "data": json.loads(raw["data"])})

    task = asyncio.create_task(consumer())

    # Wait until we observe at least the `start` event (then text is mid-stream).
    # The text BlockDelta is buffered server-side; we won't see the SSE
    # `delta` until BlockStop fires — which is gated. So the visible state
    # at the time of cancel is `start` only.
    for _ in range(50):
        await asyncio.sleep(0)
        if any(e["event"] == "start" for e in received):
            break
    assert any(e["event"] == "start" for e in received)

    # Cancel the consumer.
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Provider __aexit__ must have fired (clean upstream close).
    assert fake.aexit_count == 1

    # No `delta` was emitted (text block was never closed).
    names = [e["event"] for e in received]
    assert "delta" not in names
    assert "done" not in names


@pytest.mark.asyncio
async def test_cancellation_between_iterations(monkeypatch):
    """Cancel AFTER tool_use_end but before next iteration's stream opens."""
    from app.tools import registry
    from app.tools.registry import ToolSpec

    dispatch_gate = asyncio.Event()
    dispatched = []

    async def handler(args, ctx):
        dispatched.append(1)
        await dispatch_gate.wait()
        return {"result": "ok"}

    spec = ToolSpec(
        name="_cancel_tool",
        description="x",
        input_schema={"type": "object", "properties": {}, "required": []},
        handler=handler,
    )
    registry.register(spec)
    try:
        scripts = iter([
            [
                BlockStart(block_index=0, kind="tool_use", tool_id="t1", tool_name="_cancel_tool"),
                BlockDelta(block_index=0, kind="tool_args", text="{}"),
                BlockStop(block_index=0),
                MessageStop(stop_reason="tool_use", usage=Usage()),
            ],
            [
                BlockStart(block_index=0, kind="text"),
                BlockDelta(block_index=0, kind="text", text="done."),
                BlockStop(block_index=0),
                MessageStop(stop_reason="end_turn", usage=Usage()),
            ],
        ])

        fake = FakeStreamingProvider(script_factory=lambda req: next(scripts))
        _install_fake(monkeypatch, fake)

        gen = _stream(_request())
        received: list[dict] = []

        async def consumer():
            async for raw in gen:
                received.append({"event": raw["event"], "data": json.loads(raw["data"])})

        task = asyncio.create_task(consumer())

        # Wait until the handler has been called (we're mid-dispatch).
        for _ in range(200):
            await asyncio.sleep(0)
            if dispatched:
                break
        assert dispatched, "handler was never called"

        task.cancel()
        # Unblock the handler so it doesn't leak.
        dispatch_gate.set()
        with pytest.raises(asyncio.CancelledError):
            await task

        # First iteration's stream context manager exited cleanly.
        assert fake.aexit_count >= 1

        # We may have seen tool_use_start; we must NOT have seen `done`.
        names = [e["event"] for e in received]
        assert "done" not in names
    finally:
        registry._tools.pop("_cancel_tool", None)
