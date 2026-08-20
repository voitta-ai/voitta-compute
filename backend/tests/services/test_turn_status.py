"""TurnStatus: phase transitions, token accounting (done+stream split),
thought estimate, malformed-event tolerance, and line rendering."""

from __future__ import annotations

from app.services.agent_sdk.turn_status import TurnStatus, _compact, _ordinal


def _sev(t: TurnStatus, **raw) -> None:
    t.on_stream_event(raw)


def test_phase_transitions_from_stream_events():
    t = TurnStatus()
    assert t.phase == "starting"
    _sev(t, type="content_block_start", content_block={"type": "thinking"})
    assert t.phase == "thinking"
    _sev(t, type="content_block_delta", delta={"type": "thinking_delta", "thinking": "x" * 400})
    assert t.think_chars == 400
    assert t.think_tokens_est == 100
    _sev(t, type="content_block_delta", delta={"type": "text_delta", "text": "hi"})
    assert t.phase == "writing"
    _sev(t, type="content_block_start", content_block={"type": "tool_use"})
    assert t.phase == "tool"


def test_tool_lifecycle():
    t = TurnStatus()
    t.on_tool_start("drive_search")
    assert (t.phase, t.tool_name, t.tool_count) == ("tool", "drive_search", 1)
    t.on_tool_result()
    assert t.phase == "thinking"          # deliberating the next move
    t.on_tool_start("sheets_read_range")
    assert t.tool_count == 2


def test_output_token_done_stream_split():
    t = TurnStatus()
    # Live deltas are cumulative within one call; max() guards reordering.
    _sev(t, type="message_delta", usage={"output_tokens": 10})
    _sev(t, type="message_delta", usage={"output_tokens": 50})
    _sev(t, type="message_delta", usage={"output_tokens": 40})  # out-of-order
    assert t.out_tokens == 50
    # Complete message folds into done and resets the live counter.
    t.on_usage({"output_tokens": 60, "input_tokens": 1200, "cache_read_input_tokens": 40000})
    assert t.out_tokens == 60
    assert (t.in_tokens, t.cache_tokens) == (1200, 40000)
    # Second call in the agentic loop accumulates.
    _sev(t, type="message_delta", usage={"output_tokens": 5})
    assert t.out_tokens == 65
    t.on_usage({"output_tokens": 8})
    assert t.out_tokens == 68


def test_usage_without_stream_still_counts():
    # Degraded mode (no partials): only complete messages arrive.
    t = TurnStatus()
    t.on_usage({"output_tokens": 100})
    t.on_usage({"output_tokens": 250})
    assert t.out_tokens == 350


def test_malformed_events_are_noops():
    t = TurnStatus()
    for raw in ({}, {"type": "bogus"}, {"type": "content_block_delta"},
                {"type": "content_block_delta", "delta": None},
                {"type": "message_delta", "usage": {"output_tokens": "NaN"}},
                {"type": "content_block_start", "content_block": "not-a-dict"}):
        t.on_stream_event(raw)  # must not raise
    t.on_usage(None)
    t.on_usage("garbage")
    assert t.phase in ("starting", "thinking")  # nothing meaningful changed
    assert t.out_tokens == 0


def test_first_event_marked_once():
    t = TurnStatus()
    assert t.first_event_s is None
    _sev(t, type="content_block_start", content_block={"type": "thinking"})
    first = t.first_event_s
    assert first is not None
    t.on_tool_start("x")
    assert t.first_event_s == first


def test_line_rendering_per_phase():
    t = TurnStatus()
    assert "Working…" in t.line("⠋", waiting=False)
    _sev(t, type="content_block_start", content_block={"type": "thinking"})
    _sev(t, type="content_block_delta", delta={"type": "thinking_delta", "thinking": "x" * 8400})
    line = t.line("⠋", waiting=False)
    assert "🧠 thinking…" in line and "~2.1k thought" in line
    t.on_tool_start("drive_search")
    t.on_tool_start("drive_search")
    line = t.line("⠋", waiting=False)
    assert "⚙ drive_search" in line and "2nd call" in line
    _sev(t, type="content_block_delta", delta={"type": "text_delta", "text": "hi"})
    _sev(t, type="message_delta", usage={"output_tokens": 500})
    line = t.line("⠋", waiting=False)
    assert "✍ writing…" in line and "500 tok" in line
    # Waiting overrides everything.
    assert t.line("⠋", waiting=True).startswith("❓ Waiting")


def test_summary_line():
    t = TurnStatus()
    t.on_usage({"output_tokens": 812, "input_tokens": 1204, "cache_read_input_tokens": 47000})
    t.on_tool_start("a")
    _sev(t, type="content_block_delta", delta={"type": "thinking_delta", "thinking": "x" * 4000})
    s = t.summary()
    assert "in 1.2k" in s and "cache-read 47k" in s and "out 812" in s
    assert "~1.0k thought" in s and "1 tool" in s
    assert TurnStatus().summary() == ""


def test_compact_and_ordinal():
    assert _compact(999) == "999"
    assert _compact(1500) == "1.5k"
    assert _compact(47000) == "47k"
    assert [_ordinal(n) for n in (1, 2, 3, 4, 11, 12, 13, 21)] == [
        "1st", "2nd", "3rd", "4th", "11th", "12th", "13th", "21st",
    ]
