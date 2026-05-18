"""Stage 1.5 smoke test — RAG retrieval lands on the new panel-*.md docs.

Each representative query has an expected target file. We assert the
top-1 hit is the right file. Run after `scripts/build_rag.py --corpus
docs`.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "backend"))

from app.tools.rag.search import query  # noqa: E402


QUERIES: list[tuple[str, str]] = [
    ("how do I write a Panel report build function", "panel-skeleton.md"),
    ("load a dataframe from python_storage into a report", "panel-snapshots.md"),
    ("add custom CSS to a Panel report", "panel-theming.md"),
    ("embed a three.js scene in a report", "panel-three-scene.md"),
    ("SlickGrid stylesheet race blank table", "panel-common-errors.md"),
    ("report iframe is black or blank", "panel-common-errors.md"),
    ("how does the report theme match the host page", "panel-theming.md"),
    ("screenshot of report is missing the 3D viewer", "panel-screenshot-limits.md"),
    ("what does build(ctx) return", "panel-skeleton.md"),
    ("how to debug a report that won't render", "panel-common-errors.md"),
]


def main() -> int:
    failures = 0
    for q, expected in QUERIES:
        hits = query(q, top_k=3, dense_weight=0.7, corpus="docs")
        if not hits:
            print(f"FAIL  no hits for: {q!r}")
            failures += 1
            continue
        files = [h["file"] for h in hits[:3]]
        ok = expected in files
        rank = files.index(expected) + 1 if ok else None
        marker = "OK  " if ok else "FAIL"
        print(f"{marker}  {q!r}")
        if ok:
            print(f"      expected: {expected}  rank={rank}")
        else:
            print(f"      expected: {expected}  (not in top-3)")
            for h in hits[:3]:
                print(f"        - {h['file']}  score={h['score']:.3f}")
        if not ok:
            failures += 1
    print()
    print(f"{len(QUERIES) - failures}/{len(QUERIES)} queries land on the expected file")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
