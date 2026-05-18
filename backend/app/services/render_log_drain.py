"""Per-stream drain of post-render error logs.

Closes the "LLM saw ready, user saw broken" gap. After ``show_holoviz_report``
returns, more errors can fire (deferred widget mounts, websocket reconnects,
user toggling edit mode). They land in ``render_log.json`` but the LLM never
sees them unless it explicitly calls ``get_report_render_errors``.

This module gives ``chat_stream`` a small state object it can use to drain
new errors between iterations and inject them as a system reminder block
into the next user message, so the LLM is told without having to ask.

State is per-stream (per chat-POST). When the stream ends, the state is
garbage-collected.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.services import render_events


@dataclass
class RenderDrain:
    """Tracks which reports the conversation has touched and how far we've
    drained their error logs."""

    # report_id → last-drained timestamp. We only return events strictly
    # newer than this on subsequent drains.
    cursors: dict[str, float] = field(default_factory=dict)

    def note_report(self, report_id: str | None) -> None:
        """Remember that a report was touched. Prime the cursor to NOW
        so the *first* drain (next turn) only catches errors that
        fire after this moment — not the historical log."""
        if not report_id or not isinstance(report_id, str):
            return
        if report_id not in self.cursors:
            import time
            self.cursors[report_id] = time.time()

    def note_tool_result(self, tool_name: str, result: Any) -> None:
        """Extract report_id from a tool result dict where applicable."""
        if not isinstance(result, dict):
            return
        # define_report / edit_report_script / show_holoviz_report all
        # carry the report_id field.
        rid = result.get("report_id")
        if isinstance(rid, str):
            self.note_report(rid)

    def drain(self) -> list[dict]:
        """Pull new error events across all tracked reports.

        Updates cursors so the same event is never emitted twice.
        Returns a flat list of error dicts (oldest→newest), tagged
        with their ``report_id``.
        """
        out: list[dict] = []
        for report_id, since in list(self.cursors.items()):
            events = render_events.list_recent_for_report(
                report_id, since_ts=since, kinds=("error",), limit=50
            )
            if not events:
                continue
            # Advance cursor past the latest event we drained.
            newest_ts = max(float(e.get("ts") or 0) for e in events)
            self.cursors[report_id] = max(since, newest_ts) + 1e-6
            for e in events:
                tagged = {"report_id": report_id, **e}
                out.append(tagged)
        # Sort oldest→newest across reports for a coherent reminder.
        out.sort(key=lambda e: float(e.get("ts") or 0))
        return out


def format_reminder(events: list[dict]) -> str | None:
    """Render the drained events as a single system-reminder string.

    Returns None when there are no events — the caller skips injection.
    """
    if not events:
        return None
    lines = [
        "<system-reminder>",
        "Post-render errors fired in the report iframe AFTER the most "
        "recent show_holoviz_report returned. The user is likely seeing "
        "a broken or partially-broken report:",
        "",
    ]
    for ev in events:
        rid = ev.get("report_id") or "?"
        source = ev.get("source") or "?"
        msg = (ev.get("message") or "").strip().splitlines()[0][:300]
        lines.append(f"- [{rid}] [{source}] {msg}")
    lines.append("")
    lines.append(
        "Investigate and fix via edit_report_script, then re-call "
        "show_holoviz_report to verify. For the full stack, call "
        "get_report_render_errors(report_id=...)."
    )
    lines.append("</system-reminder>")
    return "\n".join(lines)
