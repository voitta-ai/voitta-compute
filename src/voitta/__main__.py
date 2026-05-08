"""Briefcase invokes ``python -m voitta`` inside the bundle. We hand
off to ``app.desktop_launcher.main()`` which sets the data-dir env var
and starts the rumps menu-bar app.

CRITICAL: this file is ALSO re-executed by every multiprocessing-spawn
worker chromadb / sentence-transformers spin up. macOS spawn launches
a fresh Python interpreter that calls ``runpy.run_module('voitta')``
and then dispatches into the worker's target function via
``multiprocessing.spawn.spawn_main``. If that re-execution flows into
``app.desktop_launcher.main`` it ALSO calls ``rumps.App().run()`` and
tries to bind uvicorn — duplicate icon + EADDRINUSE.

We diagnose with a launch-marker log dumped before any ``app.*``
import, so a `voitta-launch.log` accumulates one record per Python
process the .app spawns. The marker captures argv, ppid, env vars
multiprocessing sets, ``parent_process()``, and ``sys._base_executable``
— enough to figure out why a child slipped past our guard.
"""

import os
import sys


def _launch_diag(stage: str) -> None:
    """Append a JSON-ish line to a per-launch diagnostic log.

    Runs before any ``app.*`` import so it works in spawn children too.
    The log path is ``<user-data-dir>/voitta-launch.log`` — same dir
    convention as ``voitta.log``. We open in append+line-buffered mode
    so concurrent writes from parent + spawn child don't corrupt each
    other (POSIX guarantees atomic writes < PIPE_BUF for short lines).
    """
    try:
        import datetime, json
        # Mirror app.config.PROJECT_ROOT logic without importing config:
        # config does a heavy chain of imports; this guard runs before
        # any of that. Falls back to ~/Library/... default.
        env_root = os.environ.get("VOITTA_PROJECT_ROOT")
        if env_root:
            base = env_root
        else:
            base = os.path.expanduser(
                "~/Library/Application Support/Voitta Bookmarklet"
            )
        os.makedirs(base, exist_ok=True)
        record = {
            "ts": datetime.datetime.now().isoformat(timespec="milliseconds"),
            "stage": stage,
            "pid": os.getpid(),
            "ppid": os.getppid(),
            "argv": sys.argv,
            "executable": sys.executable,
            "base_executable": getattr(sys, "_base_executable", None),
            "frozen": getattr(sys, "frozen", False),
            "env_mp_fork_disable": os.environ.get("MULTIPROCESSING_FORKING_DISABLE"),
            "env_voitta_frozen": os.environ.get("VOITTA_FROZEN"),
        }
        try:
            import multiprocessing
            pp = multiprocessing.parent_process()
            record["mp_parent_process"] = (
                f"{pp.name} pid={pp.pid}" if pp is not None else None
            )
        except Exception as exc:
            record["mp_parent_process_error"] = repr(exc)
        with open(os.path.join(base, "voitta-launch.log"), "a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        # Never let diagnostics crash the launcher.
        pass


def _is_spawn_worker() -> bool:
    """True if this Python interpreter was launched as a multiprocessing
    spawn child. Detection mirrors CPython's own logic in
    ``multiprocessing/spawn.py``: the spawn bootstrap inserts
    ``--multiprocessing-fork`` into sys.argv before invoking the worker.
    We also check for env vars some libraries set when manually
    spawning helpers."""
    for arg in sys.argv[1:]:
        if "multiprocessing-fork" in arg or "spawn_main" in arg:
            return True
        if "resource_tracker" in arg:
            return True  # cleanup tracker — never run user code
    if os.environ.get("MULTIPROCESSING_FORKING_DISABLE"):
        return True
    try:
        import multiprocessing
        if multiprocessing.parent_process() is not None:
            return True
    except Exception:
        pass
    return False


_launch_diag("voitta_main_entered")

# ============================================================================
# Spawn-worker fast path: detect, dispatch, exit. NO ``app.*`` imports here —
# the worker re-creating a rumps App and binding uvicorn is exactly the bug.
# ============================================================================
if _is_spawn_worker():
    _launch_diag("spawn_worker_detected")
    import multiprocessing
    multiprocessing.freeze_support()
    _launch_diag("spawn_worker_freeze_support_returned")
    # ``freeze_support`` runs the worker target and returns; we exit
    # immediately rather than fall through to the parent flow.
    sys.exit(0)


# ============================================================================
# Parent process — the real launcher.
# ============================================================================
_launch_diag("parent_proceeding_to_main")

import multiprocessing
multiprocessing.freeze_support()  # No-op in the parent; required for safety.

from app.desktop_launcher import main


if __name__ == "__main__":
    sys.exit(main())
