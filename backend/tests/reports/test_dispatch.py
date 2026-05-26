"""Dispatcher tests — HTML-only path.

``run_and_dispatch`` is the e2e entry. We mock Chainlit's
``CopilotFunction`` since the test harness can't stand up a
real socket session.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from app.reports import dispatch, render_events, store
from app.reports.render_events import RenderEvent


@pytest.fixture
def patched_chainlit(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Replace cl.CopilotFunction(...).acall() with an AsyncMock and
    return the mock so tests can assert call args."""
    acall = AsyncMock(return_value=None)

    class FakeCopilotFunction:
        def __init__(self, name: str, args: dict[str, Any]) -> None:
            self.name = name
            self.args = args

        def acall(self) -> AsyncMock:  # type: ignore[override]
            return acall(self.name, self.args)

    import chainlit as cl

    monkeypatch.setattr(cl, "CopilotFunction", FakeCopilotFunction)
    monkeypatch.setattr(
        dispatch.cl, "CopilotFunction", FakeCopilotFunction, raising=True
    )
    return acall


@pytest.mark.asyncio
async def test_run_and_dispatch_html_happy(
    scripts_root, render_state, patched_chainlit,
):
    """A script returning a raw HTML string → render + cache + ship via
    show_html_report call_fn → await ready event."""
    code = (
        "def build(ctx):\n"
        "    return '<!doctype html><html><body><h1>hi</h1></body></html>'\n"
    )
    store.write_script("hello", code)

    async def simulate_fe():
        import asyncio
        await asyncio.sleep(0.01)
        render_events.record(RenderEvent(slug="hello", kind="ready"))

    import asyncio
    asyncio.create_task(simulate_fe())

    result = await dispatch.run_and_dispatch("hello", wait_s=1.0)
    assert result.ok, result
    assert result.kind == "html"
    assert result.status == "ready"
    patched_chainlit.assert_called_once()
    name, args = patched_chainlit.call_args.args
    assert name == "show_html_report"
    assert args["kind"] == "html"
    assert "/api/html-report" in args["url"]


@pytest.mark.asyncio
async def test_run_and_dispatch_no_render(
    scripts_root, render_state, patched_chainlit,
):
    """build() returns None + emits only inline → no call_fn, status=no-render."""
    code = "def build(ctx):\n    ctx.text('hi')\n    return None\n"
    store.write_script("inline", code)

    import chainlit as cl
    with patch.object(cl, "Message") as mock_msg:
        mock_msg.return_value.send = AsyncMock()
        result = await dispatch.run_and_dispatch("inline")

    assert result.ok and result.status == "no-render"
    patched_chainlit.assert_not_called()


@pytest.mark.asyncio
async def test_run_and_dispatch_non_string_return(
    scripts_root, render_state, patched_chainlit,
):
    """Anything that isn't None or a string → error."""
    code = "def build(ctx):\n    return {'not': 'html'}\n"
    store.write_script("bad", code)
    result = await dispatch.run_and_dispatch("bad")
    assert not result.ok
    assert result.status == "error"
    assert "string" in (result.error or "").lower()
    patched_chainlit.assert_not_called()


@pytest.mark.asyncio
async def test_run_and_dispatch_empty_string(
    scripts_root, render_state, patched_chainlit,
):
    """Empty string return → error."""
    code = "def build(ctx):\n    return '   '\n"
    store.write_script("empty", code)
    result = await dispatch.run_and_dispatch("empty")
    assert not result.ok
    assert result.status == "error"
    assert "empty" in (result.error or "").lower()


@pytest.mark.asyncio
async def test_run_and_dispatch_script_error(
    scripts_root, render_state, patched_chainlit,
):
    code = "def build(ctx):\n    raise ValueError('boom')\n"
    store.write_script("boom", code)
    result = await dispatch.run_and_dispatch("boom")
    assert not result.ok
    assert result.status == "error"
    assert "ValueError" in (result.error or "")


@pytest.mark.asyncio
async def test_run_and_dispatch_timeout(
    scripts_root, render_state, patched_chainlit,
):
    code = "def build(ctx):\n    return '<!doctype html><html></html>'\n"
    store.write_script("noevent", code)
    # No FE event posted → wait_for returns None.
    result = await dispatch.run_and_dispatch("noevent", wait_s=0.05)
    assert not result.ok
    assert result.status == "timeout"
