"""Seed the curated compute + report scripts into the writable user
data dir at first launch.

Briefcase ships every file under ``src/voitta/resources/`` inside the
read-only .app bundle. ``app.services.scripts`` writes script source
+ run state to ``<PROJECT_ROOT>/python_storage/{compute,reports,flows}/``
(writable user data dir), so on first launch the writable dir is empty
and the LLM can't call the canonical ``dat_parse`` / ``a4db_parse``
compute scripts (or open their reports).

Solution: at first launch — same flow as the RAG build — copy the
bundled seed_scripts/ tree out to ``<PROJECT_ROOT>/python_storage/``.
We do not overwrite existing files, so a user who edited a seed
script keeps their edits across upgrades.

Idempotent: ``status_summary()`` reflects what's currently on disk and
``seed()`` is a no-op when every expected script is already present.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Callable

from app.config import PROJECT_ROOT


# Same callback shape as installer.install_all so the install window's
# set_progress(current, total, label, log) works without adapter code.
ProgressCb = Callable[[int, int, str, "str | None"], None]


def _seed_source_dir() -> Path | None:
    """Resolve the bundled ``seed_scripts/`` directory.

    Source-checkout: ``<repo>/src/voitta/resources/seed_scripts/`` (only
    exists once ``build_app.sh`` has been run; in pure-dev mode the
    real ``<repo>/python_storage/{compute,reports}/`` is already on
    disk so seeding is a no-op).
    Packaged .app: same path inside the bundle, found via the ``voitta``
    package's ``__file__``.
    """
    try:
        import voitta
    except ImportError:
        return None
    candidate = Path(voitta.__file__).resolve().parent / "resources" / "seed_scripts"
    return candidate if candidate.is_dir() else None


def _target_dirs() -> tuple[Path, Path]:
    # Scripts live under ``python_storage/`` alongside the snapshot
    # cache — single unified state dir. See
    # ``app.services.scripts.SCRIPTS_ROOT``.
    target = PROJECT_ROOT / "python_storage"
    return target / "compute", target / "reports"


def _expected_scripts(seed_dir: Path) -> list[tuple[str, str]]:
    """Walk the bundled seed_scripts/ to discover what to copy.

    Returns ``[(kind, slug), ...]`` so we don't hardcode the curated
    list in two places (``build_app.sh`` decides what gets staged).
    """
    pairs: list[tuple[str, str]] = []
    for kind in ("compute", "reports"):
        kind_dir = seed_dir / kind
        if not kind_dir.is_dir():
            continue
        for slug_dir in kind_dir.iterdir():
            if not slug_dir.is_dir():
                continue
            if not (slug_dir / "code.py").is_file():
                continue
            pairs.append((kind, slug_dir.name))
    return sorted(pairs)


def status_summary() -> dict:
    """Snapshot for the Settings menu / health route.

    Reports which seed scripts are present on disk vs. expected from
    the bundle. Cheap (filesystem stat only); no script execution.
    """
    seed_dir = _seed_source_dir()
    expected = _expected_scripts(seed_dir) if seed_dir else []
    target_compute, target_reports = _target_dirs()
    target_for = {"compute": target_compute, "reports": target_reports}
    present: list[tuple[str, str]] = []
    missing: list[tuple[str, str]] = []
    for kind, slug in expected:
        if (target_for[kind] / slug / "code.py").is_file():
            present.append((kind, slug))
        else:
            missing.append((kind, slug))
    return {
        "expected": expected,
        "present": present,
        "missing": missing,
        "all_seeded": not missing,
    }


def seed(progress_cb: ProgressCb | None = None) -> bool:
    """Copy missing seed scripts from bundle → user data dir.

    Only writes scripts that don't already exist (preserves user
    edits). Returns True on full success or no-op, False if the seed
    source is missing entirely (``seed_scripts/`` dir not in bundle —
    means ``build_app.sh`` skipped the staging step).

    The progress callback is optional; when supplied it ticks once per
    seed script copied (or skipped because already present).
    """
    seed_dir = _seed_source_dir()
    if seed_dir is None:
        if progress_cb:
            progress_cb(0, 1, "Scripts: bundle has no seed dir, skipping", None)
        return False

    expected = _expected_scripts(seed_dir)
    if not expected:
        if progress_cb:
            progress_cb(0, 1, "Scripts: nothing to seed", None)
        return True

    target_compute, target_reports = _target_dirs()
    target_for = {"compute": target_compute, "reports": target_reports}
    target_compute.mkdir(parents=True, exist_ok=True)
    target_reports.mkdir(parents=True, exist_ok=True)

    for i, (kind, slug) in enumerate(expected):
        dst = target_for[kind] / slug
        src = seed_dir / kind / slug
        if (dst / "code.py").is_file():
            if progress_cb:
                progress_cb(
                    i, len(expected),
                    f"Scripts: {kind}/{slug} already present (kept)",
                    None,
                )
            continue
        try:
            shutil.copytree(src, dst)
            if progress_cb:
                progress_cb(
                    i, len(expected),
                    f"Scripts: seeded {kind}/{slug}",
                    f">>> cp -R {src.name} → {dst}",
                )
        except OSError as exc:
            if progress_cb:
                progress_cb(
                    i, len(expected),
                    f"Scripts: failed to seed {kind}/{slug} ({exc})",
                    f"!!! {exc}",
                )
            return False

    if progress_cb:
        progress_cb(
            len(expected), len(expected),
            f"Scripts: {len(expected)} seed scripts ready",
            "<<< seed complete",
        )
    return True
