"""Stage 3.4 — RenderDrain captures post-render errors and formats reminders."""

from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "backend"))

from app.services import render_events  # noqa: E402
from app.services.render_log_drain import RenderDrain, format_reminder  # noqa: E402

REPORT = "stage34_drain_test"


def main() -> int:
    log_dir = render_events.SCRIPTS_REPORTS / REPORT
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "render_log.json"
    failures = 0
    try:
        d = RenderDrain()
        d.note_tool_result("show_holoviz_report", {"report_id": REPORT, "status": "ready"})

        if REPORT not in d.cursors:
            print("FAIL  note_tool_result didn't capture report_id")
            failures += 1

        cursor = d.cursors[REPORT]
        log_path.write_text(json.dumps([
            {"ts": cursor - 1.0, "kind": "error", "source": "window.error", "message": "old"},
            {"ts": time.time() + 0.001, "kind": "error", "source": "window.error", "message": "fresh error from JS"},
        ]))

        events = d.drain()
        if len(events) != 1 or "fresh" not in events[0]["message"]:
            print(f"FAIL  drain returned wrong events: {events}")
            failures += 1
        else:
            print("OK    drain returns only post-cursor events")

        events2 = d.drain()
        if events2 != []:
            print(f"FAIL  cursor not advanced; second drain returned {events2}")
            failures += 1
        else:
            print("OK    cursor advanced; second drain empty")

        r = format_reminder([events[0]])
        if not r or "<system-reminder>" not in r or "fresh" not in r or REPORT not in r:
            print(f"FAIL  format_reminder bad output: {r!r}")
            failures += 1
        else:
            print("OK    format_reminder produces valid block")

        # Empty input → None (caller skips injection)
        if format_reminder([]) is not None:
            print("FAIL  format_reminder([]) should be None")
            failures += 1
        else:
            print("OK    format_reminder([]) returns None")
    finally:
        shutil.rmtree(log_dir, ignore_errors=True)

    if failures:
        print(f"\n{failures} assertion(s) failed")
        return 1
    print("\nstage 3.4 drain: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
