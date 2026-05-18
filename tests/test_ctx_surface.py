"""Stage 2 regression baseline — guard ctx methods used by existing reports.

Walks every stored report under python_storage/reports/*/code.py, extracts the
`ctx.<method>` calls, and asserts ScriptContext still exposes each one. Stage 3
changes (which touch the tool layer, not ctx) should leave this passing.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "backend"))

from app.services.scripts import ScriptContext  # noqa: E402

REPORTS_DIR = REPO / "python_storage" / "reports"
_CTX_CALL_RE = re.compile(r"\bctx\.([a-z_]+)\b")


def main() -> int:
    if not REPORTS_DIR.is_dir():
        print(f"no reports directory at {REPORTS_DIR} — nothing to check")
        return 0

    failures = 0
    for code_py in sorted(REPORTS_DIR.glob("*/code.py")):
        report = code_py.parent.name
        source = code_py.read_text()
        used = sorted(set(_CTX_CALL_RE.findall(source)))
        missing = [m for m in used if not hasattr(ScriptContext, m)]
        if missing:
            print(f"FAIL  {report}  missing ctx methods: {missing}")
            failures += 1
        else:
            print(f"OK    {report}  ctx methods: {used}")

    if failures:
        print(f"\n{failures} report(s) reference ctx methods that no longer exist")
        return 1
    print("\nctx surface still covers every report")
    return 0


if __name__ == "__main__":
    sys.exit(main())
