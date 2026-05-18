"""cache_monitor: hit-ratio math + burn detection."""

from __future__ import annotations

import logging
import sys
from io import StringIO
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "backend"))

from app.services import cache_monitor as cm  # noqa: E402


def _capture_logs() -> tuple[logging.Handler, StringIO]:
    buf = StringIO()
    handler = logging.StreamHandler(buf)
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    log = logging.getLogger("app.cache_monitor")
    log.addHandler(handler)
    log.setLevel(logging.DEBUG)
    return handler, buf


def main() -> int:
    failures = 0
    cm.reset()
    handler, buf = _capture_logs()

    # 1. Healthy turn — high cache_read, low cache_creation.
    cm.record(
        conv_id="conv-warm",
        model="claude-opus-4-7",
        usage={
            "input_tokens": 50,
            "output_tokens": 200,
            "cache_read_input_tokens": 55_000,
            "cache_creation_input_tokens": 1_050,
        },
        iterations=1,
    )
    out = buf.getvalue()
    if "INFO" not in out:
        print(f"FAIL  healthy turn should log INFO. Got:\n{out}")
        failures += 1
    elif "hit=98%" not in out and "hit=97%" not in out and "hit=99%" not in out:
        print(f"FAIL  healthy turn should report ~98% hit ratio. Got:\n{out}")
        failures += 1
    else:
        print("OK    healthy turn logged at INFO with high hit ratio")

    # 2. Cold turn — first conversation, all cache_creation.
    cm.reset()
    handler2, buf2 = _capture_logs()
    cm.record(
        conv_id="conv-cold",
        model="claude-opus-4-7",
        usage={
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 60_000,
        },
        iterations=1,
    )
    out = buf2.getvalue()
    # Cold turn alone (window not full) must not warn — that's normal first-turn behaviour.
    if "WARNING" in out:
        print(f"FAIL  cold first turn should not warn. Got:\n{out}")
        failures += 1
    else:
        print("OK    cold first turn does not warn")

    # 3. Sustained burn pattern — four consecutive turns with
    # cache_creation > 60% of (create + read). Should escalate to WARNING.
    cm.reset()
    handler3, buf3 = _capture_logs()
    for i in range(4):
        cm.record(
            conv_id="conv-burning",
            model="claude-opus-4-7",
            usage={
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_read_input_tokens": 1_000,    # tiny read
                "cache_creation_input_tokens": 60_000,  # huge create every turn
            },
            iterations=1,
        )
    out = buf3.getvalue()
    if "WARNING" not in out:
        print(f"FAIL  4 burn turns should escalate to WARNING. Got:\n{out}")
        failures += 1
    elif "CACHE BURN" not in out:
        print(f"FAIL  WARNING should mention CACHE BURN. Got:\n{out}")
        failures += 1
    else:
        print("OK    4 sustained burn turns trigger WARNING")

    # 4. snapshot() reflects state.
    snap = cm.snapshot()
    if "conv-burning" not in snap or len(snap["conv-burning"]) != 4:
        print(f"FAIL  snapshot missing burn conversation: {list(snap.keys())}")
        failures += 1
    else:
        print("OK    snapshot exposes per-conversation samples")

    # 5. hit_ratio math sanity.
    samples = snap["conv-burning"]
    last = samples[-1]
    # cache_read=1000, cache_creation=60000, input=100 → hit ≈ 1000/61100 ≈ 1.6%
    if not (0.01 < last.hit_ratio < 0.03):
        print(f"FAIL  hit_ratio math wrong: {last.hit_ratio}")
        failures += 1
    else:
        print(f"OK    hit_ratio matches expected (~1.6%): {last.hit_ratio:.3f}")

    # 6a. Time-aware: when a long idle gap separates the burn turns,
    # the cache between them would have died of TTL anyway → no warn.
    cm.reset()
    handler_idle, buf_idle = _capture_logs()
    # Hand-craft samples with a 6-minute gap in the middle of the window.
    import time as _time
    now = _time.time()
    convstate = cm._conv("conv-idle")
    for offset_s in (0, 5, 5 * 60 + 1, 5 * 60 + 6):  # last two are 5 min after first two
        convstate.samples.append(cm.TurnSample(
            ts=now - (5 * 60 + 6) + offset_s,
            input_tokens=100,
            output_tokens=50,
            cache_read=1_000,
            cache_creation=60_000,
            iterations=1,
            model="claude-opus-4-7",
            conv_id="conv-idle",
        ))
    # Re-run detect on this hand-crafted state.
    diag = cm._detect_burn(convstate)
    if diag is not None:
        print(f"FAIL  burn with idle gap should NOT warn. Got: {diag}")
        failures += 1
    else:
        print("OK    burn pattern across long idle gap suppressed")
    logging.getLogger("app.cache_monitor").removeHandler(handler_idle)

    # 6b. Time-aware: stale samples (>5 min ago) shouldn't count toward
    # the window at all — a fresh conversation that's "burning" but
    # all its samples are old must not warn until it has 4 fresh ones.
    cm.reset()
    handler_stale, buf_stale = _capture_logs()
    convstate2 = cm._conv("conv-stale")
    # 4 burn samples, all 10 minutes old.
    for i in range(4):
        convstate2.samples.append(cm.TurnSample(
            ts=now - (10 * 60) + i,
            input_tokens=100,
            output_tokens=50,
            cache_read=1_000,
            cache_creation=60_000,
            iterations=1,
            model="claude-opus-4-7",
            conv_id="conv-stale",
        ))
    diag = cm._detect_burn(convstate2)
    if diag is not None:
        print(f"FAIL  stale samples should not trigger burn. Got: {diag}")
        failures += 1
    else:
        print("OK    stale samples (>TTL) excluded from burn detection")
    logging.getLogger("app.cache_monitor").removeHandler(handler_stale)

    # 7. Bad usage payload doesn't crash.
    cm.reset()
    try:
        cm.record(
            conv_id="conv-bad",
            model="x",
            usage={"input_tokens": "garbage"},  # type: ignore[dict-item]
            iterations=1,
        )
    except Exception as exc:
        print(f"FAIL  bad usage payload crashed: {exc}")
        failures += 1
    else:
        print("OK    bad usage payload absorbed without crashing")

    logging.getLogger("app.cache_monitor").removeHandler(handler)
    logging.getLogger("app.cache_monitor").removeHandler(handler2)
    logging.getLogger("app.cache_monitor").removeHandler(handler3)

    if failures:
        print(f"\n{failures} assertion(s) failed")
        return 1
    print("\ncache_monitor: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
