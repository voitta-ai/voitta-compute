"""Live turn telemetry for the SDK brain's status line.

The turn loop mutates one :class:`TurnStatus` (plain assignments, no
I/O); the 1 Hz ticker in ``run_agent_sdk_turn`` reads it and renders a
single line. That split preserves the status step's single-writer
invariant (only the ticker touches the Chainlit step) and inherently
debounces phase flapping — events can flip state a hundred times a
second, the line still updates once per second.

Data sources, in order of liveness:

* ``StreamEvent`` (``include_partial_messages=True``) — raw API stream
  events: thinking/text deltas while the model works, usage deltas.
  Best-effort: shapes are guarded, malformed events change nothing.
* Complete ``AssistantMessage`` blocks — authoritative fallback; if the
  CLI path ever stops emitting partials, phases still transition at
  message/tool boundaries and counters arrive stepwise.
* Tool use/result blocks — tool phase with name + start time + ordinal.

Thought tokens are an ESTIMATE (chars/4) — rendered with a ``~`` so the
line never claims precision it doesn't have. Reasoning text is counted
(len) and immediately discarded, never displayed or stored.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class TurnStatus:
    phase: str = "starting"  # starting | thinking | writing | tool | waiting
    # Output tokens, two-part: ``out_done`` sums the authoritative usage of
    # COMPLETED API calls in the agentic loop; ``out_stream`` is the live
    # (cumulative-within-call) count from message_delta events of the call
    # in flight. Display = done + stream; a completed message folds its
    # total into ``out_done`` and resets ``out_stream``.
    out_done: int = 0
    out_stream: int = 0
    think_chars: int = 0         # accumulated thinking-delta length
    in_tokens: int = 0           # final-summary only
    cache_tokens: int = 0        # final-summary only
    tool_name: str = ""
    tool_t0: float = 0.0
    tool_count: int = 0
    first_event_s: float | None = None
    _t0: float = field(default_factory=time.monotonic)
    _phase_t0: float = field(default_factory=time.monotonic)

    @property
    def out_tokens(self) -> int:
        return self.out_done + self.out_stream

    # -- mutation (called from the turn loop) --------------------------------

    def _set_phase(self, phase: str) -> None:
        if phase != self.phase:
            self.phase = phase
            self._phase_t0 = time.monotonic()

    def _mark_first_event(self) -> None:
        if self.first_event_s is None:
            self.first_event_s = round(time.monotonic() - self._t0, 1)

    def on_stream_event(self, raw: dict[str, Any]) -> None:
        """Consume one raw Anthropic stream event (from StreamEvent.event).
        Every access is guarded — a malformed event is a no-op."""
        try:
            etype = raw.get("type")
            if etype == "content_block_start":
                self._mark_first_event()
                btype = (raw.get("content_block") or {}).get("type")
                if btype == "thinking":
                    self._set_phase("thinking")
                elif btype == "text":
                    self._set_phase("writing")
                elif btype == "tool_use":
                    # The complete-message ToolUseBlock branch owns tool
                    # bookkeeping (name unprefixing etc.) — just flip phase.
                    self._set_phase("tool")
            elif etype == "content_block_delta":
                self._mark_first_event()
                delta = raw.get("delta") or {}
                dtype = delta.get("type")
                if dtype == "thinking_delta":
                    self._set_phase("thinking")
                    self.think_chars += len(delta.get("thinking") or "")
                elif dtype == "text_delta":
                    self._set_phase("writing")
            elif etype == "message_delta":
                usage = raw.get("usage") or {}
                out = usage.get("output_tokens")
                if isinstance(out, (int, float)):
                    # Cumulative within the in-flight call — take the max so
                    # out-of-order deltas can't run the counter backwards.
                    self.out_stream = max(self.out_stream, int(out))
        except Exception:
            pass  # telemetry must never hurt the turn

    def on_tool_start(self, name: str) -> None:
        self._mark_first_event()
        self.tool_count += 1
        self.tool_name = name
        self.tool_t0 = time.monotonic()
        self._set_phase("tool")

    def on_tool_result(self) -> None:
        # Model deliberates its next move after a tool returns.
        self._set_phase("thinking")

    def on_usage(self, usage: Any) -> None:
        """Authoritative per-call totals from a complete AssistantMessage:
        fold the finished call into ``out_done``, reset the live counter."""
        if not isinstance(usage, dict):
            return
        out = usage.get("output_tokens")
        if isinstance(out, (int, float)):
            self.out_done += max(int(out), self.out_stream)
        else:
            self.out_done += self.out_stream
        self.out_stream = 0
        for key, attr in (("input_tokens", "in_tokens"),
                          ("cache_read_input_tokens", "cache_tokens")):
            v = usage.get(key)
            if isinstance(v, (int, float)):
                setattr(self, attr, getattr(self, attr) + int(v))

    # -- rendering (called from the ticker) ----------------------------------

    @property
    def think_tokens_est(self) -> int:
        return self.think_chars // 4

    def line(self, spinner: str, waiting: bool) -> str:
        elapsed = int(time.monotonic() - self._t0)
        if waiting:
            tail = f" · {self.out_tokens:,} tok" if self.out_tokens else ""
            return f"❓ Waiting for your answer… · {elapsed}s{tail}"
        phase_s = int(time.monotonic() - self._phase_t0)
        if self.phase == "thinking":
            tail = (
                f" · ~{_compact(self.think_tokens_est)} thought"
                if self.think_tokens_est else ""
            )
            return f"{spinner} 🧠 thinking… · {elapsed}s{tail}"
        if self.phase == "tool":
            ordinal = f" · {_ordinal(self.tool_count)} call" if self.tool_count > 1 else ""
            name = self.tool_name or "tool"
            return f"{spinner} ⚙ {name} · {phase_s}s{ordinal}"
        if self.phase == "writing":
            rate = ""
            if self.out_tokens and elapsed >= 3:
                rate = f" · {self.out_tokens // max(1, elapsed)} tok/s"
            toks = f" · {self.out_tokens:,} tok" if self.out_tokens else ""
            return f"{spinner} ✍ writing… · {elapsed}s{toks}{rate}"
        toks = f" · {self.out_tokens:,} tok" if self.out_tokens else ""
        return f"{spinner} Working… · {elapsed}s{toks}"

    def summary(self) -> str:
        """End-of-turn log line: the full honest split."""
        bits = []
        if self.in_tokens:
            bits.append(f"in {_compact(self.in_tokens)}")
        if self.cache_tokens:
            bits.append(f"cache-read {_compact(self.cache_tokens)}")
        if self.out_tokens:
            bits.append(f"out {_compact(self.out_tokens)}")
        if self.think_tokens_est:
            bits.append(f"~{_compact(self.think_tokens_est)} thought")
        if self.tool_count:
            bits.append(f"{self.tool_count} tool{'s' if self.tool_count != 1 else ''}")
        if self.first_event_s is not None:
            bits.append(f"first event {self.first_event_s}s")
        return " · ".join(bits)


def _compact(n: int) -> str:
    if n >= 10_000:
        return f"{n / 1000:.0f}k"
    if n >= 1_000:
        return f"{n / 1000:.1f}k"
    return str(n)


def _ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"
