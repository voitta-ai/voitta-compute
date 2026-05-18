"""Stage 3 regression baseline — define_report response shape is unified.

Asserts:
- smoke_error is always present (None on success).
- ok mirrors smoke_error is None.
- hint, when present, points at RAG rather than a hardcoded doc section.
- No legacy "docs/<file>.md" pointers leak through.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "backend"))

from app.tools.domain import scripts as tool  # noqa: E402

ARTIFACTS = [
    REPO / "python_storage" / "reports" / "stage3_test_fail",
    REPO / "python_storage" / "reports" / "stage3_test_ok",
]


async def run() -> int:
    failures = 0

    # 1. Failure path — build(ctx) raises.
    res = await tool._define_report(
        {
            "name": "stage3_test_fail",
            "code": "def build(ctx):\n    raise RuntimeError('boom')\n",
        },
        None,
    )
    expected = {
        "ok": res["ok"] is False,
        "smoke_error_set": res["smoke_error"] is not None,
        "hint_present": "hint" in res,
        "hint_points_at_rag": "Search the RAG" in res.get("hint", ""),
        "no_legacy_doc_pointer": "docs/" not in res.get("hint", ""),
        "no_slickgrid_pattern": "SlickGrid" not in res.get("hint", ""),
    }
    for k, v in expected.items():
        if not v:
            print(f"FAIL  smoke-error path: {k}={v}")
            failures += 1
    if all(expected.values()):
        print("OK    smoke-error path: shape correct")

    # 2. Success path.
    res = await tool._define_report(
        {
            "name": "stage3_test_ok",
            "code": "def build(ctx):\n    return ctx\n",
        },
        None,
    )
    expected = {
        "ok": res["ok"] is True,
        "smoke_error_none": res["smoke_error"] is None,
        "no_hint_on_success": "hint" not in res,
    }
    for k, v in expected.items():
        if not v:
            print(f"FAIL  success path: {k}={v}  (res={res})")
            failures += 1
    if all(expected.values()):
        print("OK    success path: shape correct")

    return failures


def main() -> int:
    try:
        failures = asyncio.run(run())
    finally:
        for p in ARTIFACTS:
            if p.exists():
                shutil.rmtree(p)
    if failures:
        print(f"\n{failures} assertion(s) failed")
        return 1
    print("\nstage 3 response shape: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
