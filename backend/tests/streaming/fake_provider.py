"""Fake streaming provider for tests.

Yields a scripted list of StreamEvent objects from `stream()`. Optionally
pauses on each event via an asyncio.Event for deterministic cancellation
tests.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator, Callable

from app.services.llm.base import BaseProvider, NormalisedRequest
from app.services.llm.stream import StreamEvent


class FakeStreamingProvider(BaseProvider):
    """Replays a scripted sequence of StreamEvent objects.

    If `script_factory` is provided, it is called per-iteration to build
    the next script. Otherwise the constructor `events` is yielded once.
    Use `pause_events: list[asyncio.Event]` (parallel to the script) to
    suspend yielding at deterministic points — pause_events[i] gates
    yielding events[i] (the test must `.set()` it to allow progress).
    """

    id = "fake"

    def __init__(
        self,
        events: list[StreamEvent] | None = None,
        script_factory: Callable[[NormalisedRequest], list[StreamEvent]] | None = None,
        pause_events: list[asyncio.Event] | None = None,
        on_aexit: Callable[[], None] | None = None,
    ) -> None:
        self._events = events
        self._script_factory = script_factory
        self._pause_events = pause_events
        self._on_aexit = on_aexit
        self.captured_requests: list[NormalisedRequest] = []
        self.aexit_count = 0

    def stream(self, req: NormalisedRequest):
        self.captured_requests.append(req)
        script = (
            self._script_factory(req)
            if self._script_factory is not None
            else list(self._events or [])
        )
        return _FakeStreamCM(
            script,
            self._pause_events,
            self._note_aexit,
        )

    def _note_aexit(self) -> None:
        self.aexit_count += 1
        if self._on_aexit is not None:
            self._on_aexit()


class _FakeStreamCM:
    def __init__(
        self,
        script: list[StreamEvent],
        pause_events: list[asyncio.Event] | None,
        on_aexit: Callable[[], None],
    ) -> None:
        self._script = script
        self._pause_events = pause_events
        self._on_aexit = on_aexit

    async def __aenter__(self) -> AsyncIterator[StreamEvent]:
        return self._iter()

    async def __aexit__(self, exc_type, exc, tb):
        self._on_aexit()
        return False

    async def _iter(self) -> AsyncIterator[StreamEvent]:
        for i, ev in enumerate(self._script):
            if self._pause_events is not None and i < len(self._pause_events):
                await self._pause_events[i].wait()
            yield ev
