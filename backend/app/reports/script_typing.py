"""Script typing: kind inference, sticky effects, same-turn latch.

Model — declared intent + observed effects, two axes on purpose:

* ``kind`` ("report" | "chat" | "job") is a DECLARED contract, set at
  define_script (explicit arg wins, else inferred from the dry-run smoke
  result) and re-declarable via edit_script.
* ``effects`` ({renders_html, emits_inline, writes_external}) are
  OBSERVED facts, recorded from every smoke and live run as a **sticky
  union** — bits only ever turn on. Rationale: a conditional writer
  (writes only under some args) must stay gated after a run that
  happened not to write; overwrite semantics would silently reopen the
  gate. The sole reset is an explicit ``kind`` re-declare on
  edit_script, which resets effects to that edit's smoke observation.

Disagreement between the two axes is DRIFT — computed at read time
(never stored, so it can't go stale) and surfaced, not auto-corrected.

The latch lets ``run_script`` skip the writes-external confirm gate for
a script defined/edited moments ago in the same session — the user just
watched it being authored; demanding a confirm on the very next call is
pure friction. In-memory, single-process (both shipped deployments run
one uvicorn); a multi-worker server would need this externalised.
"""

from __future__ import annotations

import time
from typing import Any, Optional

KINDS = ("report", "chat", "job")

# Effects keys that are ever recorded. Anything else is ignored on merge
# so a buggy writer can't grow the dict unboundedly.
_EFFECT_KEYS = ("renders_html", "emits_inline", "writes_external")


def infer_kind(result_value: Any, inline_count: int) -> str:
    """Classify from a (smoke) run's outcome: returned an HTML string →
    report; emitted inline items → chat; else → job. Honest caveat: a
    smoke run gets empty args, so an early-return-on-missing-args report
    infers as job — explicit ``kind`` wins, and drift self-surfaces
    after the first real run."""
    if isinstance(result_value, str):
        return "report"
    if inline_count > 0:
        return "chat"
    return "job"


def observed_effects(result_value: Any, ctx: Any) -> dict[str, bool]:
    """The effects one run demonstrably had. ``ctx.effects`` carries
    instrumented-client observations (writes_external)."""
    out = {
        "renders_html": isinstance(result_value, str),
        "emits_inline": bool(getattr(ctx, "inline", None)),
    }
    for k in _EFFECT_KEYS:
        if (getattr(ctx, "effects", None) or {}).get(k):
            out[k] = True
    return out


def merge_effects(stored: Optional[dict], observed: dict) -> dict[str, bool]:
    """Sticky union: a bit that was ever True stays True."""
    merged = {k: bool(v) for k, v in (stored or {}).items() if k in _EFFECT_KEYS}
    for k, v in observed.items():
        if k in _EFFECT_KEYS and v:
            merged[k] = True
    return merged


def drift(kind: Optional[str], effects: Optional[dict]) -> Optional[str]:
    """Computed (never stored) declared-vs-observed disagreement, or
    None. Only flags contradictions that matter, not omissions —
    e.g. a report that ALSO emits inline is fine."""
    if not kind or not effects:
        return None
    if kind == "report" and effects.get("emits_inline") and not effects.get("renders_html"):
        return "declared report but has only emitted inline chat content"
    if kind == "chat" and effects.get("renders_html"):
        return "declared chat but has rendered HTML reports"
    if kind in ("report", "chat") and effects.get("writes_external"):
        return f"declared {kind} but writes to Google Sheets"
    return None


# ---- same-turn latch --------------------------------------------------------

_LATCH_TTL_S = 10 * 60

# (session_id, slug) → monotonic timestamp of the define/edit.
_recent: dict[tuple[str, str], float] = {}


def mark_recently_edited(session_id: Optional[str], slug: str) -> None:
    if not session_id:
        return
    now = time.monotonic()
    # Opportunistic GC — the dict stays tiny.
    for k, ts in list(_recent.items()):
        if now - ts > _LATCH_TTL_S:
            _recent.pop(k, None)
    _recent[(session_id, slug)] = now


def was_recently_edited(session_id: Optional[str], slug: str) -> bool:
    if not session_id:
        return False
    ts = _recent.get((session_id, slug))
    return ts is not None and (time.monotonic() - ts) <= _LATCH_TTL_S
