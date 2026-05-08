"""Briefcase invokes ``python -m voitta`` inside the bundle. We hand
off to ``app.desktop_launcher.main()`` which sets the data-dir env var
and starts the rumps menu-bar app.

CRITICAL: this file is also re-executed by every multiprocessing-spawn
worker chromadb / sentence-transformers spin up. The default macOS
spawn method launches a fresh Python interpreter that calls
``runpy.run_module('voitta')`` and then dispatches into the worker's
target function via ``multiprocessing.spawn.spawn_main``. If we let
that re-execution flow into ``app.desktop_launcher.main`` it ALSO
calls ``rumps.App().run()`` and tries to bind uvicorn — that's why
two menu bar icons appear and the second uvicorn fails with EADDRINUSE.

The multiprocessing-aware imports BELOW the early-return are imported
lazily so a spawn child has the absolute minimum import surface: just
``multiprocessing.freeze_support`` (which dispatches the worker target
and exits) plus this file's own argv inspection.
"""

import os
import sys


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
    if os.environ.get("MULTIPROCESSING_FORKING_DISABLE"):
        return True
    # ``parent_process()`` is the canonical check, but importing
    # multiprocessing here is cheap (stdlib) and worth the certainty.
    try:
        import multiprocessing
        if multiprocessing.parent_process() is not None:
            return True
    except Exception:
        pass
    return False


# ============================================================================
# Spawn-worker fast path: detect, dispatch, exit. NO ``app.*`` imports here —
# the worker re-creating a rumps App and binding uvicorn is exactly the bug.
# ============================================================================
if _is_spawn_worker():
    import multiprocessing
    multiprocessing.freeze_support()
    # ``freeze_support`` runs the worker target and returns; we exit
    # immediately rather than fall through to the parent flow.
    sys.exit(0)


# ============================================================================
# Parent process — the real launcher.
# ============================================================================
import multiprocessing
multiprocessing.freeze_support()  # No-op in the parent; required for safety.

from app.desktop_launcher import main


if __name__ == "__main__":
    sys.exit(main())
