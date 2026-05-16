"""Flow report tools — define / edit / show / list / inspect flow charts.

A flow report is an LLM-authored Python script that constructs a
``FlowBuilder`` describing a process / state diagram. The script is
persisted (``scripts/flows/<slug>/code.py``); on ``show_flow_report``
the server execs it, calls ``build(ctx)``, coerces to a definition
dict, and ships the dict to the frontend via the ``show_flow_report``
browser primitive. The frontend renders it with ReactFlow inside the
existing widget shadow DOM — no iframe, no Panel, no Bokeh.

Tool surface mirrors the holoviz family:

    define_report          ─►  define_flow_report
    edit_report_script     ─►  edit_flow_report
    list_reports           ─►  list_flow_reports
    get_report_script      ─►  get_flow_report
    delete_report_script   ─►  delete_flow_report
    show_holoviz_report    ─►  show_flow_report
    get_report_render_errors ► get_flow_render_errors  (reuses render_events)

The frontend posts ready/error events to ``/api/report-render-events``
the same way the holoviz iframe shim does — so the same persistence
and read path covers both report kinds.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from app.services import flows, render_events
from app.tools.browser import call_browser
from app.tools.registry import ToolCtx, ToolSpec, registry


DEFAULT_RENDER_WAIT_S = 3.0  # flows render synchronously — short tail


# ---- define_flow_report ---------------------------------------------------


_FLOW_SCRIPT_DOC = (
    "Flow script signature: `def build(ctx) -> FlowBuilder | dict`.\n"
    "\n"
    "FlowBuilder API (chainable; the class is in scope automatically):\n"
    "\n"
    "  p = FlowBuilder('Process Name', 'Short description')\n"
    "\n"
    "  # Diagram-level configuration (all optional):\n"
    "  p.layout(direction='TB', engine='elk')\n"
    "      # direction: TB | LR | BT | RL          (top-down default)\n"
    "      # engine:    elk (default, higher quality) | dagre (faster)\n"
    "  p.edge_style('smoothstep')\n"
    "      # smoothstep (engineering default, orthogonal+rounded)\n"
    "      # step | straight | bezier\n"
    "  p.edge_options(border_radius=8, offset=20, step_position=0.5)\n"
    "      # Tune smoothstep routing: corner softness, distance to first\n"
    "      # turn, midpoint of the trunk. All optional.\n"
    "  p.background('dots')          # dots | lines | cross | none\n"
    "  p.show_minimap(True)          # corner overview, default off\n"
    "  p.title_block(drawing_id='APR-001', rev='B', author='voitta')\n"
    "      # engineering-drawing-style corner block\n"
    "  p.palette('dark')\n"
    "      # 'light' (default) | 'dark'. Picks node-body colours\n"
    "      # (bg / fg / muted / faint / border). Ships as a 5-key\n"
    "      # dict on config.palette — fully visible on the wire.\n"
    "      # Per-node style={...} wins over this; plugin theme.css\n"
    "      # :host { --voitta-flow-node-*: ... } wins over plain defaults\n"
    "      # but loses to p.palette() and per-node style=.\n"
    "  p.color_mode('auto')\n"
    "      # ReactFlow's INTERNAL chrome scheme (Controls panel,\n"
    "      # attribution). Independent of p.palette() — set both.\n"
    "      # 'auto' follows the palette name; 'light'/'dark'/'system'\n"
    "      # set it explicitly.\n"
    "\n"
    "  # Steps (all share the same customization kwargs):\n"
    "  p.trigger(id, label, roles=[...], artifacts_out=[...], **viz)\n"
    "  p.activity(id, label, roles=[...], artifacts_in=[...],\n"
    "             artifacts_out=[...], **viz)\n"
    "  p.decision(id, label, shape='rect'|'port'|'diamond'|'junction',\n"
    "             branches=[(label, target_id), ...], **viz)\n"
    "      # shape='rect' (default) — rectangle + DECISION chip; edge\n"
    "      #     labels. Good for 2–3 branches with short labels.\n"
    "      # shape='port' — schematic multi-port; each branch is a\n"
    "      #     labeled output row on the node, edges leave unlabeled.\n"
    "      #     Best for 4+ branches or descriptive labels (e.g.\n"
    "      #     'Critical / High / Medium / Low / Deferred').\n"
    "      # shape='diamond' — classic BPMN rotated rhombus. Use when\n"
    "      #     the SHAPE should signal 'this is a question'.\n"
    "      # shape='junction' — tiny routing node, labels live on\n"
    "      #     edges. For many branches where labels ARE the\n"
    "      #     content (no roles/meta/note allowed).\n"
    "  p.artifact(id, label, ..., **viz)\n"
    "  p.end(id, label='End', **viz)\n"
    "\n"
    "  # Edges:\n"
    "  p.connect(from_id, to_id, label='', style='solid'|'dashed',\n"
    "            tone='default'|'info'|'success'|'warning'|'critical',\n"
    "            marker='arrow-closed'|'arrow'|'none',\n"
    "            animated=True|False,           # marching-ants on the edge\n"
    "            border_radius=8)               # per-edge corner softness\n"
    "\n"
    "  # Swimlanes:\n"
    "  p.group(id, label, color='var(--voitta-surface)')\n"
    "\n"
    "  return p   # or return p.to_dict()\n"
    "\n"
    "Step visual customization (**viz):\n"
    "  tone='default'|'info'|'success'|'warning'|'critical'\n"
    "      Colours the title bar and outgoing edge accent. Maps onto\n"
    "      --voitta-flow-tone-* tokens, so plugin themes can rebrand.\n"
    "  icon='play' | 'check' | 'alert-triangle' | ... (any lucide-icons\n"
    "      name in kebab-case; see https://lucide.dev/icons).\n"
    "      Or icon={'svg': '<svg .../>'} for an inline SVG escape hatch.\n"
    "  badges=['Manager', {'label': 'SLA: 48h', 'tone': 'warning'}]\n"
    "      Small pills in the node body. List of strings or {label,tone}\n"
    "      dicts.\n"
    "  meta=[('Input', 'Request Form'), {'key': 'Out', 'value': 'Decision'}]\n"
    "      Key/value rows in the node body.\n"
    "  note='Skips review if amount < $1,000'\n"
    "      Single-line annotation below the meta block.\n"
    "  style={'background': '#1e293b', 'border-radius': '6px', ...}\n"
    "      Arbitrary CSS escape hatch (visual properties only; layout-\n"
    "      breaking props rejected). Applied to the node container.\n"
    "  title_style={'background': '#0f172a', 'color': '#f1f5f9'}\n"
    "      Same shape; targets the title bar specifically.\n"
    "\n"
    "Notes:\n"
    "  • Step types: trigger | activity | decision | artifact | end.\n"
    "  • Decision branches auto-emit connections — do NOT call .connect()\n"
    "    for branch arrows; use branches=[...] on .decision(). First\n"
    "    branch defaults to style='solid', remaining to 'dashed'.\n"
    "  • Target step IDs may be defined later in the script; reference\n"
    "    validity is checked at build time.\n"
    "  • ctx.get_theme(host=...) returns the active palette.\n"
    "  • ctx.log(*args) appends a debug line, surfaced as `log_lines`.\n"
    "\n"
    "Rendering: ReactFlow inside the widget shadow DOM. Nodes are real\n"
    "components — title-bar header with icon + step ID in monospace,\n"
    "body with label / badges / meta rows / note. Default aesthetic is\n"
    "engineering-schematic (dotted grid, orthogonal smoothstep edges,\n"
    "slate-blue palette). Plugin theme.css overrides the\n"
    "--voitta-flow-tone-* family to re-skin without touching scripts.\n"
)


async def _define_flow_report(args: dict[str, Any], ctx: ToolCtx) -> Any:
    name = (args.get("name") or "").strip()
    code = args.get("code") or ""
    if not name:
        return {"ok": False, "error": "name required"}
    if not code:
        return {"ok": False, "error": "code required"}
    try:
        rec = flows.define_flow(name, code)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    # Smoke-test build(ctx) so the model sees errors here, not at show
    # time. Runs in a thread for symmetry with define_report (flows are
    # actually fast, but a malicious infinite loop in user code would
    # still block the event loop without it).
    smoke_error = await asyncio.to_thread(flows.smoke_test_flow, rec["name"])

    response: dict[str, Any] = {
        "ok": True,
        **rec,
        "report_id": rec["name"],
    }
    if smoke_error:
        response["smoke_error"] = smoke_error
        response["hint"] = (
            f"Script saved, but build(ctx) raised. Fix it via "
            f"edit_flow_report (or another define_flow_report) and "
            f"re-test before calling show_flow_report(report_id="
            f"{rec['name']!r})."
        )
    else:
        response["hint"] = (
            f"Use show_flow_report(report_id={rec['name']!r}) to open it "
            "in the report pane next to the chat."
        )
    return response


registry.register(
    ToolSpec(
        name="define_flow_report",
        description=(
            "Define-or-update a flow-chart report. The script is persisted "
            "under `scripts/flows/<name>/code.py`. Rendering is "
            "ReactFlow-based inside the widget shadow DOM — there is no "
            "iframe, no Panel app, no Bokeh session.\n"
            "\n" + _FLOW_SCRIPT_DOC +
            "\n"
            "After persisting, the tool runs `build(ctx)` once as a smoke "
            "test. If it raises, the truncated traceback is returned in "
            "`smoke_error` and the hint points back at edit_flow_report. "
            "Show the user a working flow with `show_flow_report` only "
            "after smoke_error is absent."
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
        handler=_define_flow_report,
        side="server",
    )
)


# ---- edit_flow_report -----------------------------------------------------
# Same {find, replace, replace_all?} edit shape as edit_report_script.


_EDIT_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "edits": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "find": {"type": "string"},
                    "replace": {"type": "string"},
                    "replace_all": {"type": "boolean", "default": False},
                },
                "required": ["find", "replace"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["name", "edits"],
    "additionalProperties": False,
}


async def _edit_flow_report(args: dict[str, Any], ctx: ToolCtx) -> Any:
    from app.services import scripts as _scripts

    name = (args.get("name") or "").strip()
    edits = args.get("edits") or []
    if not name:
        return {"ok": False, "error": "name required"}
    try:
        rec = _scripts.edit_script("flow", name, edits)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    smoke_error = await asyncio.to_thread(flows.smoke_test_flow, rec["name"])
    response: dict[str, Any] = {"ok": True, **rec, "report_id": rec["name"]}
    if smoke_error:
        response["smoke_error"] = smoke_error
    return response


registry.register(
    ToolSpec(
        name="edit_flow_report",
        description=(
            "Apply search-replace edits to an existing flow report script. "
            "Same semantics as edit_report_script: ordered list of "
            "{find, replace, replace_all?}; each match must be unique "
            "unless replace_all=true; atomic on failure; final code must "
            "parse. Smoke-tests build(ctx) after applying; returns "
            "smoke_error on failure. Re-call show_flow_report to see the "
            "edited diagram."
        ),
        input_schema=_EDIT_INPUT_SCHEMA,
        handler=_edit_flow_report,
        side="server",
    )
)


# ---- list_flow_reports / get_flow_report / delete_flow_report -------------


async def _list_flow_reports(args: dict[str, Any], ctx: ToolCtx) -> Any:
    return {"reports": flows.list_flows()}


registry.register(
    ToolSpec(
        name="list_flow_reports",
        description="List every persisted flow report. Same shape as list_reports.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=_list_flow_reports,
        side="server",
    )
)


async def _get_flow_report(args: dict[str, Any], ctx: ToolCtx) -> Any:
    name = (args.get("name") or "").strip()
    if not name:
        return {"ok": False, "error": "name required"}
    rec = flows.get_flow(name)
    return rec if rec else {"ok": False, "error": f"no flow report named {name!r}"}


registry.register(
    ToolSpec(
        name="get_flow_report",
        description="Read back a flow report's source + metadata by name.",
        input_schema={
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
            "additionalProperties": False,
        },
        handler=_get_flow_report,
        side="server",
    )
)


async def _delete_flow_report(args: dict[str, Any], ctx: ToolCtx) -> Any:
    name = (args.get("name") or "").strip()
    if not name:
        return {"ok": False, "error": "name required"}
    return {"ok": True, "deleted": flows.delete_flow(name)}


registry.register(
    ToolSpec(
        name="delete_flow_report",
        description="Delete a flow report (source + metadata). Irreversible.",
        input_schema={
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
            "additionalProperties": False,
        },
        handler=_delete_flow_report,
        side="server",
    )
)


# ---- show_flow_report -----------------------------------------------------


def _summarise_event(ev: render_events.RenderEvent) -> dict[str, Any]:
    return {
        "kind": ev.kind,
        "ts": ev.ts,
        "source": ev.source,
        "message": ev.message,
        "stack": ev.stack,
    }


async def _show_flow_report(args: dict[str, Any], ctx: ToolCtx) -> Any:
    report_id = str(args.get("report_id") or "").strip()
    if not report_id:
        return {"ok": False, "error": "report_id required"}
    title = args.get("title") or f"Flow {report_id}"
    wait_s = float(args.get("wait_s") or DEFAULT_RENDER_WAIT_S)
    wait_s = max(0.2, min(15.0, wait_s))

    # Server-side build first. If this raises, the LLM gets the build
    # error synchronously — no need to round-trip through the frontend.
    from app.services.scripts import ScriptError

    try:
        definition, log_lines = await asyncio.to_thread(
            flows.build_flow_definition, report_id
        )
    except ScriptError as exc:
        return {
            "ok": False,
            "report_id": report_id,
            "status": "errored",
            "errors": [{
                "kind": "error",
                "source": "server:builder",
                "message": str(exc),
                "stack": None,
                "ts": time.time(),
            }],
            "hint": (
                "build(ctx) raised. Fix via edit_flow_report and re-call "
                "show_flow_report."
            ),
        }

    # Mint render_id and begin await BEFORE asking the browser to mount,
    # so an instant ready signal can't beat the registration.
    render_id = render_events.new_render_id()
    ready = render_events.begin_await(render_id, report_id)

    primitive_result = await call_browser(
        "show_flow_report",
        {
            "definition": definition,
            "report_id": report_id,
            "title": title,
            "render_id": render_id,
        },
        ctx,
    )

    started = time.time()
    status = "timeout"
    saw_error = False
    try:
        await asyncio.wait_for(ready.wait(), timeout=wait_s)
        _, events = render_events.collect(render_id)
        saw_error = any(e.kind == "error" for e in events)
        saw_ready = any(e.kind == "ready" for e in events)
        if saw_error:
            status = "errored"
        elif saw_ready:
            status = "ready"
        else:
            status = "unknown"
    except asyncio.TimeoutError:
        status = "timeout"
    finally:
        render_events.end_await(render_id)

    _, events = render_events.collect(render_id)
    errors = [_summarise_event(e) for e in events if e.kind == "error"]
    elapsed_ms = int((time.time() - started) * 1000)

    out: dict[str, Any] = {
        "ok": not saw_error,
        "report_id": report_id,
        "title": title,
        "render_id": render_id,
        "status": status,
        "elapsed_ms": elapsed_ms,
        "errors": errors,
        "log_lines": log_lines,
        "steps": len(definition["process"]["steps"]),
        "connections": len(definition["process"]["connections"]),
    }
    if isinstance(primitive_result, dict):
        for k, v in primitive_result.items():
            out.setdefault(k, v)
    if saw_error:
        out["hint"] = (
            "The diagram surfaced render-time errors. Check errors[*].message; "
            "common causes are broken React renders inside a node component "
            "(rare unless a node label contains something unusual)."
        )
    elif status == "timeout":
        out["hint"] = (
            "Diagram never reported ready within the wait window. The user "
            "may not have an active chat pane open."
        )
    return out


registry.register(
    ToolSpec(
        name="show_flow_report",
        description=(
            "Open a flow-chart report in the report pane next to the chat. "
            "Renders a stored flow script as a ReactFlow diagram inside "
            "the widget shadow DOM — no iframe, no Panel.\n"
            "\n"
            "Returns status ('ready' | 'errored' | 'timeout'), elapsed_ms, "
            "errors[], log_lines from the build, and step/connection "
            "counts.\n"
            "\n"
            "If show_flow_report is called while a holoviz report is open, "
            "the holoviz report is replaced — there is one report slot. "
            "The user can close the diagram with the × button.\n"
            "\n"
            "Errors that surface AFTER the tool returns (during user "
            "interaction with the diagram) are persisted; pull them with "
            "get_flow_render_errors(report_id)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "report_id": {
                    "type": "string",
                    "description": "Slug of a flow report created via define_flow_report.",
                },
                "title": {"type": "string", "description": "Pane title."},
                "wait_s": {
                    "type": "number",
                    "minimum": 0.2,
                    "maximum": 15.0,
                    "default": DEFAULT_RENDER_WAIT_S,
                },
            },
            "required": ["report_id"],
            "additionalProperties": False,
        },
        handler=_show_flow_report,
        side="hybrid",
    )
)


# ---- get_flow_render_errors ----------------------------------------------


async def _get_flow_render_errors(args: dict[str, Any], ctx: ToolCtx) -> Any:
    report_id = str(args.get("report_id") or "").strip()
    if not report_id:
        return {"ok": False, "error": "report_id required"}
    since_ts = args.get("since_ts")
    since_ts_f: float | None = None
    if since_ts is not None:
        try:
            since_ts_f = float(since_ts)
        except (TypeError, ValueError):
            return {"ok": False, "error": "since_ts must be a number"}
    limit = max(1, min(200, int(args.get("limit") or 50)))
    entries = render_events.list_recent_for_report(
        report_id, since_ts=since_ts_f, kinds=("error",), limit=limit
    )
    return {
        "ok": True,
        "report_id": report_id,
        "count": len(entries),
        "errors": entries,
    }


registry.register(
    ToolSpec(
        name="get_flow_render_errors",
        description=(
            "Read render-time errors that an open flow diagram has posted "
            "back to the backend. Same storage as get_report_render_errors "
            "— flow reports POST to /api/report-render-events the same way "
            "holoviz iframes do (just without the iframe). Use when the "
            "user reports a previously-shown flow is broken."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "report_id": {"type": "string"},
                "since_ts": {"type": "number"},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 200,
                    "default": 50,
                },
            },
            "required": ["report_id"],
            "additionalProperties": False,
        },
        handler=_get_flow_render_errors,
        side="server",
    )
)
