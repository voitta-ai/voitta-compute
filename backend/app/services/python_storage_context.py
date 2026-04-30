"""Ambient ``[Available files]`` block for the chat system prompt.

The LLM is bad at trusting handles from prior turn history — when the
user says "use that data" or "build a report on it", the LLM tends to
re-derive the handle from scratch by re-searching the source provider
or re-downloading. Most of those round-trips short-circuit on the
dedup cache, but they still cost a tool call and waste tokens.

The fix: list recent python_storage snapshots in the system prompt on
every turn. The LLM sees the handle as ambient state and can call
``run_compute({args: {handle: 'py_xxx'}})`` directly.

Cheap to compute: a single ``iterdir()`` over ``python_storage/`` plus
parsing each ``meta.json``.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from app.services import python_storage


# Cap how many snapshots we surface in the system prompt. The LLM
# rarely cares about more than the last few — anything older the
# user can refer to by name and the LLM will RAG/search to find.
MAX_ENTRIES = 12

# Don't surface snapshots older than this — keeps the block focused
# on "stuff the user is currently working with" rather than
# everything that's ever been ingested.
MAX_AGE_S = 7 * 24 * 60 * 60  # 7 days


def get_context_block() -> str | None:
    """Return a one-paragraph system-prompt block listing recent
    python_storage snapshots, or None if there are none worth
    surfacing.

    Output shape::

        [Available files in python_storage]
          py_57cde1d0  |  Los Angeles.csv  (32.2 MB, 5m ago, drive_file)
          py_3cb1b3e1  |  st_us_asn_name.csv  (697 KB, 1h ago, drive_file)
          ...
        When the user refers to a file by name, USE the handle directly
        with run_compute (ctx.snapshot(handle)). Don't re-search/re-download
        — the file is already on disk.
    """
    if not python_storage.STORAGE_ROOT.exists():
        return None
    rows: list[dict] = []
    now = time.time()
    for d in python_storage.STORAGE_ROOT.iterdir():
        if not d.is_dir() or not d.name.startswith("snapshot_"):
            continue
        meta_path = d / "meta.json"
        if not meta_path.exists():
            continue
        try:
            meta = json.loads(meta_path.read_text())
        except Exception:
            continue
        if not isinstance(meta, dict):
            continue
        try:
            mtime = d.stat().st_mtime
        except OSError:
            continue
        if now - mtime > MAX_AGE_S:
            continue
        handle = meta.get("handle") or d.name.removeprefix("snapshot_")
        name = (
            meta.get("stored_name")
            or meta.get("original_name")
            or _fallback_name(meta)
        )
        kind = meta.get("kind") or "snapshot"
        files = meta.get("files") or []
        bytes_ = files[0].get("bytes", 0) if files else 0
        origin = meta.get("origin") or {}
        rows.append({
            "mtime": mtime,
            "handle": str(handle),
            "name": str(name),
            "kind": str(kind),
            "bytes": int(bytes_),
            "source": str(origin.get("source") or ""),
            "account": str(origin.get("account") or ""),
            "path": str(origin.get("path") or ""),
        })

    if not rows:
        return None
    rows.sort(key=lambda r: r["mtime"], reverse=True)
    rows = rows[:MAX_ENTRIES]

    lines = ["[Available files in python_storage — usable handles]"]
    for r in rows:
        age_s = now - r["mtime"]
        if age_s < 90:
            age = f"{int(age_s)}s ago"
        elif age_s < 5400:
            age = f"{int(age_s / 60)}m ago"
        elif age_s < 172800:
            age = f"{int(age_s / 3600)}h ago"
        else:
            age = f"{int(age_s / 86400)}d ago"

        # Origin tag — compact, aligned with how list_python_storage
        # surfaces it. Skip when there's no origin (legacy / hand-placed
        # snapshot).
        origin_parts: list[str] = []
        if r["source"]:
            origin_parts.append(r["source"])
        if r["account"]:
            origin_parts.append(r["account"])
        if r["path"]:
            origin_parts.append(r["path"])
        origin_tag = "  ·  ".join(origin_parts) if origin_parts else r["kind"]

        lines.append(
            f"    {r['handle']}  |  {r['name']}  "
            f"({_human_bytes(r['bytes'])}, {age})  |  {origin_tag}"
        )
    lines.append(
        "  When the user refers to one of these files by name, USE the "
        "handle DIRECTLY with run_compute (via ctx.snapshot(handle)). "
        "Do NOT re-search the source provider or re-download — the file is "
        "already on disk. The `origin` info (source · account · path) "
        "lets you disambiguate when multiple files share a name. Use "
        "`list_python_storage` with filters (account=, path_contains=, "
        "source=) for an authoritative list."
    )
    return "\n".join(lines)


def _fallback_name(meta: dict) -> str:
    """When stored_name/original_name are absent (e.g. older curves
    snapshots), build something the LLM can recognise."""
    summary = meta.get("summary") or {}
    if isinstance(summary, dict):
        for k in ("title", "name", "label"):
            v = summary.get(k)
            if isinstance(v, str) and v.strip():
                return v
    kind = meta.get("kind") or "snapshot"
    handle = meta.get("handle") or "?"
    return f"({kind} {handle})"


def _human_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    if n < 1024 * 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    return f"{n / (1024 * 1024 * 1024):.2f} GB"
