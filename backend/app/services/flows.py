"""Flow reports — persisted Python that compiles into a flow-chart JSON.

Lifecycle::

    define_flow(name, code) ─► scripts/flows/<slug>/code.py + meta.json
                              (last-write-wins, symmetric with define_report)

    build_flow_definition(slug)
        ├── reads code.py
        ├── execs with FlowBuilder + FlowCtx in scope
        ├── calls top-level build(ctx)
        ├── coerces FlowBuilder → dict (or accepts a dict directly)
        └── returns the definition payload (or raises ScriptError)

The frontend never sees the .py — only the JSON definition shipped via
the ``show_flow_report`` browser primitive. Editing goes through
``scripts.edit_script("flow", ...)`` for symmetry with report scripts.

FlowCtx is intentionally small: only what a flow author actually
needs. ``get_theme()`` for palette-aware group colouring, ``log()`` for
debugging. No image/text sinks (flows produce a structural definition,
not rich-output items), no add_js/add_css (rendering is on the
frontend, the chat shadow DOM owns the theme).
"""

from __future__ import annotations

import time
import traceback
from typing import Any

from app.services.flow_builder import FlowBuilder, FlowBuilderError
from app.services.scripts import (
    ScriptError,
    _code_path,
    _list_kind,
    _meta_path,
    _persist,
    _read_meta,
    _update_run_meta,
    slugify,
)


SMOKE_ERROR_MAX_BYTES = 1500


# ---- FlowCtx --------------------------------------------------------------


class FlowCtx:
    """The object passed to ``build(ctx)`` in a flow script.

    Deliberately small: flow scripts produce a *structural* definition
    (JSON), not a live Panel layout, so most of ``ScriptContext`` is
    irrelevant here.

    Provides:
      • ``ctx.get_theme(host=…)`` — palette resolver (same shape as the
        ``get_active_theme`` tool) so the script can pick group colours
        from the active palette instead of baking in hex codes.
      • ``ctx.log(*args)`` — append a debug line; surfaced in the tool
        result under ``log_lines`` (capped at 100 entries × 1 KB).
    """

    MAX_LOG_LINES = 100
    MAX_LOG_LINE_BYTES = 1000

    def __init__(self, *, slug: str) -> None:
        self.slug = slug
        self._log_lines: list[str] = []

    def get_theme(self, host: str | None = None) -> dict[str, Any]:
        from app.tools.domain import theme as _theme
        return _theme.resolve_theme(host)

    def log(self, *args: Any) -> None:
        if len(self._log_lines) >= self.MAX_LOG_LINES:
            return
        line = " ".join(str(a) for a in args)
        if len(line.encode("utf-8")) > self.MAX_LOG_LINE_BYTES:
            line = line.encode("utf-8")[: self.MAX_LOG_LINE_BYTES].decode(
                "utf-8", errors="replace"
            )
        self._log_lines.append(line)


# ---- exec helpers ---------------------------------------------------------


def _coerce_definition(value: Any) -> dict:
    """Accept either a ``FlowBuilder`` (call ``.to_dict()``) or a
    pre-built ``{"process": {...}}`` dict. Anything else is rejected
    with a ``ScriptError`` describing what the script returned.
    """
    if isinstance(value, FlowBuilder):
        return value.to_dict()
    if isinstance(value, dict):
        if not isinstance(value.get("process"), dict):
            raise ScriptError(
                "build(ctx) returned a dict without a top-level 'process' "
                "key. Return a FlowBuilder, or call FlowBuilder.to_dict()."
            )
        return value
    raise ScriptError(
        f"build(ctx) must return a FlowBuilder or its .to_dict() result, "
        f"got {type(value).__name__}."
    )


def _run_flow_blocking(code: str, ctx: FlowCtx) -> dict:
    """Exec the script with FlowBuilder injected, call build(ctx).

    Returns the (validated) definition dict, or raises ``ScriptError``
    with a truncated traceback.
    """
    ns: dict[str, Any] = {
        "__name__": "__voitta_flow__",
        "FlowBuilder": FlowBuilder,
    }
    try:
        exec(compile(code, "<flow>", "exec"), ns)  # noqa: S102
    except Exception:
        raise ScriptError(traceback.format_exc()[-2000:])

    fn = ns.get("build")
    if not callable(fn):
        raise ScriptError(
            "flow script must define a top-level `build(ctx)` function "
            "that returns a FlowBuilder (or its .to_dict() result)."
        )

    try:
        result = fn(ctx)
    except FlowBuilderError as exc:
        raise ScriptError(f"FlowBuilderError: {exc}")
    except ScriptError:
        raise
    except Exception:
        raise ScriptError(traceback.format_exc()[-2000:])

    try:
        return _coerce_definition(result)
    except FlowBuilderError as exc:
        raise ScriptError(f"FlowBuilderError: {exc}")


# ---- public API -----------------------------------------------------------


def define_flow(name: str, code: str) -> dict:
    """Persist a flow script. Returns ``{name, code_path, meta}``.

    Same shape as ``define_report``: write code.py + meta.json under
    ``scripts/flows/<slug>/``, last-write-wins on slug collision. Code
    is parse-checked but not executed here — call ``smoke_test_flow``
    for that.
    """
    slug = slugify(name)
    try:
        compile(code, "<flow-validation>", "exec")
    except SyntaxError as exc:
        raise ValueError(f"syntax error: {exc.msg} at line {exc.lineno}")
    code_path, meta = _persist("flow", slug, code)
    return {"name": slug, "code_path": str(code_path), "meta": meta}


def list_flows() -> list[dict]:
    return _list_kind("flow")


def get_flow(name: str) -> dict | None:
    slug = slugify(name)
    code_path = _code_path("flow", slug)
    if not code_path.exists():
        return None
    return {
        "name": slug,
        "code": code_path.read_text(),
        "meta": _read_meta(_meta_path("flow", slug)),
    }


def delete_flow(name: str) -> bool:
    import shutil
    from app.services.scripts import _script_dir

    slug = slugify(name)
    d = _script_dir("flow", slug)
    if not d.is_dir():
        return False
    shutil.rmtree(d)
    return True


def build_flow_definition(name: str) -> tuple[dict, list[str]]:
    """Load the stored flow script and run it to produce the JSON
    definition.

    Returns ``(definition_dict, log_lines)``. Raises ``ScriptError`` on
    a broken script. Updates last-run meta either way (this counts as a
    real "run").
    """
    slug = slugify(name)
    code_path = _code_path("flow", slug)
    if not code_path.exists():
        raise ScriptError(f"no flow script named {name!r}")

    code = code_path.read_text()
    ctx = FlowCtx(slug=slug)
    started = time.time()
    ok = True
    error: str | None = None
    definition: dict = {}
    try:
        definition = _run_flow_blocking(code, ctx)
    except ScriptError as exc:
        ok = False
        error = str(exc)
        raise
    finally:
        elapsed_s = round(time.time() - started, 2)
        # No run_id concept for flows — synth one so meta stays consistent
        # with reports.
        import secrets as _s
        run_id = _s.token_hex(4)
        _update_run_meta(
            "flow", slug, ok=ok, run_id=run_id, elapsed_s=elapsed_s, error=error
        )

    return definition, ctx._log_lines


def smoke_test_flow(name: str) -> str | None:
    """Run the flow's ``build(ctx)`` once and return any error.

    Returns ``None`` on success, otherwise a truncated error message
    suitable for surfacing back to the LLM. Used by ``define_flow`` /
    ``edit_flow`` tool handlers to fail-fast at define-time instead of
    at show-time.
    """
    try:
        build_flow_definition(name)
        return None
    except ScriptError as exc:
        msg = str(exc)
        if len(msg) > SMOKE_ERROR_MAX_BYTES:
            msg = "…[truncated]…\n" + msg[-SMOKE_ERROR_MAX_BYTES:]
        return msg
    except Exception as exc:  # pragma: no cover — defensive
        return f"{type(exc).__name__}: {str(exc)[:SMOKE_ERROR_MAX_BYTES]}"
