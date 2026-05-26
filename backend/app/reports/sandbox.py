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
from typing import Any, Optional

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


def smoke_test(slug: str, code: str, host: Optional[str] = None) -> RunResult:
    """Run ``build(ctx)`` once with a throwaway context.

    Used by ``define_script`` / ``edit_script`` to validate code BEFORE
    it lands on disk. Side-effects in ``ctx`` are discarded.
    """
    return _execute(code, ScriptContext(slug=slug, host=host))


async def run(
    slug: str,
    code: str,
    args: Optional[dict[str, Any]] = None,
    host: Optional[str] = None,
) -> RunResult:
    """Live run. Offloads script execution to a thread pool so the event
    loop stays responsive and ``ctx.ensure_local()`` can bridge async
    resolvers back via ``run_coroutine_threadsafe``."""
    try:
        loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    ctx = ScriptContext(slug=slug, args=dict(args or {}), host=host, _loop=loop)
    return await asyncio.to_thread(_execute, code, ctx)
