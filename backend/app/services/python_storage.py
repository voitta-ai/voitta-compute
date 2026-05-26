"""Python-side on-disk storage for downloaded VOITTA data.

Each download creates ``python_storage/cache/snapshot_<handle>/`` at the
project root. Inside each snapshot:

  • ``meta.json``  — provenance: source URL, file id, parser, fetched_at,
                     http status, byte size, content type.
  • ``raw.json``   — verbatim response body (when JSON-shaped).
  • ``curves.pkl`` — pickled pandas DataFrame, best-effort flatten of
                     the ``curves`` array. Absent if the response wasn't
                     curves-shaped or pandas wasn't available.

Folders
-------
Snapshots can optionally be placed in a named folder:

    python_storage/cache/
      snapshot_{handle}/           ← unfoldered
      folders/
        {folder_name}/
          folder.json              ← {"name", "description", "color", "created_at"}
          snapshot_{handle}/       ← foldered snapshots

Folder names must match ``[a-z0-9_-]{1,64}``.

Why a separate Python store at all?

  • It survives a browser reload — the JS-side buffer doesn't.
  • Anything that wants pandas/polars/pyarrow on multi-MB data is much
    cheaper here than passing JSON through the bridge first.
  • Snapshots persist across backend restarts (the directory itself
    survives; ``meta.json`` per snapshot makes the on-disk state
    self-describing).

The store is local to the developer's machine; it is NOT a shared
cache. ``python_storage/`` is gitignored. Use ``clear_python_storage``
between sessions for disk hygiene.
"""

from __future__ import annotations

import json
import re
import secrets
import shutil
import time
from collections import Counter
from pathlib import Path
from typing import Any


# Cap on distinct values per metadata key in the rich summary. Above this,
# the summary records cardinality only ("distinct_values": ">20") and drops
# the enumeration — keeps the summary compact for fields like `s/n` and
# `Time` that are unique per curve.
META_VALUE_CAP = 20


# Mirror app.config.PROJECT_ROOT so packaged (.app) and dev modes agree
# on where mutable state lives. See app/config.py for the env-var override.
from app.config import USER_DATA_ROOT  # noqa: E402

# Snapshot cache lives under ``python_storage/cache/``. The parent
# ``python_storage/`` is the unified state dir; sibling subdirs
# (``compute/`` / ``reports/`` / ``flows/`` — see services/scripts.py)
# hold persisted LLM-authored code. Keeping snapshots in a named
# subdir means the namespace at the top is meaningful (cache vs
# durable code) rather than a soup of mixed kinds.
STORAGE_ROOT = USER_DATA_ROOT / "python_storage" / "cache"

# Folder container inside the cache root.
FOLDERS_ROOT = STORAGE_ROOT / "folders"


def _validate_folder_name(name: str) -> None:
    if not re.fullmatch(r'[a-z0-9_-]{1,64}', name):
        raise ValueError(
            f"Invalid folder name {name!r}: use [a-z0-9_-], max 64 chars"
        )


def _ensure_root() -> None:
    STORAGE_ROOT.mkdir(parents=True, exist_ok=True)
    FOLDERS_ROOT.mkdir(parents=True, exist_ok=True)


def _new_handle() -> str:
    return f"py_{secrets.token_hex(4)}"


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------------------
# Folder management
# ---------------------------------------------------------------------------

def create_folder(name: str, description: str = "", color: str = "") -> dict:
    """Create a new folder inside FOLDERS_ROOT.

    Raises ``ValueError`` for invalid names or if the folder already exists.
    Returns the folder metadata dict.
    """
    _validate_folder_name(name)
    _ensure_root()
    folder_dir = FOLDERS_ROOT / name
    if folder_dir.exists():
        raise ValueError(f"Folder {name!r} already exists")
    folder_dir.mkdir(parents=True)
    data = {
        "name": name,
        "description": description,
        "color": color,
        "created_at": _now_iso(),
    }
    (folder_dir / "folder.json").write_text(
        json.dumps(data, indent=2, ensure_ascii=False)
    )
    return data


def list_folders() -> list[dict]:
    """Return a list of all folder metadata dicts, each with a ``snapshot_count`` key."""
    _ensure_root()
    out: list[dict] = []
    if not FOLDERS_ROOT.is_dir():
        return out
    for folder_dir in sorted(FOLDERS_ROOT.iterdir()):
        if not folder_dir.is_dir():
            continue
        folder_json = folder_dir / "folder.json"
        if not folder_json.exists():
            continue
        try:
            data = json.loads(folder_json.read_text())
        except Exception:
            continue
        # Count snapshots inside this folder.
        count = sum(
            1
            for d in folder_dir.iterdir()
            if d.is_dir() and d.name.startswith("snapshot_")
        )
        data["snapshot_count"] = count
        out.append(data)
    return out


def delete_folder(name: str) -> bool:
    """Delete a folder, moving all its snapshots back to STORAGE_ROOT first.

    Returns False if the folder doesn't exist.
    """
    _ensure_root()
    folder_dir = FOLDERS_ROOT / name
    if not folder_dir.is_dir():
        return False
    # Move any snapshots out to the root first.
    for d in list(folder_dir.iterdir()):
        if d.is_dir() and d.name.startswith("snapshot_"):
            dest = STORAGE_ROOT / d.name
            shutil.move(str(d), str(dest))
    shutil.rmtree(folder_dir)
    return True


def move_to_folder(handle: str, folder_name: str | None) -> bool:
    """Move a snapshot to a folder (or back to root when folder_name is None).

    Returns False if the snapshot handle is not found.
    """
    _ensure_root()
    snap_dir = _find_snapshot_dir(handle)
    if snap_dir is None:
        return False

    if folder_name is None:
        dest = STORAGE_ROOT / f"snapshot_{handle}"
    else:
        _validate_folder_name(folder_name)
        folder_dir = FOLDERS_ROOT / folder_name
        if not folder_dir.is_dir():
            raise ValueError(f"Folder {folder_name!r} does not exist")
        dest = folder_dir / f"snapshot_{handle}"

    if snap_dir == dest:
        return True  # Already in the right place.

    shutil.move(str(snap_dir), str(dest))
    return True


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _find_snapshot_dir(handle: str) -> Path | None:
    """Locate the snapshot directory for *handle*, searching root then folders."""
    root_dir = STORAGE_ROOT / f"snapshot_{handle}"
    if root_dir.is_dir():
        return root_dir
    if FOLDERS_ROOT.is_dir():
        for folder_dir in FOLDERS_ROOT.iterdir():
            if not folder_dir.is_dir():
                continue
            candidate = folder_dir / f"snapshot_{handle}"
            if candidate.is_dir():
                return candidate
    return None


def _folder_name_for_dir(snap_dir: Path) -> str | None:
    """Return the folder name if *snap_dir* lives inside FOLDERS_ROOT, else None."""
    try:
        rel = snap_dir.relative_to(FOLDERS_ROOT)
        # rel is  <folder_name>/snapshot_<handle>
        parts = rel.parts
        if len(parts) >= 1:
            return parts[0]
    except ValueError:
        pass
    return None


# ---------------------------------------------------------------------------
# Summarizers
# ---------------------------------------------------------------------------

def summarize_curves(body: Any) -> dict:
    """Compute a metadata-rich summary of a curves-shaped payload.

    Designed to give the LLM enough to plan next steps without round-
    tripping curve values. Bounded:

      • Per-key value enumerations cap at ``META_VALUE_CAP`` distinct
        values; over that, only cardinality is reported ("distinct_values":
        ">20"). Keeps the summary compact for unique-per-curve fields
        like ``s/n`` and ``Time``.
      • Per-series stats are O(1) memory regardless of curve-value
        length — we only carry counts and length samples.
      • Total summary size grows with #distinct series + #distinct
        metadata keys (typically 5-30 each), not with curve count.
    """

    if not isinstance(body, dict):
        return {"shape": type(body).__name__}
    curves = body.get("curves")
    if not isinstance(curves, list):
        return {
            "shape": "no_curves_array",
            "top_level_keys": list(body.keys())[:32],
        }

    out: dict[str, Any] = {
        "curve_count": len(curves),
        "filename": body.get("filename"),
        "dataset_name": body.get("datasetName"),
        "parsed_with": body.get("parsedWith"),
    }

    # ---- parsing report (often verbose; trim aggressively) ----------------
    parsing_report = body.get("parsingReport")
    if isinstance(parsing_report, dict):
        report_status = parsing_report.get("status")
        if report_status:
            out["parsing_status"] = report_status
        for key in ("warnings", "messages", "errors"):
            v = parsing_report.get(key)
            if isinstance(v, list) and v:
                out[f"parsing_{key}_count"] = len(v)
                out[f"parsing_{key}_sample"] = v[:3]

    # ---- per-curve aggregation ------------------------------------------
    series_curves: dict[str, int] = {}          # series name → count of curves
    series_lengths: dict[str, list[int]] = {}   # series name → values-array lengths seen
    series_units: dict[str, set[str]] = {}      # series name → set of units seen
    meta_curves: dict[str, int] = {}            # metadata key → count of curves with key
    meta_values: dict[str, dict[str, int]] = {} # metadata key → {value: count}
    meta_overflow: set[str] = set()             # keys that exceeded META_VALUE_CAP

    for c in curves:
        if not isinstance(c, dict):
            continue
        for s in c.get("series") or []:
            if not isinstance(s, dict):
                continue
            name = s.get("name")
            if not name:
                continue
            series_curves[name] = series_curves.get(name, 0) + 1
            unit = s.get("unit")
            if unit:
                series_units.setdefault(name, set()).add(unit)
            vals = s.get("values")
            if isinstance(vals, list):
                series_lengths.setdefault(name, []).append(len(vals))
        for m in c.get("metadata") or []:
            if not isinstance(m, dict):
                continue
            k = m.get("key")
            if not isinstance(k, str):
                continue
            meta_curves[k] = meta_curves.get(k, 0) + 1
            if k in meta_overflow:
                continue
            v = m.get("value")
            if v is None:
                continue
            v_str = str(v)
            d = meta_values.setdefault(k, {})
            d[v_str] = d.get(v_str, 0) + 1
            if len(d) > META_VALUE_CAP:
                meta_overflow.add(k)
                meta_values.pop(k, None)

    # ---- per-series block ------------------------------------------------
    series_summary: dict[str, dict] = {}
    for name in sorted(series_curves.keys()):
        block: dict[str, Any] = {"curves_with_series": series_curves[name]}
        units = sorted(series_units.get(name, set()))
        if units:
            block["units"] = units
        lengths = series_lengths.get(name) or []
        if lengths:
            block["value_length_min"] = min(lengths)
            block["value_length_max"] = max(lengths)
            mode_len, mode_count = Counter(lengths).most_common(1)[0]
            block["value_length_mode"] = mode_len
            block["value_length_mode_count"] = mode_count
        series_summary[name] = block
    out["series"] = series_summary

    # ---- per-metadata-key block ------------------------------------------
    meta_summary: dict[str, dict] = {}
    for k in sorted(meta_curves.keys()):
        block = {"curves_with_key": meta_curves[k]}
        if k in meta_overflow:
            block["distinct_values"] = f">{META_VALUE_CAP}"
        else:
            d = meta_values.get(k, {})
            block["distinct_values"] = len(d)
            if d:
                # Sort by count desc, value asc (for ties) — most useful at top.
                items = sorted(d.items(), key=lambda kv: (-kv[1], kv[0]))
                block["values"] = dict(items)
        meta_summary[k] = block
    out["metadata_coverage"] = meta_summary

    return out


def summarize_generic(body: Any) -> dict:
    """Fallback summary for non-curves payloads — top-level shape only."""

    if isinstance(body, list):
        return {
            "shape": "array",
            "length": len(body),
            "sample_keys": (
                sorted(body[0].keys())[:20]
                if body and isinstance(body[0], dict)
                else None
            ),
        }
    if isinstance(body, dict):
        keys = list(body.keys())
        out: dict[str, Any] = {"shape": "object", "top_level_keys": keys[:32]}
        # Hint at array-shaped fields' lengths (cheap, useful).
        for k in keys[:32]:
            v = body[k]
            if isinstance(v, list):
                out[f"{k}_length"] = len(v)
            elif isinstance(v, dict):
                out[f"{k}_keys"] = len(v)
        return out
    return {"shape": type(body).__name__, "value": body}


def _pick_summarizer(kind: str):
    return summarize_curves if kind == "curves" else summarize_generic


def make_origin(
    *,
    source: str,
    account: str | None = None,
    path: str | None = None,
    file_id: str | None = None,
    host: str | None = None,
    url: str | None = None,
    extra: dict | None = None,
) -> dict[str, Any]:
    """Build the canonical ``origin`` block we attach to every snapshot.

    Five top-level fields are always present (possibly None) so
    downstream filters / display can rely on a stable shape:

      • ``source`` — short identifier of the system the data came from
        (``"google_drive"``, ``"google_drive_export"``, ``"upload"``,
        future provider keys like ``"dropbox"``).
      • ``account`` — the user identity at the source (e.g. Drive OAuth
        ``account_email``). Used for ``account=`` filters in
        list_python_storage.
      • ``path`` — human-readable path/location at the source. For
        Drive: ``/My Drive/Folder/file.pdf``. Used for
        ``path_contains=`` filters.
      • ``file_id`` — opaque source-side ID (e.g. Drive file ID).
      • ``host`` — origin host where the data lives
        (``drive.google.com``, etc).
      • ``url`` — the deep link to view the source in its native UI
        (e.g. Drive's ``webViewLink``).
      • ``extra`` — anything source-specific that doesn't fit above
        (Drive owners, MIME, export format, etc). Free-form dict.
    """
    out: dict[str, Any] = {
        "source": source,
        "account": account,
        "path": path,
        "file_id": file_id,
        "host": host,
        "url": url,
    }
    if extra:
        out["extra"] = extra
    return out


def put(
    *,
    kind: str,
    response_body: Any,
    meta: dict,
    folder_name: str | None = None,
) -> dict:
    """Persist a response body to disk and return the snapshot record.

    For ``kind="curves"`` we also try to flatten into a pandas DataFrame
    (one row per (curve, series, value)) and pickle it as ``curves.pkl``.
    The flatten is best-effort: if pandas isn't importable or the body
    isn't curves-shaped, we record a ``flatten_error`` in meta and skip.

    A rich summary (see ``summarize_curves`` / ``summarize_generic``) is
    computed and stored in ``meta["summary"]`` so subsequent
    ``get_python_storage_info`` calls return it for free.

    If ``folder_name`` is given the snapshot is placed inside
    ``FOLDERS_ROOT/{folder_name}/snapshot_{handle}/``.
    """

    _ensure_root()
    if folder_name is not None:
        _validate_folder_name(folder_name)
        folder_dir = FOLDERS_ROOT / folder_name
        if not folder_dir.is_dir():
            raise ValueError(f"Folder {folder_name!r} does not exist")

    handle = _new_handle()
    if folder_name is not None:
        snapshot_dir = FOLDERS_ROOT / folder_name / f"snapshot_{handle}"
    else:
        snapshot_dir = STORAGE_ROOT / f"snapshot_{handle}"
    snapshot_dir.mkdir()

    files: list[dict] = []

    raw_path = snapshot_dir / "raw.json"
    raw_path.write_text(json.dumps(response_body, ensure_ascii=False))
    files.append({"name": "raw.json", "bytes": raw_path.stat().st_size})

    flatten_error: str | None = None
    if kind == "curves":
        try:
            df_info = _flatten_and_pickle(response_body, snapshot_dir)
            if df_info:
                files.append(df_info)
        except Exception as exc:
            flatten_error = f"{type(exc).__name__}: {exc}"

    summary = _pick_summarizer(kind)(response_body)

    full_meta = {
        "handle": handle,
        "kind": kind,
        "created_at": _now_iso(),
        "files": files,
        **meta,
        "summary": summary,
    }
    if flatten_error:
        full_meta["flatten_error"] = flatten_error

    meta_path = snapshot_dir / "meta.json"
    meta_path.write_text(json.dumps(full_meta, indent=2, ensure_ascii=False))

    return {
        "handle": handle,
        "path": str(snapshot_dir),
        "kind": kind,
        "files": files,
        "meta": full_meta,
        "folder_name": folder_name,
    }


def find_latest_by_meta(predicate: Any) -> dict | None:
    """Return the most-recent snapshot record matching ``predicate(meta)``.

    Used by ingest tools that want to avoid downloading the same blob
    twice. ``predicate`` is a callable taking the parsed ``meta.json``
    dict and returning truthy on a match. The newest match wins (sorted
    by mtime of the snapshot dir).

    Returns the same shape as ``get(handle)`` plus ``meta``, or
    ``None`` if no match.

    Searches both root snapshots and all foldered snapshots.
    """

    if not STORAGE_ROOT.exists():
        return None

    candidates: list[Path] = []

    # Root-level snapshots.
    for d in STORAGE_ROOT.iterdir():
        if d.is_dir() and d.name.startswith("snapshot_"):
            candidates.append(d)

    # Foldered snapshots.
    if FOLDERS_ROOT.is_dir():
        for folder_dir in FOLDERS_ROOT.iterdir():
            if not folder_dir.is_dir():
                continue
            for d in folder_dir.iterdir():
                if d.is_dir() and d.name.startswith("snapshot_"):
                    candidates.append(d)

    matches: list[tuple[float, Path, dict]] = []
    for d in candidates:
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
            if not predicate(meta):
                continue
        except Exception:
            continue
        try:
            mtime = d.stat().st_mtime
        except OSError:
            continue
        matches.append((mtime, d, meta))

    if not matches:
        return None
    matches.sort(reverse=True)
    _, snap_dir, meta = matches[0]
    files = list(meta.get("files") or [])
    handle = meta.get("handle") or snap_dir.name.removeprefix("snapshot_")
    return {
        "handle": handle,
        "path": str(snap_dir),
        "kind": meta.get("kind"),
        "files": files,
        "meta": meta,
        "folder_name": _folder_name_for_dir(snap_dir),
    }


def put_file(
    *,
    src_path: Path | str,
    original_name: str,
    kind: str,
    meta: dict,
    move: bool = True,
    folder_name: str | None = None,
) -> dict:
    """Bring an external file into python_storage.

    Used by the Drive download-pickup flow: the browser triggered a
    download, the user-side filesystem now has the file at ``src_path``,
    and we move it into ``python_storage/cache/snapshot_<handle>/`` next to a
    ``meta.json`` describing where it came from. Symmetric with
    ``put(...)`` for response bodies — same handle/dir layout so the
    rest of the system (compute scripts, ctx.snapshot, etc.) sees both
    flavours uniformly.

    Returns ``{handle, path, kind, files, meta, folder_name}`` (same shape
    as ``put``).

    ``move=True`` means the source is unlinked after copy. ``False``
    leaves it in place (useful for tests).

    If ``folder_name`` is given the snapshot is placed inside
    ``FOLDERS_ROOT/{folder_name}/snapshot_{handle}/``.
    """

    src = Path(src_path)
    if not src.is_file():
        raise FileNotFoundError(f"src_path {src!s} is not a file")

    _ensure_root()
    if folder_name is not None:
        _validate_folder_name(folder_name)
        folder_dir = FOLDERS_ROOT / folder_name
        if not folder_dir.is_dir():
            raise ValueError(f"Folder {folder_name!r} does not exist")

    handle = _new_handle()
    if folder_name is not None:
        snapshot_dir = FOLDERS_ROOT / folder_name / f"snapshot_{handle}"
    else:
        snapshot_dir = STORAGE_ROOT / f"snapshot_{handle}"
    snapshot_dir.mkdir()

    # Sanitise the destination name — keep the user-recognisable original,
    # but drop characters that would break shell paths.
    safe_name = "".join(
        c if (c.isalnum() or c in "._- ") else "_" for c in original_name
    ).strip() or "file"
    if len(safe_name) > 200:
        safe_name = safe_name[:200]
    dst = snapshot_dir / safe_name
    # If somehow the name collides with meta.json or curves.pkl, suffix.
    if dst.name in {"meta.json", "raw.json", "curves.pkl"}:
        dst = snapshot_dir / ("file_" + safe_name)

    if move:
        try:
            shutil.move(str(src), str(dst))
        except OSError:
            # Cross-device or permissions — fall back to copy + unlink.
            shutil.copy2(str(src), str(dst))
            try:
                src.unlink()
            except OSError:
                pass
    else:
        shutil.copy2(str(src), str(dst))

    bytes_written = dst.stat().st_size
    files = [{"name": dst.name, "bytes": bytes_written}]

    full_meta = {
        "handle": handle,
        "kind": kind,
        "created_at": _now_iso(),
        "original_name": original_name,
        "stored_name": dst.name,
        "files": files,
        **meta,
    }
    (snapshot_dir / "meta.json").write_text(
        json.dumps(full_meta, indent=2, ensure_ascii=False)
    )

    return {
        "handle": handle,
        "path": str(snapshot_dir),
        "kind": kind,
        "files": files,
        "meta": full_meta,
        "folder_name": folder_name,
    }


def _flatten_and_pickle(body: Any, snapshot_dir: Path) -> dict | None:
    """Best-effort: turn a curves-shaped body into a long-form DataFrame
    and pickle it. Returns the file record or None when there's no
    curves array to flatten."""

    if not isinstance(body, dict):
        return None
    curves = body.get("curves")
    if not isinstance(curves, list) or not curves:
        return None

    import pandas as pd

    rows: list[dict] = []
    for ci, c in enumerate(curves):
        if not isinstance(c, dict):
            continue
        meta_dict: dict[str, Any] = {}
        for m in c.get("metadata") or []:
            if isinstance(m, dict) and "key" in m:
                meta_dict[f"meta.{m['key']}"] = m.get("value")
        for s in c.get("series") or []:
            if not isinstance(s, dict):
                continue
            values = s.get("values")
            if not isinstance(values, list):
                continue
            series_name = s.get("name")
            series_unit = s.get("unit")
            for vi, v in enumerate(values):
                rows.append(
                    {
                        "curve_idx": ci,
                        "curve_id": c.get("id"),
                        "curve_name": c.get("name"),
                        "series_name": series_name,
                        "series_unit": series_unit,
                        "value_idx": vi,
                        "value": v,
                        **meta_dict,
                    }
                )
    if not rows:
        return None
    df = pd.DataFrame(rows)
    pkl_path = snapshot_dir / "curves.pkl"
    df.to_pickle(pkl_path)
    return {
        "name": "curves.pkl",
        "bytes": pkl_path.stat().st_size,
        "row_count": int(len(df)),
        "columns": list(df.columns),
    }


def list_all() -> list[dict]:
    """Return all snapshots from both root and all folders.

    Each entry includes a ``folder_name`` key (``str`` if foldered,
    ``None`` if at root).
    """
    _ensure_root()
    out: list[dict] = []

    def _read_snapshot(d: Path, folder: str | None) -> dict:
        meta_path = d / "meta.json"
        if not meta_path.exists():
            return {
                "handle": d.name.removeprefix("snapshot_"),
                "path": str(d),
                "corrupt": True,
                "folder_name": folder,
            }
        try:
            meta = json.loads(meta_path.read_text())
        except Exception as exc:
            meta = {"corrupt": True, "error": str(exc)}
        return {
            "handle": meta.get("handle", d.name.removeprefix("snapshot_")),
            "path": str(d),
            "meta": meta,
            "folder_name": folder,
        }

    # Root-level snapshots.
    for d in sorted(STORAGE_ROOT.iterdir()):
        if d.is_dir() and d.name.startswith("snapshot_"):
            out.append(_read_snapshot(d, None))

    # Foldered snapshots.
    if FOLDERS_ROOT.is_dir():
        for folder_dir in sorted(FOLDERS_ROOT.iterdir()):
            if not folder_dir.is_dir():
                continue
            for d in sorted(folder_dir.iterdir()):
                if d.is_dir() and d.name.startswith("snapshot_"):
                    out.append(_read_snapshot(d, folder_dir.name))

    return out


def get(handle: str) -> dict | None:
    """Locate and return a snapshot by handle.

    Searches root first, then all folders. Returns ``None`` if not found.
    Result includes ``folder_name``.
    """
    _ensure_root()
    snap_dir = _find_snapshot_dir(handle)
    if snap_dir is None:
        return None
    meta_path = snap_dir / "meta.json"
    if not meta_path.exists():
        return None
    return {
        "handle": handle,
        "path": str(snap_dir),
        "meta": json.loads(meta_path.read_text()),
        "folder_name": _folder_name_for_dir(snap_dir),
    }


def delete(handle: str) -> bool:
    """Delete a snapshot by handle, searching root then folders.

    Returns False if not found.
    """
    _ensure_root()
    snap_dir = _find_snapshot_dir(handle)
    if snap_dir is None:
        return False
    shutil.rmtree(snap_dir)
    return True


def clear() -> dict:
    """Remove every snapshot (root + foldered). Returns total bytes freed."""

    _ensure_root()
    freed = 0
    removed = 0

    def _nuke(d: Path) -> None:
        nonlocal freed, removed
        for f in d.rglob("*"):
            if f.is_file():
                freed += f.stat().st_size
        shutil.rmtree(d)
        removed += 1

    # Root snapshots.
    for d in list(STORAGE_ROOT.iterdir()):
        if d.is_dir() and d.name.startswith("snapshot_"):
            _nuke(d)

    # Foldered snapshots.
    if FOLDERS_ROOT.is_dir():
        for folder_dir in list(FOLDERS_ROOT.iterdir()):
            if not folder_dir.is_dir():
                continue
            for d in list(folder_dir.iterdir()):
                if d.is_dir() and d.name.startswith("snapshot_"):
                    _nuke(d)

    return {"freed_bytes": freed, "removed_snapshots": removed}
