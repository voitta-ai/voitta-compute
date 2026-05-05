"""User-defined Python scripts — compute scripts and report scripts.

Two flavours, one host:

  • **Compute scripts** define ``run(ctx, args=None)`` and return a small
    JSON-serialisable value. Side-effects via ``ctx.text(md) /
    ctx.image(fig|bytes) / ctx.log(s)`` flow back to the chat as inline
    rich blocks.

  • **Report scripts** define ``build(ctx)`` and return a Panel layout.
    The Panel-served route ``/panel/reports?id=<slug>`` (mounted via
    ``panel.io.fastapi.add_applications``) calls ``build(ctx)`` once per
    browser session and serves the layout live, so EditableTemplate's
    drag/resize commits round-trip to Python.

Persistence layout — one folder per script, with run output co-located::

    scripts/
    ├── compute/
    │   └── <slug>/
    │       ├── code.py
    │       ├── meta.json
    │       └── runs/<run_id>/img_*.png
    └── reports/
        └── <slug>/
            ├── code.py
            └── meta.json

Image outputs from compute scripts land under
``scripts/compute/<slug>/runs/<run_id>/img_N.png`` and are served by
the ``/api/script-output/<slug>/<run_id>/<file>`` route.

Trust model — same as ``buffer_eval``:

  • In-process execution; full venv import surface; no sandbox.
  • Per-script timeout enforced via ``asyncio.wait_for`` over a
    ThreadPoolExecutor. **Note**: the executor thread keeps running
    after timeout (Python doesn't expose a kill primitive); the
    coroutine returns and the orchestrator continues. A truly
    pathological infinite loop would hold a thread until the dev
    restarts the backend — acceptable for v1.
"""

from __future__ import annotations

import asyncio
import json
import re
import secrets
import shutil
import time
import traceback
from pathlib import Path
from typing import Any


# Project layout — same conventions as services/python_storage.py.
PROJECT_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_ROOT = PROJECT_ROOT / "scripts"
SCRIPTS_COMPUTE = SCRIPTS_ROOT / "compute"
SCRIPTS_REPORTS = SCRIPTS_ROOT / "reports"

# Timeouts. Both are async wait_for'd; the underlying thread isn't killed.
COMPUTE_TIMEOUT_S = 60.0
COMPUTE_TIMEOUT_MAX_S = 300.0
REPORT_TIMEOUT_S = 120.0
REPORT_TIMEOUT_MAX_S = 300.0

# Per-run output caps so a runaway script can't fill memory.
MAX_LOG_LINES = 200
MAX_LOG_LINE_BYTES = 1000
MAX_TEXT_BLOCKS = 100
MAX_IMAGES = 20

_SLUG_OK = re.compile(r"^[a-z0-9_-]+$")


# ---- helpers --------------------------------------------------------------


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _ensure_dirs() -> None:
    SCRIPTS_COMPUTE.mkdir(parents=True, exist_ok=True)
    SCRIPTS_REPORTS.mkdir(parents=True, exist_ok=True)


def slugify(name: str) -> str:
    """Reduce ``name`` to a filesystem-safe slug.

    Rules: lowercase, ``[a-z0-9_-]`` only, runs of separators collapsed,
    leading/trailing ``_-`` stripped, max 64 chars. Empty results
    rejected with ``ValueError``.
    """

    if not isinstance(name, str):
        raise ValueError("name must be a string")
    s = re.sub(r"[^a-z0-9_-]+", "_", name.lower())
    s = re.sub(r"_+", "_", s).strip("_-")
    if not s:
        raise ValueError("name slugifies to empty")
    return s[:64]


def _script_dir(kind: str, slug: str) -> Path:
    base = SCRIPTS_COMPUTE if kind == "compute" else SCRIPTS_REPORTS
    return base / slug


def _meta_path(kind: str, slug: str) -> Path:
    return _script_dir(kind, slug) / "meta.json"


def _code_path(kind: str, slug: str) -> Path:
    return _script_dir(kind, slug) / "code.py"


def _runs_dir(kind: str, slug: str) -> Path:
    return _script_dir(kind, slug) / "runs"


def _read_meta(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _write_meta(path: Path, meta: dict) -> None:
    path.write_text(json.dumps(meta, indent=2, ensure_ascii=False))


def _persist(kind: str, slug: str, code: str) -> tuple[Path, dict]:
    """Write the script + sidecar meta. Last-write-wins (overwrites). Returns
    ``(code_path, meta_dict)``."""

    _ensure_dirs()
    _script_dir(kind, slug).mkdir(parents=True, exist_ok=True)
    code_path = _code_path(kind, slug)
    meta_path = _meta_path(kind, slug)

    meta = _read_meta(meta_path)
    new_meta = {
        "name": slug,
        "kind": kind,
        "created_at": meta.get("created_at") or _now_iso(),
        "updated_at": _now_iso(),
        "code_bytes": len(code.encode("utf-8")),
        # Run history fields are populated by run_compute / report_handler.
        "last_run_at": meta.get("last_run_at"),
        "last_run_ok": meta.get("last_run_ok"),
        "last_run_id": meta.get("last_run_id"),
        "last_run_elapsed_s": meta.get("last_run_elapsed_s"),
    }
    code_path.write_text(code)
    _write_meta(meta_path, new_meta)
    return code_path, new_meta


def _update_run_meta(kind: str, slug: str, *, ok: bool, run_id: str, elapsed_s: float, error: str | None) -> None:
    path = _meta_path(kind, slug)
    meta = _read_meta(path)
    meta["last_run_at"] = _now_iso()
    meta["last_run_ok"] = ok
    meta["last_run_id"] = run_id
    meta["last_run_elapsed_s"] = elapsed_s
    if error:
        meta["last_run_error"] = error[:500]
    else:
        meta.pop("last_run_error", None)
    _write_meta(path, meta)


# ---- ScriptContext --------------------------------------------------------


class ScriptError(RuntimeError):
    """Raised inside ``_run_*_blocking`` when the script itself raises;
    carries the truncated traceback as its message."""


class ScriptContext:
    """The single object scripts use to interact with the host.

    Methods:

      • ``snapshot(handle)``  — return the python_storage record
      • ``dataframe(handle)`` — return ``pd.read_pickle(curves.pkl)``
      • ``raw(handle)``       — return the parsed ``raw.json``
      • ``text(markdown)``    — emit an inline text/markdown block
      • ``image(fig_or_bytes, alt?)`` — emit an inline image; saves to
                                         disk and returns the URL path
      • ``log(*args)``        — append a debug log line
    """

    def __init__(self, run_id: str, *, kind: str, slug: str) -> None:
        self.run_id = run_id
        self.kind = kind
        self.slug = slug
        self.output_dir = _runs_dir(kind, slug) / run_id
        self._image_count = 0
        self._items: list[dict] = []
        self._log_lines: list[str] = []

    # ---- data access -----------------------------------------------------

    def snapshot(self, handle: str) -> dict:
        from app.services import python_storage

        rec = python_storage.get(handle)
        if rec is None:
            raise KeyError(f"no python_storage snapshot {handle!r}")
        return rec

    def dataframe(self, handle: str):
        import pandas as pd

        rec = self.snapshot(handle)
        pkl = Path(rec["path"]) / "curves.pkl"
        if not pkl.exists():
            raise FileNotFoundError(
                f"snapshot {handle!r} has no curves.pkl (kind not 'curves'?)"
            )
        return pd.read_pickle(pkl)

    def raw(self, handle: str) -> Any:
        rec = self.snapshot(handle)
        path = Path(rec["path"]) / "raw.json"
        if not path.exists():
            raise FileNotFoundError(f"snapshot {handle!r} has no raw.json")
        return json.loads(path.read_text())

    # ---- output ---------------------------------------------------------

    def text(self, markdown: str) -> None:
        if len([i for i in self._items if i.get("kind") == "text"]) >= MAX_TEXT_BLOCKS:
            return
        self._items.append({"kind": "text", "markdown": str(markdown)})

    def image(self, fig_or_bytes: Any, alt: str | None = None) -> str:
        """Save an image to ``output_dir`` and emit an inline image block.

        Accepts a matplotlib ``Figure``, a PIL ``Image``, or raw PNG/JPEG
        bytes. Returns the public URL path (``/api/script-output/...``).
        """

        if self._image_count >= MAX_IMAGES:
            raise RuntimeError(
                f"ctx.image() called more than {MAX_IMAGES} times in one run"
            )
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._image_count += 1
        name = f"img_{self._image_count}.png"
        out_path = self.output_dir / name

        if hasattr(fig_or_bytes, "savefig"):  # matplotlib Figure
            fig_or_bytes.savefig(out_path, format="png", bbox_inches="tight")
        elif hasattr(fig_or_bytes, "save"):  # PIL.Image
            fig_or_bytes.save(out_path, format="PNG")
        elif isinstance(fig_or_bytes, (bytes, bytearray)):
            out_path.write_bytes(bytes(fig_or_bytes))
        else:
            raise TypeError(
                "ctx.image() expected matplotlib.Figure / PIL.Image / bytes; "
                f"got {type(fig_or_bytes).__name__}"
            )
        url = f"/api/script-output/{self.slug}/{self.run_id}/{name}"
        self._items.append({"kind": "image", "url": url, "alt": alt})
        return url

    def log(self, *args: Any) -> None:
        if len(self._log_lines) >= MAX_LOG_LINES:
            return
        msg = " ".join(self._stringify(a) for a in args)[:MAX_LOG_LINE_BYTES]
        self._log_lines.append(msg)

    @staticmethod
    def _stringify(a: Any) -> str:
        if isinstance(a, str):
            return a
        try:
            return json.dumps(a, default=str, ensure_ascii=False)
        except Exception:
            return str(a)


# ---- execution ------------------------------------------------------------


def _exec_script(code: str, source_label: str) -> dict:
    """Compile + exec the script body in a fresh namespace. The script
    typically defines top-level functions (``run`` for compute,
    ``build`` for reports). Raises ``ScriptError`` with a truncated
    traceback if the script body itself raises."""

    ns: dict[str, Any] = {"__name__": "__voitta_script__"}
    try:
        exec(compile(code, source_label, "exec"), ns)  # noqa: S102
    except Exception:
        raise ScriptError(traceback.format_exc()[-2000:])
    return ns


def _run_compute_blocking(code: str, ctx: ScriptContext, args: Any) -> Any:
    """Run a compute script in the calling thread. Returns the script's
    return value (or None). Raises ``ScriptError`` on script exceptions."""

    ns = _exec_script(code, "<compute>")
    fn = ns.get("run")
    if not callable(fn):
        raise ScriptError("compute script must define a top-level `run(ctx, args=None)` function")
    try:
        # Try the (ctx, args) form first; fall back to (ctx,) for
        # scripts that don't accept args.
        try:
            return fn(ctx, args)
        except TypeError:
            if args is None:
                return fn(ctx)
            raise
    except ScriptError:
        raise
    except Exception:
        raise ScriptError(traceback.format_exc()[-2000:])


def _run_report_blocking(code: str, ctx: ScriptContext) -> Any:
    """Run a report script. Returns its Panel layout (or whatever
    ``build(ctx)`` returns). Raises ``ScriptError`` on script
    exceptions."""

    ns = _exec_script(code, "<report>")
    fn = ns.get("build")
    if not callable(fn):
        raise ScriptError("report script must define a top-level `build(ctx)` function")
    try:
        return fn(ctx)
    except ScriptError:
        raise
    except Exception:
        raise ScriptError(traceback.format_exc()[-2000:])


# ---- public: compute scripts ----------------------------------------------


async def run_compute(name: str, code: str, args: Any = None, *, timeout_s: float | None = None) -> dict:
    """Persist + execute a compute script. Returns:

        {
          ok, name, run_id, result, error?, items, log_lines, elapsed_s
        }
    """

    slug = slugify(name)
    code_path, _ = _persist("compute", slug, code)
    run_id = secrets.token_hex(4)
    ctx = ScriptContext(run_id, kind="compute", slug=slug)

    timeout = max(1.0, min(timeout_s or COMPUTE_TIMEOUT_S, COMPUTE_TIMEOUT_MAX_S))
    started = time.time()

    ok = True
    error: str | None = None
    result: Any = None
    loop = asyncio.get_running_loop()
    try:
        result = await asyncio.wait_for(
            loop.run_in_executor(None, _run_compute_blocking, code, ctx, args),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        ok = False
        error = f"timeout after {timeout:.1f}s"
    except ScriptError as exc:
        ok = False
        error = str(exc)
    except Exception as exc:  # pragma: no cover — host-level failure
        ok = False
        error = f"{type(exc).__name__}: {exc}"

    elapsed_s = round(time.time() - started, 2)
    _update_run_meta(
        "compute", slug, ok=ok, run_id=run_id, elapsed_s=elapsed_s, error=error
    )

    return {
        "ok": ok,
        "name": slug,
        "run_id": run_id,
        "result": result,
        "error": error,
        "items": ctx._items,  # surfaced as rich blocks by the chat handler
        "log_lines": ctx._log_lines,
        "elapsed_s": elapsed_s,
        "code_path": str(code_path),
    }


def _list_kind(kind: str) -> list[dict]:
    base = SCRIPTS_COMPUTE if kind == "compute" else SCRIPTS_REPORTS
    _ensure_dirs()
    out: list[dict] = []
    for d in sorted(base.iterdir()):
        if not d.is_dir():
            continue
        if not (d / "code.py").exists():
            continue
        meta = _read_meta(d / "meta.json")
        out.append({"name": d.name, **meta})
    return out


def list_compute() -> list[dict]:
    return _list_kind("compute")


def get_compute(name: str) -> dict | None:
    slug = slugify(name)
    code_path = _code_path("compute", slug)
    if not code_path.exists():
        return None
    return {
        "name": slug,
        "code": code_path.read_text(),
        "meta": _read_meta(_meta_path("compute", slug)),
    }


def delete_compute(name: str) -> bool:
    slug = slugify(name)
    d = _script_dir("compute", slug)
    if not d.is_dir():
        return False
    shutil.rmtree(d)
    return True


# ---- public: report scripts -----------------------------------------------


def define_report(name: str, code: str) -> dict:
    """Persist a report script. Returns ``{name, code_path, meta}``. The
    rendering happens at GET-time on /api/reports/{name}."""

    slug = slugify(name)
    # Reject bodies that don't define `build` early (cheap parse-only check).
    try:
        compile(code, "<report-validation>", "exec")
    except SyntaxError as exc:
        raise ValueError(f"syntax error: {exc.msg} at line {exc.lineno}")
    code_path, meta = _persist("reports", slug, code)
    return {"name": slug, "code_path": str(code_path), "meta": meta}


def list_reports() -> list[dict]:
    return _list_kind("reports")


def get_report_script(name: str) -> dict | None:
    slug = slugify(name)
    code_path = _code_path("reports", slug)
    if not code_path.exists():
        return None
    return {
        "name": slug,
        "code": code_path.read_text(),
        "meta": _read_meta(_meta_path("reports", slug)),
    }


def delete_report(name: str) -> bool:
    slug = slugify(name)
    d = _script_dir("reports", slug)
    if not d.is_dir():
        return False
    shutil.rmtree(d)
    return True


# ---- public: incremental edits --------------------------------------------


def edit_script(kind: str, name: str, edits: list[dict]) -> dict:
    """Apply a sequence of search-replace edits to a stored script.

    Each edit is ``{"find": str, "replace": str, "replace_all": bool}``
    (default ``replace_all=False``). Same semantics as Claude Code's
    Edit tool:

      • ``find`` must occur in the current code (after preceding edits).
      • If ``replace_all`` is False, ``find`` must occur exactly once;
        a multi-match is rejected so the model surfaces a non-unique
        anchor early instead of silently editing the wrong site.
      • Edits apply in order; later edits see earlier edits' results.

    Atomicity: if ANY edit fails (not found / non-unique / final syntax
    error) we don't write anything — the script on disk stays as it was.
    The model retries with better anchors instead of debugging a
    half-applied state.

    Returns ``{name, code_path, applied: [{find, replace, count}]}`` on
    success, raises ``ValueError`` with a human-readable message on
    failure.
    """

    if kind not in ("compute", "reports"):
        raise ValueError(f"invalid kind {kind!r}")
    if not edits:
        raise ValueError("edits list is empty")

    slug = slugify(name)
    code_path = _code_path(kind, slug)
    if not code_path.exists():
        raise ValueError(f"no {kind} script named {name!r}")

    code = code_path.read_text()
    applied: list[dict] = []
    for i, edit in enumerate(edits):
        if not isinstance(edit, dict):
            raise ValueError(f"edit #{i + 1}: expected object, got {type(edit).__name__}")
        find = edit.get("find")
        replace = edit.get("replace")
        replace_all = bool(edit.get("replace_all", False))
        if not isinstance(find, str) or find == "":
            raise ValueError(f"edit #{i + 1}: 'find' must be a non-empty string")
        if not isinstance(replace, str):
            raise ValueError(f"edit #{i + 1}: 'replace' must be a string")
        count = code.count(find)
        if count == 0:
            raise ValueError(f"edit #{i + 1}: 'find' string not present in script")
        if count > 1 and not replace_all:
            raise ValueError(
                f"edit #{i + 1}: 'find' matches {count} sites; "
                "set replace_all=true OR add more surrounding context to make it unique"
            )
        code = code.replace(find, replace) if replace_all else code.replace(find, replace, 1)
        applied.append({"find": find, "replace": replace, "count": count if replace_all else 1})

    # Reject syntactically-broken results before writing — same guard as
    # define_report. Compute scripts go through the same check for
    # symmetry; the cost is negligible.
    try:
        compile(code, f"<{kind}-edit-validation>", "exec")
    except SyntaxError as exc:
        raise ValueError(
            f"edits would leave script with a syntax error: "
            f"{exc.msg} at line {exc.lineno} (no changes written)"
        )

    code_path, _ = _persist(kind, slug, code)
    return {"name": slug, "code_path": str(code_path), "applied": applied}


def report_script_layout(report_id: str) -> Any | None:
    """Run a stored report script and return its Panel layout.

    Returns ``None`` if no script with slugified ``report_id`` exists —
    the caller (typically ``app.services.panel_app.panel_factory``)
    falls back to the mock layout in that case.

    Synchronous on purpose: it's called inside a Bokeh session document,
    where async wait isn't useful and ``run_in_executor`` would just add
    a layer. Errors raise ``ScriptError`` so the factory can render an
    error layout instead of leaking a 500.
    """

    try:
        slug = slugify(report_id)
    except ValueError:
        return None
    code_path = _code_path("reports", slug)
    if not code_path.exists():
        return None
    code = code_path.read_text()
    run_id = secrets.token_hex(4)
    ctx = ScriptContext(run_id, kind="reports", slug=slug)
    started = time.time()
    ok = True
    error: str | None = None
    layout: Any = None
    try:
        layout = _run_report_blocking(code, ctx)
    except ScriptError as exc:
        ok = False
        error = str(exc)
        raise
    except Exception as exc:  # pragma: no cover
        ok = False
        error = f"{type(exc).__name__}: {exc}"
        raise ScriptError(error)
    finally:
        elapsed_s = round(time.time() - started, 2)
        _update_run_meta(
            "reports", slug, ok=ok, run_id=run_id, elapsed_s=elapsed_s, error=error
        )
    return layout


# Cap the smoke-test error message so the LLM doesn't burn its context
# window on a 50-frame Bokeh/Panel traceback. Tracebacks already get
# truncated to 2000 bytes by ``_run_report_blocking``; we tighten that
# further for the tool-result path. The tail (where the actual exception
# is) is the useful part — keep the last N bytes.
SMOKE_ERROR_MAX_BYTES = 1500


def smoke_test_report(name: str) -> str | None:
    """Run a stored report's ``build(ctx)`` once and return any error.

    Returns ``None`` on success, otherwise a truncated error message
    suitable for surfacing back to the LLM via tool result. Used by
    ``define_report`` and ``edit_report_script`` so runtime errors land
    at the moment the model has the most context to fix them — instead
    of after the user opens the iframe and hits a red error page.

    The script's run metadata is updated either way (this counts as a
    real run via ``report_script_layout``), so subsequent ``list_reports``
    output reflects the smoke result.
    """

    try:
        report_script_layout(name)
        return None
    except ScriptError as exc:
        msg = str(exc)
        if len(msg) > SMOKE_ERROR_MAX_BYTES:
            msg = "…[truncated]…\n" + msg[-SMOKE_ERROR_MAX_BYTES:]
        return msg
    except Exception as exc:  # pragma: no cover — defensive
        return f"{type(exc).__name__}: {str(exc)[:SMOKE_ERROR_MAX_BYTES]}"


# ---- public: cleanup ------------------------------------------------------


def clear_script_output() -> dict:
    """Delete every ``runs/<run_id>/`` directory under every script.
    Doesn't touch script source or meta. Returns
    ``{freed_bytes, removed_runs}``."""

    freed = 0
    removed = 0
    for base in (SCRIPTS_COMPUTE, SCRIPTS_REPORTS):
        if not base.exists():
            continue
        for script_dir in base.iterdir():
            if not script_dir.is_dir():
                continue
            runs = script_dir / "runs"
            if not runs.is_dir():
                continue
            for run_dir in list(runs.iterdir()):
                if not run_dir.is_dir():
                    continue
                for f in run_dir.rglob("*"):
                    if f.is_file():
                        freed += f.stat().st_size
                shutil.rmtree(run_dir)
                removed += 1
    return {"freed_bytes": freed, "removed_runs": removed}
