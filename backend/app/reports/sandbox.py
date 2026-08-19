"""Controlled-``exec`` harness + smoke-test.

The script's source is ``compile()``d (so syntax errors fail fast and
the traceback points at the right line) and ``exec()``d into a fresh
namespace per run. We deliberately do NOT pass a restricted
``__builtins__`` — restricted-exec sandboxes are notoriously easy to
break out of, and the model is not adversarial here. The sandboxing
that *matters* is filesystem isolation (slug regex + atomic writes)
which lives elsewhere.

Two entry points:

* :func:`smoke_test` — compile + run ``build(ctx)`` against a fresh
  ``ScriptContext``, return ``(ok, result, error)``. Used by
  ``define_script`` / ``edit_script`` to reject bad code *before*
  persisting. No side-effects on disk.
* :func:`run` — same shape but for the live-run path. Caller decides
  what to do with the return value + ctx side-effects.
"""

from __future__ import annotations

import asyncio
import traceback
from dataclasses import dataclass
from typing import Any, Optional  # noqa: F401 — Any used in _make_sheets_client return type

from app.reports.ctx import ScriptContext


@dataclass
class RunResult:
    ok: bool
    result: Any = None
    error: Optional[str] = None
    traceback: Optional[str] = None
    ctx: Optional[ScriptContext] = None


def _build_namespace() -> dict[str, Any]:
    """The execution namespace for ``exec``.

    We expose Python's normal builtins — this is a power-tool for
    advanced users (the LLM), not an untrusted user sandbox.
    """
    # Scripts run in a thread pool (asyncio.to_thread), never the main thread.
    # Force matplotlib to the non-interactive Agg backend before user code
    # can import pyplot, otherwise the macOS GUI backend raises RuntimeError.
    try:
        import matplotlib
        matplotlib.use("agg")
    except Exception:
        pass
    return {"__name__": "voitta_script", "__builtins__": __builtins__}


def _execute(code: str, ctx: ScriptContext) -> RunResult:
    try:
        compiled = compile(code, f"<script:{ctx.slug}>", "exec")
    except SyntaxError as exc:
        return RunResult(
            ok=False,
            error=f"SyntaxError: {exc.msg} at line {exc.lineno}",
            traceback=traceback.format_exc(),
            ctx=ctx,
        )
    ns = _build_namespace()
    try:
        exec(compiled, ns)
    except Exception as exc:
        return RunResult(
            ok=False,
            error=f"{type(exc).__name__}: {exc}",
            traceback=traceback.format_exc(),
            ctx=ctx,
        )

    build = ns.get("build")
    if not callable(build):
        return RunResult(
            ok=False,
            error="script must define a `build(ctx)` function at top level",
            ctx=ctx,
        )
    try:
        result = build(ctx)
    except Exception as exc:
        return RunResult(
            ok=False,
            error=f"{type(exc).__name__}: {exc}",
            traceback=traceback.format_exc(),
            ctx=ctx,
        )
    return RunResult(ok=True, result=result, ctx=ctx)


def _make_sheets_client(
    loop: Optional[asyncio.AbstractEventLoop],
    google_account: Optional[str] = None,
) -> Any:
    """Return a SheetsClient bound to the script's pinned Google account
    (``google_account`` — an email captured when the script was saved),
    or a _NullSheetsClient stub that raises a NAMED error on use.

    Pin semantics are strict on purpose: a pinned account that is
    missing / disconnected / scope-less yields a hard error naming that
    account — never a silent fall-through to the current default (which
    is how data ends up in the wrong Google account). Import is guarded
    so a missing plugin never breaks script execution on non-Sheets pages."""
    try:
        from app.services import google_oauth
        from voitta_sheets.client import SheetsClient, _NullSheetsClient

        if google_account:
            try:
                account_id = google_oauth.resolve_account(google_account)
            except google_oauth.UnknownAccount:
                return _NullSheetsClient(
                    f"ctx.sheets is not available: this script is pinned to "
                    f"Google account {google_account!r}, which is no longer "
                    f"configured. Reconnect it in Settings → Google, or "
                    f"re-pin the script (edit_script with google_account=...)."
                )
            if not google_oauth.is_connected(account_id):
                return _NullSheetsClient(
                    f"ctx.sheets is not available: pinned Google account "
                    f"{google_account!r} is not connected. Reconnect it in "
                    f"Settings → Google."
                )
            if not google_oauth.has_sheets_scope(account_id):
                return _NullSheetsClient(
                    f"ctx.sheets is not available: pinned Google account "
                    f"{google_account!r} lacks the 'spreadsheets' scope. "
                    f"Reconnect it in Settings → Google to grant it."
                )
            return SheetsClient(loop=loop, account=google_account)

        # No pin (legacy script or nothing connected at save time):
        # the settings-default account, resolved at run time.
        if google_oauth.has_sheets_scope():
            return SheetsClient(loop=loop)
        return _NullSheetsClient()
    except Exception:
        try:
            from voitta_sheets.client import NULL_SHEETS_CLIENT
            return NULL_SHEETS_CLIENT
        except Exception:
            return None


def smoke_test(
    slug: str,
    code: str,
    host: Optional[str] = None,
    google_account: Optional[str] = None,
) -> RunResult:
    """Run ``build(ctx)`` once with a throwaway context.

    Used by ``define_script`` / ``edit_script`` to validate code BEFORE
    it lands on disk. Side-effects in ``ctx`` are discarded.

    SYNCHRONOUS — never call this from the event loop; use
    :func:`smoke_test_async`. A wedged uvicorn incident traced back to a
    smoke test running inline in an async tool handler: the candidate code
    did a filesystem-wide ``glob.glob(..., recursive=True)`` and the sync
    frame blocked the loop for good (no asyncio timeout can preempt it).
    """
    ctx = ScriptContext(slug=slug, host=host)
    ctx.sheets = _make_sheets_client(None, google_account)
    return _execute(code, ctx)


_SMOKE_TIMEOUT_S = 60  # max wall time for a define/edit validation run


async def smoke_test_async(
    slug: str,
    code: str,
    host: Optional[str] = None,
    google_account: Optional[str] = None,
) -> RunResult:
    """Thread-offloaded, time-limited smoke test — safe on the event loop.

    On timeout the worker thread keeps running (Python threads can't be
    killed) but the loop stays responsive and the caller gets a clean
    failure instead of a frozen app.
    """
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(smoke_test, slug, code, host, google_account),
            timeout=_SMOKE_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        return RunResult(
            ok=False,
            error=(
                f"smoke test timed out after {_SMOKE_TIMEOUT_S}s — build(ctx) "
                "must finish quickly at definition time. Avoid unbounded "
                "loops, filesystem-wide scans (e.g. recursive glob from /), "
                "and slow network calls without timeouts."
            ),
        )


_SCRIPT_TIMEOUT_S = 120  # max wall time for a single build(ctx) run


async def run(
    slug: str,
    code: str,
    args: Optional[dict[str, Any]] = None,
    host: Optional[str] = None,
    google_account: Optional[str] = None,
) -> RunResult:
    """Live run. Offloads script execution to a thread pool so the event
    loop stays responsive and ``ctx.ensure_local()`` can bridge async
    resolvers back via ``run_coroutine_threadsafe``.

    ``google_account`` is the script's pinned account email (from its
    meta) — it makes re-runs reproducible regardless of the current
    settings default."""
    try:
        loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    ctx = ScriptContext(slug=slug, args=dict(args or {}), host=host, _loop=loop)
    ctx.sheets = _make_sheets_client(loop, google_account)
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_execute, code, ctx),
            timeout=_SCRIPT_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        return RunResult(
            ok=False,
            error=f"Script timed out after {_SCRIPT_TIMEOUT_S}s — "
                  "it may be making too many API calls or waiting on a slow network.",
            ctx=ctx,
        )
