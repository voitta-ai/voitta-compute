"""Briefcase entry point for the Voitta Compute .app bundle.

Responsibilities (all before importing any app.* module):
  1. Bail out if we're a multiprocessing spawn child.
  2. Prepare the writable user data dir under ~/Library/Application Support/.
  3. Set VOITTA_PROJECT_ROOT so app.config resolves paths to the user data dir.
  4. Configure PIP_PREFIX so lazy installs land in userbase/, not the bundle.
  5. Redirect stdout/stderr to voitta.log (visible in Console.app).
  6. Seed resources (frontend, docs, plugins, lib-sources) into the user data dir.
  7. Hand off to app.desktop.main().
"""

from __future__ import annotations

import multiprocessing
import os
import shutil
import sys
from pathlib import Path

# freeze_support() must be at module level in a frozen executable so that
# multiprocessing spawn children (which re-run this module) exit immediately
# after running their target function without touching Cocoa or any UI code.
multiprocessing.freeze_support()


# ---------------------------------------------------------------------------
# Multiprocessing / subprocess guard
# ---------------------------------------------------------------------------

def _is_mp_child() -> bool:
    """Return True if this process was spawned by Python's multiprocessing."""
    # Standard spawn marker added to sys.argv by multiprocessing.spawn
    if any("--multiprocessing-fork" in a for a in sys.argv):
        return True
    # Older macOS / fork-server style
    if os.environ.get("MULTIPROCESSING_FORKING_DISABLE"):
        return True
    return False


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

_APP_SUPPORT_NAME = "Voitta Compute"


def _user_data_dir() -> Path:
    return Path.home() / "Library" / "Application Support" / _APP_SUPPORT_NAME


def _bundle_resources_dir() -> Path:
    """Resources subtree shipped inside the .app bundle."""
    try:
        import voitta_compute
        return Path(voitta_compute.__file__).resolve().parent / "resources"
    except Exception:
        pass
    rp = os.environ.get("RESOURCEPATH")
    if rp:
        return Path(rp)
    return Path(sys.executable).resolve().parent.parent / "Resources"


# ---------------------------------------------------------------------------
# Seeding helpers
# ---------------------------------------------------------------------------

def _seed_overwrite(src: Path, dst: Path) -> None:
    """Copy src → dst, overwriting every file. For frontend/docs/plugins."""
    if not src.is_dir():
        return
    dst.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        target = dst / item.name
        if item.is_dir():
            shutil.copytree(item, target, dirs_exist_ok=True)
        else:
            shutil.copy2(item, target)


def _seed_lib_sources(src: Path, dst: Path, stamp_src: Path) -> None:
    """Seed lib-sources only when the submodule stamp changed.

    Avoids copying ~80 MB of text files on every launch when nothing changed.
    """
    if not src.is_dir():
        return
    if not stamp_src.is_file():
        return
    new_stamp = stamp_src.read_text(encoding="utf-8").strip()
    deployed_stamp = dst / ".sources_version"
    if deployed_stamp.is_file() and deployed_stamp.read_text(encoding="utf-8").strip() == new_stamp:
        return  # already up to date
    shutil.copytree(src, dst, dirs_exist_ok=True)
    deployed_stamp.write_text(new_stamp, encoding="utf-8")


def _migrate_settings() -> None:
    """One-time migration: copy settings from old Voitta Chainlit config dir."""
    old_config = Path.home() / ".config" / "voitta-bookmarklet-chainlit" / "settings.json"
    new_config = Path.home() / ".config" / "voitta-compute" / "settings.json"
    if old_config.is_file() and not new_config.is_file():
        new_config.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(old_config, new_config)

    old_app_support = Path.home() / "Library" / "Application Support" / "Voitta Chainlit"
    new_app_support = Path.home() / "Library" / "Application Support" / "Voitta Compute"
    if old_app_support.is_dir() and not new_app_support.exists():
        shutil.copytree(old_app_support, new_app_support, dirs_exist_ok=False)


def _prepare_user_data_dir() -> Path:
    _migrate_settings()
    user = _user_data_dir()
    user.mkdir(parents=True, exist_ok=True)

    res = _bundle_resources_dir()

    # frontend/dist — overwrite every launch so .app upgrades propagate.
    _seed_overwrite(res / "frontend_dist", user / "frontend" / "dist")

    # docs + plugins — replace every launch (small, fast). rmtree first so
    # removed or reorganised files don't survive across .app upgrades.
    _seed_overwrite(res / "docs", user / "docs")
    plugins_dst = user / "plugins"
    if plugins_dst.exists():
        shutil.rmtree(plugins_dst)
    _seed_overwrite(res / "plugins", plugins_dst)

    # lib-sources — stamp-gated copy (may be large).
    _seed_lib_sources(
        res / "lib-sources",
        user / "lib-sources",
        res / "code_sources_version.txt",
    )

    # Ensure writable scaffolding exists.
    (user / "backend" / "certs").mkdir(parents=True, exist_ok=True)
    (user / "scripts" / "compute").mkdir(parents=True, exist_ok=True)
    (user / "scripts" / "reports").mkdir(parents=True, exist_ok=True)

    return user


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _acquire_instance_lock() -> bool:
    """Return True if this is the first (and only) running instance.

    Uses a lock file + flock so the lock releases automatically when the
    process exits, even on a crash.
    """
    import fcntl
    lock_path = Path.home() / "Library" / "Application Support" / _APP_SUPPORT_NAME / ".voitta.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        # Keep the file descriptor alive for the process lifetime by storing
        # it on the module — if it goes out of scope the lock is released.
        _acquire_instance_lock._fd = lock_path.open("w")
        fcntl.flock(_acquire_instance_lock._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except (OSError, BlockingIOError):
        return False


def main() -> int:
    if _is_mp_child():
        return 0

    if not _acquire_instance_lock():
        # Another instance is already running — silently exit.
        return 0

    user = _prepare_user_data_dir()

    # VOITTA_PROJECT_ROOT = user_data/backend so that app.config computes:
    #   PROJECT_ROOT     = <user>/backend
    #   TLS_CERT_PATH    = <user>/backend/certs/...
    #   RAG_DIR          = <user>/rag
    #   PLUGINS_DIR      = <user>/plugins
    #   DOCS_DIR         = <user>/docs
    os.environ["VOITTA_PROJECT_ROOT"] = str(user / "backend")

    # USER_DATA_ROOT resolves from VOITTA_DATA_ROOT (see app.config). In the
    # bundle it's the same <user>/backend dir as PROJECT_ROOT; set it
    # explicitly so the config default (a bare macOS path) is never relied on.
    os.environ["VOITTA_DATA_ROOT"] = str(user / "backend")

    # Route pip installs to userbase/ — prefix layout so pip's resolver
    # sees already-bundled packages and skips them (saves ~300 MB vs --target).
    py_dir = f"python{sys.version_info.major}.{sys.version_info.minor}"
    user_prefix = user / "userbase"
    user_site = user_prefix / "lib" / py_dir / "site-packages"
    user_site.mkdir(parents=True, exist_ok=True)
    os.environ["PIP_PREFIX"] = str(user_prefix)
    if str(user_site) not in sys.path:
        sys.path.insert(0, str(user_site))

    # Redirect stdout/stderr to voitta.log (frozen .app has no terminal).
    # Open in "w" mode to start fresh on each launch — old logs are stale.
    log_path = user / "voitta.log"
    try:
        log_fp = log_path.open("w", buffering=1, encoding="utf-8")
        sys.stdout = log_fp
        sys.stderr = log_fp
    except OSError:
        pass

    # Clear MCP dump files from the previous session.
    dumps_dir = user / "mcp_dumps"
    try:
        if dumps_dir.exists():
            shutil.rmtree(dumps_dir)
        dumps_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

    from app.desktop import main as desktop_main
    return desktop_main()


if __name__ == "__main__":
    sys.exit(main())
