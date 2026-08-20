"""Projects — named, isolated workspaces under the user data root.

A project owns everything a piece of work accumulates: scripts,
python_storage snapshots, script run-state, chat uploads, per-project
thread history (via thread metadata tagging in the data layer), and a
PROJECT.md memory file injected into the system prompt each turn.

Layout::

    USER_DATA_ROOT[/users/<slug>]/
    ├── projects/
    │   ├── legacy/                  ← always exists; adopted pre-projects data
    │   │   ├── project.json         {"name": "Legacy", "created_at": …}
    │   │   ├── PROJECT.md           (created on first remember)
    │   │   ├── scripts/  python_storage/  scripts_state/  uploads/
    │   │   └── _archived/<slug>/    ← deleted projects, preserved verbatim
    │   └── <slug>/                  (same shape)
    └── (global, NOT project-scoped: settings.json, auth_secret,
        conversations.sqlite, claude_code/, logs/, voice/…)

Scoping mechanism: :func:`project_data_root` inserts one path segment
below :func:`current_user.user_data_root` — the same lazy-resolution
trick UserPath already plays for per-user scoping, one level deeper.
Only the four data trees above resolve through it; identity, settings
and the conversations DB deliberately do not.

Selection: the active project slug persists in the user's settings.json
(``activeProject``) so it survives restarts and is per-user in server
mode for free. Reads are cached with a short TTL (path resolution runs
on every file op); the switch route invalidates directly.

Legacy adoption: the first :func:`project_data_root` call on a data
root that predates projects moves the existing ``scripts/``,
``python_storage/``, ``scripts_state/`` and ``uploads/`` trees into
``projects/legacy/`` — once, under a lock, marked by legacy's
project.json. Old chat threads carry no project tag and are treated as
legacy by the data layer, so history + uploads stay consistent.
"""

from __future__ import annotations

import json
import re
import shutil
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from app.services.current_user import user_data_root

PROJECTS_DIRNAME = "projects"
LEGACY_SLUG = "legacy"
LEGACY_NAME = "Legacy"
ARCHIVE_DIRNAME = "_archived"

# The data trees that belong to a project (moved on adoption; everything
# else under the user root stays global).
_SCOPED_TREES = ("scripts", "python_storage", "scripts_state", "uploads")

_SLUG_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")

_MEMORY_FILENAME = "PROJECT.md"
_MEMORY_MAX_BYTES = 64 * 1024  # keep the system-prompt injection bounded

_adopt_lock = threading.Lock()

# active-project cache: settings path → (slug, expires_at). Path ops
# resolve through this on every access, so a raw settings.json read per
# op would be prohibitive; 2 s staleness is invisible in practice and
# set_active_project() invalidates directly.
_active_cache: dict[str, tuple[str, float]] = {}
_ACTIVE_TTL_S = 2.0


class UnknownProject(ValueError):
    """Raised when a project slug matches no existing project."""


@dataclass
class Project:
    slug: str
    name: str
    created_at: str
    is_legacy: bool


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate_slug(slug: str) -> None:
    if not _SLUG_RE.fullmatch(slug or ""):
        raise ValueError(
            f"invalid project slug {slug!r}: use [a-z0-9_-], start with a "
            "letter or digit, max 64 chars"
        )
    if slug == ARCHIVE_DIRNAME:
        raise ValueError(f"{ARCHIVE_DIRNAME!r} is reserved")


def slug_from_name(name: str) -> str:
    """Derive a filesystem slug from a display name."""
    s = re.sub(r"[^a-z0-9_-]+", "-", (name or "").strip().lower()).strip("-_")
    return s[:64] or "project"


# ---- roots ------------------------------------------------------------------


def projects_root() -> Path:
    return Path(user_data_root()) / PROJECTS_DIRNAME


def project_dir(slug: str) -> Path:
    return projects_root() / slug


def project_data_root() -> Path:
    """Root for the ACTIVE project's data trees. Ensures adoption ran
    for this user root. This is the resolver behind every scoped
    UserPath — keep it cheap (adoption check is one stat once warm)."""
    _ensure_adopted()
    return project_dir(active_project())


# ---- adoption ---------------------------------------------------------------


def _legacy_marker() -> Path:
    return project_dir(LEGACY_SLUG) / "project.json"


def _ensure_adopted() -> None:
    if _legacy_marker().is_file():
        return
    with _adopt_lock:
        if _legacy_marker().is_file():  # lost the race — fine
            return
        root = Path(user_data_root())
        legacy = project_dir(LEGACY_SLUG)
        legacy.mkdir(parents=True, exist_ok=True)
        for tree in _SCOPED_TREES:
            src = root / tree
            dst = legacy / tree
            if src.is_dir() and not dst.exists():
                try:
                    shutil.move(str(src), str(dst))
                except Exception:
                    # Never brick startup on a half-movable tree (open
                    # file handles etc.) — leave the remainder in place;
                    # the next call retries the missing pieces.
                    pass
        _write_project_json(legacy, LEGACY_NAME, created_at=_now_iso())


def _write_project_json(d: Path, name: str, *, created_at: str) -> None:
    d.mkdir(parents=True, exist_ok=True)
    payload = {"name": name, "created_at": created_at}
    tmp = d / ".project.json.tmp"
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    tmp.replace(d / "project.json")


# ---- active project -----------------------------------------------------------


def active_project() -> str:
    """The active project slug for the current user (settings-backed,
    TTL-cached). Falls back to legacy when unset or dangling."""
    from app.services import user_settings

    key = str(user_settings._settings_path())
    hit = _active_cache.get(key)
    now = time.monotonic()
    if hit and hit[1] > now:
        return hit[0]
    slug = LEGACY_SLUG
    try:
        raw = user_settings.read().get("activeProject")
        if isinstance(raw, str) and raw and (projects_root() / raw / "project.json").is_file():
            slug = raw
    except Exception:
        pass
    _active_cache[key] = (slug, now + _ACTIVE_TTL_S)
    return slug


def set_active_project(slug: str) -> None:
    """Switch the active project (persisted per user). Raises
    UnknownProject for a slug with no project.json."""
    from app.services import user_settings

    _ensure_adopted()
    if not (project_dir(slug) / "project.json").is_file():
        raise UnknownProject(f"no project {slug!r}")
    blob = user_settings.read()
    blob["activeProject"] = slug
    user_settings.write(blob)
    _active_cache[str(user_settings._settings_path())] = (
        slug, time.monotonic() + _ACTIVE_TTL_S,
    )


# ---- registry -----------------------------------------------------------------


def _read_project(d: Path) -> Optional[Project]:
    pj = d / "project.json"
    if not pj.is_file():
        return None
    try:
        data = json.loads(pj.read_text(encoding="utf-8"))
    except Exception:
        return None
    return Project(
        slug=d.name,
        name=str(data.get("name") or d.name),
        created_at=str(data.get("created_at") or ""),
        is_legacy=d.name == LEGACY_SLUG,
    )


def list_projects() -> list[Project]:
    """All projects, legacy first, then by creation order (dir mtime is
    unreliable; created_at from project.json)."""
    _ensure_adopted()
    out: list[Project] = []
    root = projects_root()
    if root.is_dir():
        for d in root.iterdir():
            if not d.is_dir() or d.name == ARCHIVE_DIRNAME:
                continue
            p = _read_project(d)
            if p is not None:
                out.append(p)
    out.sort(key=lambda p: (not p.is_legacy, p.created_at))
    return out


def get_project(slug: str) -> Project:
    _ensure_adopted()
    p = _read_project(project_dir(slug))
    if p is None:
        known = ", ".join(x.slug for x in list_projects())
        raise UnknownProject(f"no project {slug!r} — projects: {known}")
    return p


def create_project(name: str) -> Project:
    """Create a project from a display name; slug is derived and must be
    free. Returns the new project (not switched-to — callers decide)."""
    _ensure_adopted()
    name = (name or "").strip()
    if not name:
        raise ValueError("project name is required")
    slug = slug_from_name(name)
    validate_slug(slug)
    d = project_dir(slug)
    if (d / "project.json").is_file():
        raise ValueError(f"project {slug!r} already exists")
    _write_project_json(d, name, created_at=_now_iso())
    return get_project(slug)


def rename_project(slug: str, name: str) -> Project:
    """Display-name change only — the slug (dir name, thread tags) is
    immutable, same id/label split as Google accounts."""
    p = get_project(slug)
    name = (name or "").strip()
    if not name:
        raise ValueError("project name is required")
    _write_project_json(project_dir(slug), name, created_at=p.created_at)
    return get_project(slug)


def delete_project(slug: str) -> None:
    """Archive a project into legacy/_archived/<slug> — never destroys
    data. Legacy itself is undeletable. Deleting the active project
    switches back to legacy first."""
    if slug == LEGACY_SLUG:
        raise ValueError("the Legacy project cannot be deleted")
    get_project(slug)  # raises UnknownProject
    if active_project() == slug:
        set_active_project(LEGACY_SLUG)
    archive = project_dir(LEGACY_SLUG) / ARCHIVE_DIRNAME
    archive.mkdir(parents=True, exist_ok=True)
    dest = archive / slug
    n = 1
    while dest.exists():
        n += 1
        dest = archive / f"{slug}-{n}"
    shutil.move(str(project_dir(slug)), str(dest))


# ---- memory (PROJECT.md) -------------------------------------------------------


def memory_path(slug: Optional[str] = None) -> Path:
    return project_dir(slug or active_project()) / _MEMORY_FILENAME


def project_memory() -> str:
    """The active project's PROJECT.md contents ('' when absent). Size-
    capped defensively — this is injected into every system prompt."""
    _ensure_adopted()
    try:
        text = memory_path().read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except Exception:
        return ""
    if len(text.encode("utf-8")) > _MEMORY_MAX_BYTES:
        # Keep the tail — recent notes matter more than the preamble.
        return text.encode("utf-8")[-_MEMORY_MAX_BYTES:].decode("utf-8", "ignore")
    return text


def append_memory(text: str) -> Path:
    """Append a dated note to the active project's PROJECT.md."""
    text = (text or "").strip()
    if not text:
        raise ValueError("nothing to remember — text is empty")
    _ensure_adopted()
    path = memory_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with open(path, "a", encoding="utf-8") as f:
        if path.stat().st_size == 0:
            f.write(f"# Project notes — {get_project(active_project()).name}\n")
        f.write(f"\n- ({stamp}) {text}\n")
    return path


# ---- system prompt block -------------------------------------------------------


def system_prompt_block() -> str:
    """The per-turn injection: which project is active + its memory.
    Empty-memory projects still get the one-liner so the model knows
    the workspace context and that project_remember exists."""
    try:
        p = get_project(active_project())
    except Exception:
        return ""
    lines = [
        f"## Active project: {p.name}",
        "Scripts, data snapshots and files are scoped to this project. "
        "Use the `project_remember` tool to save durable facts about "
        "this project for future conversations.",
    ]
    mem = project_memory()
    if mem.strip():
        lines.append("\n### Project notes (PROJECT.md)\n" + mem.strip())
    return "\n".join(lines)


def _reset_caches_for_tests() -> None:
    _active_cache.clear()
