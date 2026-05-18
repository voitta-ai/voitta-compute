"""Script tools — define / run / list / inspect / delete user Python scripts.

Two flavours:

  • **Compute scripts** — the model writes Python that operates on
    ``python_storage`` snapshots (or anything in scope), emits inline
    text + images via ``ctx.text() / ctx.image()``, and returns a small
    JSON value. ``run_compute`` is define-and-run (last-write-wins on
    name) — that matches the "iterate fast" use case better than a
    separate define-then-run pair.

  • **Report scripts** — the model writes Python that returns a Panel
    layout. ``define_report`` persists the script; the layout is built
    per browser session by the Panel app at ``/panel/reports?id=<slug>``
    (see ``app.services.panel_app.panel_factory`` +
    ``app.services.scripts.report_script_layout``). The pre-existing
    ``show_holoviz_report`` tool is the trigger that opens the iframe
    pane.

Why these tools live in ``scripts.py`` instead of ``buffers.py``:

  • Compute scripts work against ``python_storage`` (server-side
    persistent state), not browser-side buffers.
  • They run in the FastAPI process — full pandas/matplotlib/panel
    surface, no Web Worker sandbox.
  • Their results survive the chat turn (script source is kept on
    disk; you can re-run by name later).
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.services import scripts
from app.tools.registry import ToolCtx, ToolSpec, registry


# NOTE: pattern-based doc-section hints were removed in Stage 3. They
# encoded "hidden agenda" (the tool layer guessing which doc the LLM
# should read) and went stale as docs reshuffled. The tool now returns
# the raw traceback; the LLM searches the RAG with the error text.


_HINT_ON_SMOKE_ERROR = (
    "build(ctx) raised at smoke-test time — see smoke_error for the raw "
    "traceback. Search the RAG (rag_query, corpus='docs') for the error "
    "text or for 'panel <feature>' patterns, then fix via "
    "edit_report_script."
)


# ---- run_compute ----------------------------------------------------------


async def _run_compute(args: dict[str, Any], ctx: ToolCtx) -> Any:
    name = (args.get("name") or "").strip()
    code = args.get("code") or ""
    if not name:
        return {"ok": False, "error": "name required"}
    if not code:
        return {"ok": False, "error": "code required"}
    script_args = args.get("args")
    timeout_s = args.get("timeout_s")
    return await scripts.run_compute(
        name, code, script_args, timeout_s=float(timeout_s) if timeout_s else None
    )


registry.register(
    ToolSpec(
        name="run_compute",
        description=(
            "Define-and-run a Python compute script that operates on "
            "`python_storage` snapshots. The script is persisted under "
            "`python_storage/compute/<name>/code.py` (re-running the same name "
            "overwrites — that's the intended iteration loop).\n"
            "\n"
            "Script signature: `def run(ctx, args=None) -> any`.\n"
            "\n"
            "ctx methods (see services/scripts.py::ScriptContext):\n"
            "  • ctx.snapshot(handle)  → snapshot record\n"
            "  • ctx.dataframe(handle) → pd.DataFrame loaded from curves.pkl\n"
            "  • ctx.raw(handle)       → parsed raw.json\n"
            "  • ctx.text(markdown)    → emit inline markdown to chat\n"
            "  • ctx.image(fig, alt?)  → save matplotlib Figure / PIL Image / "
            "    bytes; emit inline <img> in chat. Returns the URL path.\n"
            "  • ctx.log(*args)        → debug log line (≤ 200 lines, "
            "    1 KB each).\n"
            "\n"
            "The script's return value is small JSON. Inline output items "
            "(`ctx.text` / `ctx.image`) flow back as separate `rich` SSE "
            "events and render in chat between this tool's start/end "
            "markers — values never travel through the LLM context.\n"
            "\n"
            "Trust model: in-process execution, full venv imports, "
            "60 s default timeout (max 300 s). Same trust as buffer_eval. "
            "Errors return {ok:false, error: <truncated traceback>}."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Slugified to [a-z0-9_-], max 64 chars. Re-using a name overwrites.",
                },
                "code": {"type": "string", "description": "Python source defining `def run(ctx, args=None)`."},
                "args": {
                    "description": "Optional JSON value passed as the script's second argument.",
                },
                "timeout_s": {
                    "type": "number",
                    "minimum": 1,
                    "maximum": 300,
                    "default": 60,
                },
            },
            "required": ["name", "code"],
            "additionalProperties": False,
        },
        handler=_run_compute,
        side="server",
    )
)


# ---- list_compute_scripts -------------------------------------------------


async def _list_compute_scripts(args: dict[str, Any], ctx: ToolCtx) -> Any:
    return {"scripts": scripts.list_compute()}


registry.register(
    ToolSpec(
        name="list_compute_scripts",
        description=(
            "List every persisted compute script. Returns "
            "{scripts: [{name, kind, created_at, updated_at, "
            "code_bytes, last_run_at, last_run_ok, last_run_id, "
            "last_run_elapsed_s, last_run_error?}, ...]}."
        ),
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=_list_compute_scripts,
        side="server",
    )
)


# ---- get_compute_script ---------------------------------------------------


async def _get_compute_script(args: dict[str, Any], ctx: ToolCtx) -> Any:
    name = (args.get("name") or "").strip()
    if not name:
        return {"ok": False, "error": "name required"}
    rec = scripts.get_compute(name)
    return rec if rec else {"ok": False, "error": f"no compute script named {name!r}"}


registry.register(
    ToolSpec(
        name="get_compute_script",
        description="Read back a compute script's source + metadata by name.",
        input_schema={
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
            "additionalProperties": False,
        },
        handler=_get_compute_script,
        side="server",
    )
)


# ---- delete_compute_script ------------------------------------------------


async def _delete_compute_script(args: dict[str, Any], ctx: ToolCtx) -> Any:
    name = (args.get("name") or "").strip()
    if not name:
        return {"ok": False, "error": "name required"}
    return {"ok": True, "deleted": scripts.delete_compute(name)}


registry.register(
    ToolSpec(
        name="delete_compute_script",
        description="Delete a compute script (source + metadata). Irreversible.",
        input_schema={
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
            "additionalProperties": False,
        },
        handler=_delete_compute_script,
        side="server",
    )
)


# ---- define_report --------------------------------------------------------


async def _define_report(args: dict[str, Any], ctx: ToolCtx) -> Any:
    name = (args.get("name") or "").strip()
    code = args.get("code") or ""
    if not name:
        return {"ok": False, "error": "name required"}
    if not code:
        return {"ok": False, "error": "code required"}
    try:
        rec = scripts.define_report(name, code)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    # Smoke-test build(ctx) so the model sees runtime errors here rather
    # than after the user opens the iframe. Run in a thread because Panel
    # / Bokeh / matplotlib aren't async-friendly.
    smoke_error = await asyncio.to_thread(scripts.smoke_test_report, rec["name"])
    response: dict[str, Any] = {
        "ok": smoke_error is None,
        **rec,
        "report_id": rec["name"],
        "render_url_path": f"/panel/reports?id={rec['name']}",
        "smoke_error": smoke_error,
    }
    if smoke_error:
        response["hint"] = _HINT_ON_SMOKE_ERROR
    return response


registry.register(
    ToolSpec(
        name="define_report",
        description=(
            "Define-or-update a HoloViz Panel report. Persisted under "
            "`python_storage/reports/<name>/code.py`. Open via "
            "`show_holoviz_report(report_id=<name>)`.\n"
            "\n"
            "BEFORE AUTHORING: rag_query the docs corpus for "
            "'panel-skeleton' (then 'panel-snapshots' / 'panel-theming' / "
            "'panel-three-scene' / 'panel-common-errors' / "
            "'panel-screenshot-limits' as needed). These short, intent-"
            "keyed docs are authoritative — the API evolves, your priors "
            "may not match.\n"
            "\n"
            "Script signature: `def build(ctx) -> pn.viewable.Viewable`. "
            "Return any Viewable — content layout (Column / Row / "
            "GridSpec / Card / pane) or, if you have a strong reason, a "
            "pn.template.* (the host detects and uses it as-is). For "
            "content layouts the host wraps in EditableTemplate.\n"
            "\n"
            "Smoke test: tool re-runs build(ctx) after persist. Failures "
            "land in `smoke_error` (raw traceback). Use edit_report_script "
            "for targeted fixes (faster wall-clock than re-submitting "
            "full source)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "code": {"type": "string"},
            },
            "required": ["name", "code"],
            "additionalProperties": False,
        },
        handler=_define_report,
        side="server",
    )
)


# ---- list_reports ---------------------------------------------------------


async def _list_reports(args: dict[str, Any], ctx: ToolCtx) -> Any:
    return {"reports": scripts.list_reports()}


registry.register(
    ToolSpec(
        name="list_reports",
        description=(
            "List every persisted report script. Same shape as "
            "list_compute_scripts."
        ),
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=_list_reports,
        side="server",
    )
)


# ---- get_report_script ----------------------------------------------------


async def _get_report_script(args: dict[str, Any], ctx: ToolCtx) -> Any:
    name = (args.get("name") or "").strip()
    if not name:
        return {"ok": False, "error": "name required"}
    rec = scripts.get_report_script(name)
    return rec if rec else {"ok": False, "error": f"no report script named {name!r}"}


registry.register(
    ToolSpec(
        name="get_report_script",
        description="Read back a report script's source + metadata by name.",
        input_schema={
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
            "additionalProperties": False,
        },
        handler=_get_report_script,
        side="server",
    )
)


# ---- delete_report_script -------------------------------------------------


async def _delete_report_script(args: dict[str, Any], ctx: ToolCtx) -> Any:
    name = (args.get("name") or "").strip()
    if not name:
        return {"ok": False, "error": "name required"}
    return {"ok": True, "deleted": scripts.delete_report(name)}


registry.register(
    ToolSpec(
        name="delete_report_script",
        description="Delete a report script (source + metadata). Irreversible.",
        input_schema={
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
            "additionalProperties": False,
        },
        handler=_delete_report_script,
        side="server",
    )
)


# ---- edit_compute_script / edit_report_script -----------------------------
# Search-and-replace editing on a stored script. The model passes a list of
# `{find, replace, replace_all?}` ops; each is applied in order to the live
# code. Same semantics as Claude Code's Edit tool — exact string match,
# error on missing or non-unique unless replace_all=True.
#
# Why this exists: redefining a 200-line report to fix one chart axis is
# both wasteful and error-prone (tokens, accidental drift in unrelated
# code). Use this when the change is localised. Use define_compute /
# define_report when restructuring large chunks.


_EDIT_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "description": "Existing script name (will be slugified to match storage).",
        },
        "edits": {
            "type": "array",
            "minItems": 1,
            "description": (
                "Ordered list of search-replace operations. Each edit "
                "applies to the result of all preceding edits. If any "
                "edit fails (not found, non-unique without replace_all, "
                "or final result has a syntax error), nothing is written."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "find": {
                        "type": "string",
                        "description": (
                            "Exact substring to find. Match must be unique "
                            "within the current script unless replace_all "
                            "is true. Include surrounding context (lines "
                            "above/below) when needed to make it unique."
                        ),
                    },
                    "replace": {
                        "type": "string",
                        "description": "Replacement text (may be empty to delete).",
                    },
                    "replace_all": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "If true, replace every occurrence of `find`. "
                            "Use for renaming a variable or constant across "
                            "the script."
                        ),
                    },
                },
                "required": ["find", "replace"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["name", "edits"],
    "additionalProperties": False,
}


async def _edit_compute_script(args: dict[str, Any], ctx: ToolCtx) -> Any:
    name = (args.get("name") or "").strip()
    edits = args.get("edits") or []
    if not name:
        return {"ok": False, "error": "name required"}
    try:
        rec = scripts.edit_script("compute", name, edits)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, **rec}


async def _edit_report_script(args: dict[str, Any], ctx: ToolCtx) -> Any:
    name = (args.get("name") or "").strip()
    edits = args.get("edits") or []
    if not name:
        return {"ok": False, "error": "name required"}
    try:
        rec = scripts.edit_script("reports", name, edits)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    # Smoke-test the edited script. Same rationale as define_report:
    # surface runtime errors at edit time, not when the iframe loads.
    smoke_error = await asyncio.to_thread(scripts.smoke_test_report, rec["name"])
    response: dict[str, Any] = {
        "ok": smoke_error is None,
        **rec,
        "report_id": rec["name"],
        "render_url_path": f"/panel/reports?id={rec['name']}",
        "smoke_error": smoke_error,
    }
    if smoke_error:
        response["hint"] = _HINT_ON_SMOKE_ERROR
    return response


registry.register(
    ToolSpec(
        name="edit_compute_script",
        description=(
            "Apply a list of search-replace edits to an existing compute "
            "script (defined via run_compute). Use this for localised "
            "changes — fixing a column name, adjusting a threshold, "
            "tweaking ctx.text formatting — instead of redefining the "
            "whole script.\n"
            "\n"
            "Each edit is {find, replace, replace_all?}. Edits apply in "
            "order; the next edit sees the result of the previous one. "
            "Without replace_all, `find` must match exactly once — add "
            "surrounding context if the literal target appears elsewhere. "
            "If any edit fails or the resulting code doesn't parse, "
            "nothing is written.\n"
            "\n"
            "After editing, call run_compute(name, code=...) with the new "
            "code path's contents — or just call run_compute by name and "
            "rely on its define-and-run behaviour. Returns "
            "{ok, name, code_path, applied}."
        ),
        input_schema=_EDIT_INPUT_SCHEMA,
        handler=_edit_compute_script,
        side="server",
    )
)


registry.register(
    ToolSpec(
        name="edit_report_script",
        description=(
            "Apply a list of search-replace edits to an existing report "
            "script (defined via define_report). Use this for localised "
            "changes — fixing a chart title, tweaking a Markdown header, "
            "swapping a colour — instead of redefining the whole script.\n"
            "\n"
            "Each edit is {find, replace, replace_all?}. Edits apply in "
            "order; the next edit sees the result of the previous one. "
            "Without replace_all, `find` must match exactly once — add "
            "surrounding context if the literal target appears elsewhere. "
            "If any edit fails or the resulting code doesn't parse, "
            "nothing is written.\n"
            "\n"
            "Reload the iframe (re-call show_holoviz_report) to see the "
            "edited report — Panel sessions cache the layout from the "
            "moment the iframe connected. Returns "
            "{ok, name, code_path, applied, report_id, render_url_path}.\n"
            "\n"
            "After applying the edits, the tool runs `build(ctx)` once as "
            "a smoke test. If it raises, the truncated traceback (~1500 "
            "bytes) is returned in `smoke_error` so you can iterate before "
            "the user sees a red error page in the iframe."
        ),
        input_schema=_EDIT_INPUT_SCHEMA,
        handler=_edit_report_script,
        side="server",
    )
)


# ---- clear_script_output --------------------------------------------------
# Bonus utility: delete every file under python_storage/script_output/.
# Doesn't touch script source files.


async def _clear_script_output(args: dict[str, Any], ctx: ToolCtx) -> Any:
    return scripts.clear_script_output()


registry.register(
    ToolSpec(
        name="clear_script_output",
        description=(
            "Delete every `runs/<run_id>/` directory under every script "
            "(the directory where ctx.image() saves PNGs). Doesn't "
            "touch script source or meta. Returns "
            "{freed_bytes, removed_runs}."
        ),
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=_clear_script_output,
        side="server",
    )
)
