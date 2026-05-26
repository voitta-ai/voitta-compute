"""``ensure_local`` registry + dispatcher.

Plugins register an async resolver for their URI scheme at import time::

    from app.services import ensure_local
    ensure_local.register("vre", resolve)   # async def resolve(ref: Ref) -> Path

``ensure_local(ref_string, loop=...)`` dispatches by scheme, runs the
resolver, and returns the local path as a string.

**Sync/async bridge**: resolvers are always ``async def``. Scripts run
inside ``asyncio.to_thread`` (sandbox.run offloads _execute to a thread
pool), so the event loop is free. We bridge back with
``run_coroutine_threadsafe(resolver(ref), loop).result()``. Callers
outside an asyncio context (tests, CLI) fall through to ``asyncio.run``.

Adding a new plugin scheme:

1.  Write ``async def resolve(ref: refs.Ref) -> Path``
2.  Call ``ensure_local.register("myscheme", resolve)`` at import time
3.  Write ``meta.json`` in the snapshot dir with ``origin.ref = ref.canonical``
    so subsequent runs skip the download.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
from pathlib import Path
from typing import Awaitable, Callable

from app.services import refs

logger = logging.getLogger(__name__)


class EnsureLocalError(RuntimeError):
    pass


ResolverFn = Callable[[refs.Ref], Awaitable[Path]]

_resolvers: dict[str, ResolverFn] = {}
# In-memory dedup: canonical ref string → resolved local path.
# Cleared if the cached path no longer exists on disk.
_cache: dict[str, str] = {}


def register(scheme: str, fn: ResolverFn) -> None:
    """Register the resolver for ``scheme``. Called at plugin import time.
    Re-registration replaces — last writer wins."""
    scheme = scheme.lower()
    if scheme in _resolvers:
        logger.warning(
            "ensure_local resolver for %r already registered; replacing", scheme
        )
    _resolvers[scheme] = fn
    logger.debug("ensure_local: registered resolver for scheme %r", scheme)


def available_schemes() -> set[str]:
    return set(_resolvers)


def ensure_local(
    ref_string: str,
    *,
    loop: asyncio.AbstractEventLoop | None = None,
) -> str:
    """Materialise ``ref_string`` to a local path and return it.

    ``loop`` must be the running event loop when called from a thread
    (i.e. from inside ``asyncio.to_thread``). Pass ``ctx._loop`` from
    ``ScriptContext`` — it is set by ``sandbox.run`` before offloading
    ``_execute`` to the thread pool.
    """
    try:
        ref = refs.parse(ref_string)
    except refs.RefError as exc:
        raise EnsureLocalError(f"invalid ref {ref_string!r}: {exc}") from exc

    # In-memory cache: skip the resolver entirely if we already have it.
    cached = _cache.get(ref.canonical)
    if cached and Path(cached).exists():
        logger.debug("ensure_local: cache hit %r → %r", ref.canonical, cached)
        return cached

    resolver = _resolvers.get(ref.scheme)
    if resolver is None:
        raise EnsureLocalError(
            f"no ensure_local resolver for scheme {ref.scheme!r} — "
            f"registered schemes: {sorted(_resolvers) or '(none)'}"
        )

    coro = resolver(ref)

    if loop is not None and loop.is_running():
        # We are in a thread pool thread; the event loop is running in another
        # thread. Bridge the async resolver back into that loop.
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        try:
            path = future.result(timeout=120)
        except concurrent.futures.TimeoutError:
            raise EnsureLocalError(
                f"resolver for {ref.scheme!r} timed out after 120 s"
            )
    else:
        # No running loop (tests, CLI). Run synchronously.
        path = asyncio.run(coro)

    result = str(path)
    _cache[ref.canonical] = result
    return result


# ---------------------------------------------------------------------------
# Built-in py:// resolver — python_storage snapshots
#
# URI format:  py://<handle>/<filename>
#              py://<handle>          ← first non-meta file in snapshot dir
#
# Examples:
#   py://py_71e1c5b2/frame_0.00s.jpg
#   py://py_71e1c5b2
# ---------------------------------------------------------------------------

async def _resolve_py(ref: refs.Ref) -> Path:
    from app.services import python_storage
    # ref.authority is the handle; ref.path is optional filename
    handle = ref.authority
    filename = (ref.path or "").lstrip("/")

    rec = python_storage.get(handle)
    if rec is None:
        raise EnsureLocalError(f"py:// no snapshot with handle {handle!r}")

    snap_dir = Path(rec["path"])
    if filename:
        p = snap_dir / filename
        if not p.exists():
            raise EnsureLocalError(f"py://{handle}/{filename} not found in snapshot")
        return p

    # No filename — return the first non-meta file
    skip = {"meta.json", "raw.json", "curves.pkl"}
    for f in sorted(snap_dir.iterdir()):
        if f.name not in skip and f.is_file():
            return f
    raise EnsureLocalError(f"py://{handle} snapshot contains no data files")


register("py", _resolve_py)
