"""Stage 3.5 — verify_report reads the inventory the shim writes."""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "backend"))

from app.services import render_events  # noqa: E402
from app.tools.domain import holoviz as tool  # noqa: E402

REPORT = "stage35_verify_test"


async def run() -> int:
    failures = 0
    rdir = render_events.SCRIPTS_REPORTS / REPORT
    rdir.mkdir(parents=True, exist_ok=True)
    try:
        # 1. No inventory yet → ok=False.
        res = await tool._verify_report({"report_id": REPORT}, None)
        if res.get("ok") is not False or "no inventory" not in (res.get("error") or ""):
            print(f"FAIL  missing-inventory path: {res}")
            failures += 1
        else:
            print("OK    missing inventory → ok=False with clear error")

        # 2. Write a synthetic inventory file (what the shim writes).
        (rdir / "inventory.json").write_text(json.dumps({
            "ts": time.time(),
            "render_id": "test123",
            "viewport": {"w": 1280, "h": 800},
            "roots": [
                {"root_index": 0, "type": "Column", "bbox": {"x": 0, "y": 0, "w": 1280, "h": 400}},
                {"root_index": 1, "type": "Tabulator", "bbox": {"x": 0, "y": 410, "w": 1280, "h": 320}},
            ],
        }))

        res = await tool._verify_report({"report_id": REPORT}, None)
        if res.get("ok") is not True:
            print(f"FAIL  populated inventory: ok={res.get('ok')}")
            failures += 1
        elif res.get("root_count") != 2:
            print(f"FAIL  root_count: {res.get('root_count')}")
            failures += 1
        elif res["roots"][0]["type"] != "Column":
            print(f"FAIL  root type: {res['roots'][0]}")
            failures += 1
        else:
            print("OK    populated inventory returns roots + bboxes")

        # 3. Missing report_id → input validation.
        res = await tool._verify_report({}, None)
        if res.get("ok") is not False:
            print(f"FAIL  missing report_id: {res}")
            failures += 1
        else:
            print("OK    missing report_id rejected")
    finally:
        shutil.rmtree(rdir, ignore_errors=True)

    if failures:
        print(f"\n{failures} assertion(s) failed")
        return 1
    print("\nstage 3.5 verify_report: OK")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
