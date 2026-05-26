import asyncio

import pytest

from app.reports import render_events
from app.reports.render_events import RenderEvent


def _evt(slug: str, kind: str = "ready") -> RenderEvent:
    return RenderEvent(slug=slug, kind=kind)


def test_record_then_recent(render_state):
    render_events.record(_evt("a", "ready"))
    render_events.record(_evt("a", "error"))
    items = render_events.recent("a")
    assert [e.kind for e in items] == ["ready", "error"]


def test_recent_since_ts_filters(render_state):
    render_events.record(_evt("s", "info"))
    items = render_events.recent("s")
    pivot = items[0].ts
    render_events.record(_evt("s", "ready"))
    later = render_events.recent("s", since_ts=pivot)
    assert [e.kind for e in later] == ["ready"]


def test_ring_size_cap(render_state):
    # Use varying messages so dedup doesn't collapse them into one entry.
    for i in range(render_events.RING_SIZE + 5):
        render_events.record(RenderEvent(slug="cap", kind="info", message=f"m{i}"))
    assert len(render_events.recent("cap", limit=999)) == render_events.RING_SIZE


def test_disk_log_capped(render_state):
    """Past MAX_LOG_LINES the head should be trimmed."""
    cap = render_events.MAX_LOG_LINES
    for i in range(cap + 10):
        render_events.record(RenderEvent(slug="big", kind="error", message=f"err{i}"))
    on_disk = render_events.read_log("big", limit=cap * 2)
    assert len(on_disk) == cap


def test_inventory_roundtrip(render_state):
    render_events.record_inventory("inv", {"series": 3, "title": "x"})
    got = render_events.read_inventory("inv")
    assert got == {"series": 3, "title": "x"}


def test_inventory_missing_returns_none(render_state):
    assert render_events.read_inventory("nope") is None


@pytest.mark.asyncio
async def test_wait_for_resolved_by_record(render_state):
    async def deliver():
        await asyncio.sleep(0.01)
        render_events.record(_evt("w", "ready"))

    asyncio.create_task(deliver())
    got = await render_events.wait_for("w", timeout=1.0)
    assert got is not None and got.kind == "ready"


@pytest.mark.asyncio
async def test_wait_for_timeout(render_state):
    got = await render_events.wait_for("idle", timeout=0.05)
    assert got is None


def test_dedup_consecutive_identical(render_state):
    """Same slug+kind+message → counter bumps; ring size stays 1."""
    for _ in range(5):
        render_events.record(
            RenderEvent(slug="dup", kind="error", message="canvas crashed")
        )
    items = render_events.recent("dup")
    assert len(items) == 1
    assert items[0].detail["count"] == 5


def test_dedup_only_consecutive(render_state):
    """A different message breaks the dedup streak."""
    render_events.record(RenderEvent(slug="x", kind="error", message="A"))
    render_events.record(RenderEvent(slug="x", kind="error", message="A"))
    render_events.record(RenderEvent(slug="x", kind="error", message="B"))
    render_events.record(RenderEvent(slug="x", kind="error", message="A"))
    items = render_events.recent("x")
    assert [e.message for e in items] == ["A", "B", "A"]
    assert items[0].detail.get("count") == 2


def test_dedup_disk_log_not_appended(render_state):
    """Repeats stay out of the on-disk log too."""
    for _ in range(10):
        render_events.record(RenderEvent(slug="d", kind="error", message="x"))
    on_disk = render_events.read_log("d", limit=100)
    assert len(on_disk) == 1
