"""On-disk CRUD for scripts.

Layout:

    scripts/
      {slug}/                      ← unfoldered script
          code.py
          meta.json
      folders/
        {folder_name}/
          folder.json              ← {"name", "description", "color", "created_at"}
          {slug}/
              code.py
              meta.json
      scripts_state/               ← stays flat, never moves

All writes are atomic: temp file in the same parent directory, then
``os.replace`` for a same-filesystem rename. Concurrent writers can
race on which one "wins" the final state, but the file is always
either the old contents or the new contents — never half-written.

Folder names must match ``[a-z0-9_-]{1,64}``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from app.reports.paths import SCRIPTS_DIR, SCRIPTS_FOLDERS_DIR
from app.reports.slug import resolve_under, validate_slug

META_FILENAME = "meta.json"
CODE_FILENAME = "code.py"


@dataclass
class Meta:
    """Per-script metadata persisted alongside the source."""

    schema_version: int = 1
    name: str = ""
    created_at: str = ""
    updated_at: str = ""
    last_run_at: Optional[str] = None
    # Populated after the first successful run; one of the strings the
    # detect.py sniffer can produce. ``None`` until then.
    last_kind: Optional[str] = None
    last_ok: Optional[bool] = None
    extra: dict[str, Any] = field(default_factory=dict)
    # None when at root; folder name string when foldered.
    folder_name: Optional[str] = field(default=None)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # NamedTemporaryFile(delete=False) so we own the rename; same dir
    # so the rename is atomic (same filesystem).
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        # Best-effort cleanup; the next write will overwrite it anyway.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _validate_folder_name(name: str) -> None:
    if not re.fullmatch(r'[a-z0-9_-]{1,64}', name):
        raise ValueError(
            f"Invalid folder name {name!r}: use [a-z0-9_-], max 64 chars"
        )


def _now_iso_fs() -> str:
    """ISO timestamp for folder.json ``created_at``."""
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Folder management
# ---------------------------------------------------------------------------

def create_folder(name: str, description: str = "", color: str = "") -> dict:
    """Create a new script folder.

    Raises ``ValueError`` for invalid names or if the folder already exists.
    Returns the folder metadata dict.
    """
    _validate_folder_name(name)
    SCRIPTS_FOLDERS_DIR.mkdir(parents=True, exist_ok=True)
    folder_dir = SCRIPTS_FOLDERS_DIR / name
    if folder_dir.exists():
        raise ValueError(f"Folder {name!r} already exists")
    folder_dir.mkdir(parents=True)
    data = {
        "name": name,
        "description": description,
        "color": color,
        "created_at": _now_iso_fs(),
    }
    (folder_dir / "folder.json").write_text(
        json.dumps(data, indent=2) + "\n"
    )
    return data


def list_folders() -> list[dict]:
    """Return all folder metadata dicts, each with a ``script_count`` key."""
    if not SCRIPTS_FOLDERS_DIR.is_dir():
        return []
    out: list[dict] = []
    for folder_dir in sorted(SCRIPTS_FOLDERS_DIR.iterdir()):
        if not folder_dir.is_dir():
            continue
        folder_json = folder_dir / "folder.json"
        if not folder_json.exists():
            continue
        try:
            data = json.loads(folder_json.read_text())
        except Exception:
            continue
        count = sum(
            1
            for d in folder_dir.iterdir()
            if d.is_dir() and not d.name.startswith(".")
            and (d / CODE_FILENAME).is_file()
        )
        data["script_count"] = count
        out.append(data)
    return out


def delete_folder(name: str) -> bool:
    """Delete a folder, orphaning its scripts back to SCRIPTS_DIR root.

    Returns False if the folder doesn't exist.
    """
    folder_dir = SCRIPTS_FOLDERS_DIR / name
    if not folder_dir.is_dir():
        return False
    # Move script dirs back to root.
    for d in list(folder_dir.iterdir()):
        if d.is_dir() and not d.name.startswith(".") and d.name != "folder.json":
            if (d / CODE_FILENAME).is_file():
                dest = SCRIPTS_DIR / d.name
                shutil.move(str(d), str(dest))
    shutil.rmtree(folder_dir)
    return True


def move_script_to_folder(slug: str, folder_name: str | None) -> bool:
    """Move a script to a folder (or back to root when folder_name is None).

    Returns False if the slug is not found.
    """
    validate_slug(slug)
    src = _find_script_dir(slug)
    if src is None:
        return False

    if folder_name is None:
        dest = SCRIPTS_DIR / slug
    else:
        _validate_folder_name(folder_name)
        folder_dir = SCRIPTS_FOLDERS_DIR / folder_name
        if not folder_dir.is_dir():
            raise ValueError(f"Folder {folder_name!r} does not exist")
        dest = folder_dir / slug

    if src == dest:
        return True

    shutil.move(str(src), str(dest))
    return True


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _find_script_dir(slug: str) -> Path | None:
    """Locate the directory for *slug*, searching root then all folders."""
    root_candidate = SCRIPTS_DIR / slug
    if root_candidate.is_dir() and (root_candidate / CODE_FILENAME).is_file():
        return root_candidate
    if SCRIPTS_FOLDERS_DIR.is_dir():
        for folder_dir in SCRIPTS_FOLDERS_DIR.iterdir():
            if not folder_dir.is_dir():
                continue
            candidate = folder_dir / slug
            if candidate.is_dir() and (candidate / CODE_FILENAME).is_file():
                return candidate
    return None


def _folder_name_for_script_dir(d: Path) -> str | None:
    """Return the folder name if *d* lives inside SCRIPTS_FOLDERS_DIR, else None."""
    try:
        rel = d.relative_to(SCRIPTS_FOLDERS_DIR)
        parts = rel.parts
        if len(parts) >= 1:
            return parts[0]
    except ValueError:
        pass
    return None


def script_dir(slug: str, *, root: Path | None = None) -> Path:
    """Return the directory for *slug*.

    When *root* is given (test override), falls back to
    ``resolve_under(root, slug)`` only (legacy behaviour).

    Without *root*, searches both SCRIPTS_DIR root and all folders,
    returning the existing location. Falls back to
    ``SCRIPTS_DIR/{slug}`` (for new / not-yet-created scripts).
    """
    if root is not None:
        return resolve_under(root, slug)
    found = _find_script_dir(slug)
    if found is not None:
        return found
    # Not found — return canonical new location.
    return SCRIPTS_DIR / slug


def exists(slug: str, *, root: Path | None = None) -> bool:
    if root is not None:
        d = resolve_under(root, slug)
        return (d / CODE_FILENAME).is_file()
    return _find_script_dir(slug) is not None


def read_code(slug: str, *, root: Path | None = None) -> str:
    return (script_dir(slug, root=root) / CODE_FILENAME).read_text()


def read_meta(slug: str, *, root: Path | None = None) -> Meta:
    d = script_dir(slug, root=root)
    path = d / META_FILENAME
    folder = _folder_name_for_script_dir(d) if root is None else None
    if not path.is_file():
        # Tolerate a missing meta — could happen if a user dropped a
        # script in by hand. Build a fresh one from the code mtime.
        code_path = d / CODE_FILENAME
        ts = (
            datetime.fromtimestamp(code_path.stat().st_mtime, tz=timezone.utc).isoformat()
            if code_path.is_file()
            else _now_iso()
        )
        return Meta(name=slug, created_at=ts, updated_at=ts, folder_name=folder)
    data = json.loads(path.read_text())
    return Meta(
        schema_version=int(data.get("schema_version", 1)),
        name=str(data.get("name", slug)),
        created_at=str(data.get("created_at", "")),
        updated_at=str(data.get("updated_at", "")),
        last_run_at=data.get("last_run_at"),
        last_kind=data.get("last_kind"),
        last_ok=data.get("last_ok"),
        extra={k: v for k, v in data.items() if k not in _META_FIELDS},
        folder_name=folder,
    )


_META_FIELDS = {
    "schema_version",
    "name",
    "created_at",
    "updated_at",
    "last_run_at",
    "last_kind",
    "last_ok",
}


def write_script(
    slug: str,
    code: str,
    *,
    root: Path | None = None,
    folder_name: str | None = None,
) -> Meta:
    """Persist ``code`` for ``slug``. Updates timestamps in meta.

    Creates ``meta.json`` on first write; preserves ``created_at`` on
    subsequent writes.

    If ``folder_name`` is given the script is placed (or kept) inside
    ``SCRIPTS_FOLDERS_DIR/{folder_name}/{slug}/``.  When the script
    already exists at a different location it is written in-place (use
    ``move_script_to_folder`` to relocate).
    """
    validate_slug(slug)

    if root is not None:
        # Legacy test-override path — ignore folder_name.
        d = resolve_under(root, slug)
        now = _now_iso()
        meta = read_meta(slug, root=root) if (d / CODE_FILENAME).is_file() else Meta(
            name=slug, created_at=now, updated_at=now
        )
        meta.name = slug
        meta.updated_at = now
        if not meta.created_at:
            meta.created_at = now
        _atomic_write(d / CODE_FILENAME, code.encode("utf-8"))
        _write_meta(slug, meta, root=root)
        return meta

    # Determine target directory.
    if folder_name is not None:
        _validate_folder_name(folder_name)
        SCRIPTS_FOLDERS_DIR.mkdir(parents=True, exist_ok=True)
        folder_dir = SCRIPTS_FOLDERS_DIR / folder_name
        if not folder_dir.is_dir():
            raise ValueError(f"Folder {folder_name!r} does not exist")
        d = folder_dir / slug
    else:
        existing = _find_script_dir(slug)
        d = existing if existing is not None else SCRIPTS_DIR / slug

    now = _now_iso()
    meta = read_meta(slug) if (d / CODE_FILENAME).is_file() else Meta(
        name=slug, created_at=now, updated_at=now
    )
    meta.name = slug
    meta.updated_at = now
    if not meta.created_at:
        meta.created_at = now
    _atomic_write(d / CODE_FILENAME, code.encode("utf-8"))
    _write_meta_to_dir(d, slug, meta)
    return meta


def _write_meta(slug: str, meta: Meta, *, root: Path | None = None) -> None:
    d = script_dir(slug, root=root)
    _write_meta_to_dir(d, slug, meta)


def _write_meta_to_dir(d: Path, slug: str, meta: Meta) -> None:
    payload = asdict(meta)
    # folder_name is runtime-only; don't persist it in meta.json.
    payload.pop("folder_name", None)
    extra = payload.pop("extra", {}) or {}
    payload.update(extra)
    _atomic_write(d / META_FILENAME, (json.dumps(payload, indent=2) + "\n").encode("utf-8"))


def update_meta(slug: str, **fields: Any) -> Meta:
    """Patch select meta fields. Used by the dispatcher after a run."""
    meta = read_meta(slug)
    for k, v in fields.items():
        if k in _META_FIELDS:
            setattr(meta, k, v)
        else:
            meta.extra[k] = v
    _write_meta(slug, meta)
    return meta


def list_scripts(*, root: Path | None = None) -> list[Meta]:
    """List all scripts from root and all folders.

    Each ``Meta`` entry has ``folder_name`` set (``None`` for root scripts).
    When *root* is given (test override), only that tree is scanned
    and ``folder_name`` is always ``None``.
    """
    if root is not None:
        base = root
        if not base.is_dir():
            return []
        out: list[Meta] = []
        for entry in sorted(base.iterdir()):
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            if not (entry / CODE_FILENAME).is_file():
                continue
            try:
                out.append(read_meta(entry.name, root=root))
            except Exception:
                continue
        return out

    out = []

    # Root-level scripts.
    if SCRIPTS_DIR.is_dir():
        for entry in sorted(SCRIPTS_DIR.iterdir()):
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            # Skip the folders/ subdirectory itself.
            if entry.name == "folders":
                continue
            if not (entry / CODE_FILENAME).is_file():
                continue
            try:
                m = read_meta(entry.name)
                out.append(m)
            except Exception:
                continue

    # Foldered scripts.
    if SCRIPTS_FOLDERS_DIR.is_dir():
        for folder_dir in sorted(SCRIPTS_FOLDERS_DIR.iterdir()):
            if not folder_dir.is_dir():
                continue
            for entry in sorted(folder_dir.iterdir()):
                if not entry.is_dir() or entry.name.startswith("."):
                    continue
                if not (entry / CODE_FILENAME).is_file():
                    continue
                try:
                    m = read_meta(entry.name)
                    out.append(m)
                except Exception:
                    continue

    return out


def delete_script(slug: str, *, root: Path | None = None) -> bool:
    """Remove the script directory for *slug*.

    Returns True on a real removal, False if the slug didn't exist.
    Idempotent — a missing slug is **not** an error.

    When the script is foldered, ``shutil.rmtree`` is used directly
    (the spec permits this). For root scripts with no subdirs the
    original careful unlink-then-rmdir path is used.
    """
    if root is not None:
        d = resolve_under(root, slug)
        if not d.exists():
            return False
        for child in d.iterdir():
            if child.is_file() or child.is_symlink():
                child.unlink()
            else:
                raise OSError(f"unexpected non-file entry {child} under script dir")
        d.rmdir()
        return True

    d = _find_script_dir(slug)
    if d is None:
        return False

    # Use shutil.rmtree for simplicity (handles both flat and any future subdirs).
    shutil.rmtree(d)
    return True
