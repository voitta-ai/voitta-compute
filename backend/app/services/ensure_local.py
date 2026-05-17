"""``ctx.ensure_local`` — resolve an upstream-artefact ref to a local path.

Reports and compute scripts persist source code that runs months later.
Hard-coding a ``py_<handle>`` snapshot id in the script means it breaks
the moment the snapshot is GC'd or the user deletes it. The contract
documented in ``VOITTA_SYSTEM_PROMPT`` (§ REPORTS — REFERENCE UPSTREAM
ARTEFACTS) is: write canonical refs, let the runtime materialise local
copies on demand.

This module is that runtime. ``ensure_local(ref)``:

  1. Parses ``ref`` via :mod:`app.services.refs`.
  2. Walks ``python_storage/cache/snapshot_*/meta.json`` looking for an
     ``origin.ref`` that matches the canonical form. Hit → return its
     local path. (This is the "share" semantics: two reports requesting
     the same upstream artefact share its cache entry.)
  3. Miss → dispatch to the scheme's resolver, which fetches the
     artefact, writes a snapshot, and stamps ``meta.json::origin.ref``
     with the canonical so step 2 wins next time.

Resolvers run async — VRE goes over MCP, Drive uses httpx. From sync
script code we hand them to ``run_coroutine_threadsafe`` against the
backend's main loop. The script's executor thread blocks on ``.result()``
until the resolver returns. Crashes / non-existent connectors / signed
URL failures surface as :class:`EnsureLocalError`.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Awaitable, Callable

from app.services import python_storage, refs

_logger = logging.getLogger(__name__)


class EnsureLocalError(RuntimeError):
    """Raised when a ref can't be resolved.

    The script's ``try/except`` around ``ctx.ensure_local`` can catch
    this and decide whether to degrade gracefully or propagate.
    """


# Resolver registry. Keyed by scheme. Each resolver is an async
# function ``async def resolve(parsed_ref) -> Path`` that performs the
# fetch and returns the local path of the freshly-cached snapshot.
ResolverFn = Callable[[refs.Ref], Awaitable[Path]]
_RESOLVERS: dict[str, ResolverFn] = {}


def register(scheme: str, fn: ResolverFn) -> None:
    """Register a resolver for ``scheme``. Idempotent on re-register —
    the latest binding wins, which is useful in test fixtures."""
    _RESOLVERS[scheme.lower()] = fn


def _find_cached(canonical: str) -> Path | None:
    """Walk the snapshot cache and return the cached path for ``canonical``.

    Single-file snapshots return the file path (so the script can
    ``Path(p).read_bytes()`` immediately); multi-file snapshots
    return the directory (so the script can enumerate variants).
    The choice is driven by ``meta.stored_name`` — single-file
    resolvers set it, multi-file resolvers leave it None. Crucially,
    this must match the shape the resolver returns on a *fresh* fetch;
    otherwise scripts see ``Is a directory: …`` errors the second
    time they reference the same ref.

    Slow path is linear in number of snapshots, but every miss already
    triggers a network fetch — a few ``stat`` + ``read_text`` calls
    don't move the needle.
    """
    root = python_storage.STORAGE_ROOT
    if not root.exists():
        return None
    for snap_dir in root.iterdir():
        if not snap_dir.is_dir():
            continue
        meta_path = snap_dir / "meta.json"
        if not meta_path.is_file():
            continue
        try:
            meta = json.loads(meta_path.read_text())
        except Exception:
            continue
        origin = meta.get("origin") or {}
        if not (isinstance(origin, dict) and origin.get("ref") == canonical):
            continue
        stored = meta.get("stored_name")
        if isinstance(stored, str) and stored:
            candidate = snap_dir / stored
            if candidate.is_file():
                return candidate
            # Fall through: meta says single-file but the file is gone.
            # Returning the dir is safer than returning a non-existent
            # path; scripts that wrap ensure_local in try/except will
            # see a clear error. Bare dir return matches multi-file
            # semantics.
        return snap_dir
    return None


def _resolve_sync(ref_str: str) -> Path:
    """Synchronous entry point used by ``ctx.ensure_local`` (which is
    itself called from sync script code).

    Cache lookup is sync. On miss we hand off to the registered async
    resolver via ``run_coroutine_threadsafe`` against whichever event
    loop happens to be running the backend — the same loop that
    scheduled the script's executor thread.
    """
    try:
        parsed = refs.parse(ref_str)
    except refs.RefError as exc:
        raise EnsureLocalError(f"invalid ref: {exc}") from exc

    # Cache lookup first.
    cached = _find_cached(parsed.canonical)
    if cached is not None:
        _logger.info("ensure_local cache hit: %s -> %s", parsed.canonical, cached)
        return cached

    resolver = _RESOLVERS.get(parsed.scheme)
    if resolver is None:
        raise EnsureLocalError(
            f"no resolver registered for scheme {parsed.scheme!r} "
            f"(ref={parsed.canonical!r})"
        )

    # Dispatch onto the dedicated resolver loop. We do NOT use the
    # main FastAPI loop: report_script_layout (Panel session handler)
    # runs synchronously on the main loop, then calls build(ctx) →
    # ctx.ensure_local() → us. Scheduling back to the main loop with
    # run_coroutine_threadsafe and blocking on .result() would
    # deadlock the loop with itself. A private loop on its own
    # thread side-steps the whole class of problem and works
    # identically from the script-executor thread (compute scripts)
    # and from the main loop (Panel report handlers).
    loop = _resolver_loop()
    fut = asyncio.run_coroutine_threadsafe(resolver(parsed), loop)
    try:
        path = fut.result()
    except Exception as exc:
        raise EnsureLocalError(
            f"resolver for {parsed.scheme!r} failed: {exc}"
        ) from exc
    _logger.info("ensure_local cache miss → fetched: %s -> %s", parsed.canonical, path)
    return path


# ─── Dedicated resolver loop ───────────────────────────────────────────────
#
# One background thread, one private asyncio loop. Started lazily on
# first use; lives for the process lifetime (no shutdown hook — the
# OS reclaims it when uvicorn exits). The loop is dedicated to
# ensure_local's resolver coroutines so the main FastAPI loop is
# never blocked or recursed into.

import threading

_resolver_loop_lock = threading.Lock()
_resolver_loop_obj: asyncio.AbstractEventLoop | None = None


def _resolver_loop() -> asyncio.AbstractEventLoop:
    global _resolver_loop_obj
    if _resolver_loop_obj is not None:
        return _resolver_loop_obj
    with _resolver_loop_lock:
        if _resolver_loop_obj is not None:
            return _resolver_loop_obj
        loop = asyncio.new_event_loop()
        # Daemon=True so the process can exit cleanly without joining.
        # Name shows up in py-spy / debuggers if someone goes looking.
        t = threading.Thread(
            target=loop.run_forever,
            name="ensure_local-resolver",
            daemon=True,
        )
        t.start()
        _resolver_loop_obj = loop
        _logger.info("ensure_local: resolver loop started on background thread")
        return loop


def bind_loop(_loop: asyncio.AbstractEventLoop) -> None:
    """Compatibility shim — kept so main.py's startup hook keeps
    importing cleanly during the in-place refactor. The resolver loop
    is now self-managing (lazy-started on first ensure_local call),
    so the bound loop is just ignored. The shim stays as a tombstone
    until the next API-sweep commit."""
    _logger.debug(
        "ensure_local.bind_loop called; resolver now uses its own private loop"
    )


def ensure_local(ref: str) -> str:
    """Public entry point used by ``ScriptCtx.ensure_local``.

    Returns the local *file or directory* path (as a string) that
    materialises ``ref``. Raises :class:`EnsureLocalError` on any
    failure — script authors can wrap the call in ``try/except`` to
    degrade gracefully if a single asset is missing.
    """
    return str(_resolve_sync(ref))
