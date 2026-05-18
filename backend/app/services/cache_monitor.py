"""Prompt-cache observability.

A lightweight in-memory monitor that records cache_creation / cache_read /
input-token counts per chat turn, computes a hit ratio, and logs a
structured line. When a conversation drifts into a "burning money"
pattern (cache_creation dominates over multiple consecutive turns) it
escalates to WARNING so the line stands out in ``server.log``.

Backend-only. No frontend surface. The whole point is to detect cache
regressions (e.g. someone reintroduces volatile content into the
system prompt) WITHOUT having to manually inspect every turn's usage
counters.

Per-conversation state is keyed by ``session_id`` (the bookmarklet
bridge session) when available, falling back to ``"global"`` for CLI
/ MCP-driven runs that have no browser session.

State lives in-process. A backend restart wipes it; this is logging,
not billing, so that's fine.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque

_logger = logging.getLogger("app.cache_monitor")

# How many recent turns to inspect when deciding whether the
# conversation is in a "burn" pattern (cache_creation dominating).
_BURN_WINDOW = 4

# A turn is considered a "burn" turn if its create:read ratio exceeds
# this threshold. First-turn cache writes naturally exceed reads, so
# we don't flag a single turn — only sustained burns over the window.
_BURN_CREATE_RATIO = 0.6

# Anthropic's prompt-cache TTL. Samples older than this can't have
# benefited from the cache regardless of marker placement — their
# burn-ness is unavoidable, not a regression. We drop them before
# burn detection so a "lunch break" idle gap doesn't trigger false
# positives the next time the user resumes typing.
_CACHE_TTL_S = 5 * 60

# Maximum inter-turn gap that still counts as "within the same hot
# cache". If any two consecutive turns in the burn window are spaced
# wider than this, the cache between them has already died of TTL and
# the burn pattern is unavoidable (not an actionable bug). Slightly
# under _CACHE_TTL_S so we have headroom — the cache age clock starts
# at the previous turn's request time, not its response time.
_MAX_INTERTURN_GAP_S = 4 * 60

# Provider rate multipliers — Anthropic's published prices for cache
# tokens vs uncached input tokens. Used to compute a rough "$ wasted"
# estimate in the burn warning so the dev sees the impact in human
# terms instead of token counts. Not authoritative — for orientation,
# not billing. Updated to match Anthropic's listed prompt-cache pricing
# (Sonnet/Opus family, 5-minute cache; long-context tier costs differ).
_RATE_INPUT = 1.0          # baseline
_RATE_CACHE_READ = 0.10    # 10× cheaper than fresh input
_RATE_CACHE_WRITE = 1.25   # 25 % surcharge on create


@dataclass
class TurnSample:
    ts: float
    input_tokens: int
    output_tokens: int
    cache_read: int
    cache_creation: int
    iterations: int
    model: str
    # Conversation-level identifier — session_id when available, else
    # "global" for CLI/MCP runs.
    conv_id: str

    @property
    def hit_ratio(self) -> float:
        """Fraction of *input-side* tokens that came from cache.

        Output tokens are excluded — they don't participate in caching.
        Denominator is the full input footprint (fresh + read + write)
        so a turn that paid full price for everything reads as 0.0 and
        a turn that fully hit cache reads as ~1.0.
        """
        total_input = self.input_tokens + self.cache_read + self.cache_creation
        if total_input == 0:
            return 0.0
        return self.cache_read / total_input

    @property
    def cost_units(self) -> float:
        """Synthetic relative cost — useful only for comparing turns,
        not for actual billing. Lower is better."""
        return (
            self.input_tokens * _RATE_INPUT
            + self.cache_read * _RATE_CACHE_READ
            + self.cache_creation * _RATE_CACHE_WRITE
        )

    @property
    def cost_if_no_cache(self) -> float:
        """What this turn would have cost if every input token had
        been billed at fresh-input rate. Diff against ``cost_units``
        is the "savings" the cache delivered."""
        return (
            self.input_tokens + self.cache_read + self.cache_creation
        ) * _RATE_INPUT


@dataclass
class _ConvState:
    samples: Deque[TurnSample] = field(
        default_factory=lambda: deque(maxlen=_BURN_WINDOW)
    )
    burn_warnings_emitted: int = 0


_state: dict[str, _ConvState] = {}


def _conv(conv_id: str) -> _ConvState:
    s = _state.get(conv_id)
    if s is None:
        s = _ConvState()
        _state[conv_id] = s
    return s


def record_iteration(
    *,
    conv_id: str | None,
    model: str,
    iteration: int,
    usage: dict[str, int],
) -> None:
    """Log one Anthropic API call's cache stats.

    A user turn (one POST /chat/stream) runs the agent loop, which
    makes ONE Anthropic API call per iteration. Each call is its own
    cache transaction with its own ``cache_read`` / ``cache_creation``
    counters; aggregating across iterations hides which call is
    burning. This emits one log line per call:

        cache[<conv>] iter=<n> in=<X> read=<Y> create=<Z>
            out=<W> hit=<P>% savings=<S>% model=<M>

    Iteration 0 of a turn is the first request to the provider with
    the user's new message. Iteration 1+ sends a longer prompt
    including the model's previous text/tool_use + the dispatched
    tool_result. Each later iteration SHOULD show ``read`` growing as
    it picks up the previous iteration's cache.

    Defensive — never raises; bad usage payloads land in
    ``app.cache_monitor`` exception traces.
    """
    try:
        s = TurnSample(
            ts=time.time(),
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            cache_read=int(usage.get("cache_read_input_tokens") or 0),
            cache_creation=int(usage.get("cache_creation_input_tokens") or 0),
            iterations=int(iteration),
            model=model or "?",
            conv_id=conv_id or "global",
        )
    except Exception:
        _logger.exception("cache_monitor: bad iteration payload %r", usage)
        return

    saved = s.cost_if_no_cache - s.cost_units
    savings_pct = (
        (saved / s.cost_if_no_cache) * 100.0
        if s.cost_if_no_cache > 0 else 0.0
    )
    _logger.info(
        f"cache[{s.conv_id[:8]}] iter={iteration} "
        f"in={s.input_tokens} read={s.cache_read} create={s.cache_creation} "
        f"out={s.output_tokens} hit={s.hit_ratio:.0%} "
        f"savings={savings_pct:+.0f}% model={s.model}"
    )


def record(
    *,
    conv_id: str | None,
    model: str,
    usage: dict[str, int],
    iterations: int,
) -> None:
    """Record one chat turn's usage and emit a log line.

    Safe to call from inside the chat-stream finally-block; never
    raises. ``usage`` is the same dict shape used by routes/chat.py
    (``input_tokens``, ``output_tokens``, ``cache_read_input_tokens``,
    ``cache_creation_input_tokens``).
    """
    try:
        sample = TurnSample(
            ts=time.time(),
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            cache_read=int(usage.get("cache_read_input_tokens") or 0),
            cache_creation=int(usage.get("cache_creation_input_tokens") or 0),
            iterations=int(iterations),
            model=model or "?",
            conv_id=conv_id or "global",
        )
    except Exception:
        # Defensive — usage from a misbehaving provider shouldn't crash chat.
        _logger.exception("cache_monitor: bad usage payload %r", usage)
        return

    conv = _conv(sample.conv_id)
    conv.samples.append(sample)

    line = _format_line(sample)
    burn = _detect_burn(conv)
    if burn is not None:
        conv.burn_warnings_emitted += 1
        _logger.warning("%s  ⚠ %s", line, burn)
    else:
        _logger.info(line)


def _format_line(s: TurnSample) -> str:
    """One compact log line per turn — token-by-token breakdown plus
    a hit-ratio percentage so eyeballing the log file in real time is
    enough to spot regressions."""
    saved = s.cost_if_no_cache - s.cost_units
    savings_pct = (
        (saved / s.cost_if_no_cache) * 100.0
        if s.cost_if_no_cache > 0 else 0.0
    )
    return (
        f"cache[{s.conv_id[:8]}] "
        f"in={s.input_tokens} read={s.cache_read} create={s.cache_creation} "
        f"out={s.output_tokens} iters={s.iterations} "
        f"hit={s.hit_ratio:.0%} savings={savings_pct:+.0f}% "
        f"model={s.model}"
    )


def _detect_burn(conv: _ConvState) -> str | None:
    """Look at the recent window. Flag if the LAST N turns all show
    cache_creation dominating cache_read — that's the textbook
    "we're invalidating the prefix every turn" pattern.

    Time-aware: only considers samples within the cache TTL (older
    samples can't have benefited from cache anyway) AND requires the
    burn window's turns to be close enough together that the cache
    would actually have survived between them. Without these gates a
    "lunch break" idle gap would trigger a false-positive WARNING on
    the user's first turn back, because the cache legitimately died
    of TTL during their absence.

    Returns a human-readable diagnostic string, or None if we're fine.
    """
    now = time.time()
    # Filter samples that could have legitimately participated in the
    # cache. Anything older than the TTL is outside the window of
    # actionability — leave it in the deque for snapshot()/history
    # use, but don't count it toward a burn warning.
    fresh = [s for s in conv.samples if now - s.ts <= _CACHE_TTL_S]
    if len(fresh) < _BURN_WINDOW:
        return None

    # If any two consecutive turns in the burn window are spaced wider
    # than _MAX_INTERTURN_GAP_S, the cache between them already died
    # of TTL — the burn pattern isn't a marker-placement bug, it's
    # just user-pace. Don't warn.
    window = fresh[-_BURN_WINDOW:]
    for prev, curr in zip(window, window[1:]):
        if curr.ts - prev.ts > _MAX_INTERTURN_GAP_S:
            return None

    burns = 0
    for s in window:
        denom = s.cache_creation + s.cache_read
        if denom == 0:
            continue
        if s.cache_creation / denom >= _BURN_CREATE_RATIO:
            burns += 1
    if burns < _BURN_WINDOW:
        return None
    # Last sample's stats give the most actionable "right now" view.
    last = window[-1]
    return (
        f"CACHE BURN: last {_BURN_WINDOW} turns (within {_CACHE_TTL_S // 60}m "
        f"TTL window, max gap {_MAX_INTERTURN_GAP_S // 60}m between turns) "
        f"are >{int(_BURN_CREATE_RATIO * 100)}% cache_create. "
        f"Last turn paid create={last.cache_creation} tok at "
        f"{_RATE_CACHE_WRITE}× rate; only read={last.cache_read} tok at "
        f"{_RATE_CACHE_READ}× rate. Likely cause: volatile content in "
        f"system prompt or a mutated prior turn — see "
        f"backend/app/services/llm/anthropic.py docstring."
    )


def reset() -> None:
    """Drop all conversation state. Used by tests."""
    _state.clear()


def snapshot() -> dict[str, list[TurnSample]]:
    """Read-only view of recent samples per conversation. Used by tests
    and by anyone who wants to surface the data later (e.g. a tray
    submenu)."""
    return {cid: list(c.samples) for cid, c in _state.items()}
