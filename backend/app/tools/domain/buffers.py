"""Buffer-aware tools.

Buffers are browser-side data stores: arbitrary JSON the LLM has decided
to keep around for further inspection. The LLM never sees the raw
values — only a small **handle** + **summary**. Operating on the data
(filtering, plotting, eval'ing arbitrary JS) all happens client-side
via the bridge.

Data lands in a buffer via either ``buffer_eval`` (arbitrary JS, return
the value) or by an upstream tool that writes one. The Python side is
intentionally thin — every tool is a one-line bridge call to the
matching browser primitive. Schemas live here; logic lives client-side.

    list_buffers           — what's in memory and how big
    get_buffer_summary     — re-show a buffer's summary (for forgotten
                              handles)
    delete_buffer          — free one
    delete_buffer_keys     — free PARTS of one (e.g. drop heavy series)
    clear_buffers          — free everything

    query_buffer_curves    — declarative metadata filter (NO values)
    plot_buffer_curves     — declarative Chart.js plot (curves-aware)
    plot_bars_from_buffer  — declarative group-by histogram
    plot                   — declarative plot from inline data
    buffer_eval            — arbitrary JS in a sandboxed Worker;
                              CAN return a small JSON value AND emit
                              draw.* commands inline
"""

from __future__ import annotations

from typing import Any

from app.services.user_settings import js_compute_enabled
from app.tools.browser import call_browser
from app.tools.registry import ToolCtx, ToolSpec, registry


# Generic helper: call the browser primitive, return the raw envelope.
async def _call(name: str, args: dict[str, Any], ctx: ToolCtx, *, timeout_ms: int = 60_000) -> Any:
    return await call_browser(name, args, ctx, timeout_ms=timeout_ms)


# ---- list_buffers ---------------------------------------------------------


async def _list_buffers(args: dict[str, Any], ctx: ToolCtx) -> Any:
    return await _call("buffer_list", {}, ctx)


registry.register(
    ToolSpec(
        name="list_buffers",
        description=(
            "List every buffer the browser is currently holding. Returns "
            "{totals: {count, bytes}, buffers: [{handle, kind, bytes, "
            "summary, createdAt, meta}, …]}. Use to remind yourself what "
            "data is available without re-fetching."
        ),
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=_list_buffers,
        side="hybrid",
        visibility_check=js_compute_enabled,
    )
)


# ---- get_buffer_summary --------------------------------------------------


async def _get_buffer_summary(args: dict[str, Any], ctx: ToolCtx) -> Any:
    return await _call("buffer_get_summary", args, ctx)


registry.register(
    ToolSpec(
        name="get_buffer_summary",
        description=(
            "Re-show one buffer's summary (handle, kind, bytes, summary, "
            "created_at, meta). Useful when you've forgotten what's behind "
            "a handle."
        ),
        input_schema={
            "type": "object",
            "properties": {"handle": {"type": "string"}},
            "required": ["handle"],
            "additionalProperties": False,
        },
        handler=_get_buffer_summary,
        side="hybrid",
        visibility_check=js_compute_enabled,
    )
)


# ---- delete_buffer -------------------------------------------------------


async def _delete_buffer(args: dict[str, Any], ctx: ToolCtx) -> Any:
    return await _call("buffer_delete", args, ctx)


registry.register(
    ToolSpec(
        name="delete_buffer",
        description=(
            "Free one browser-side buffer entirely. Returns {deleted: bool}."
        ),
        input_schema={
            "type": "object",
            "properties": {"handle": {"type": "string"}},
            "required": ["handle"],
            "additionalProperties": False,
        },
        handler=_delete_buffer,
        side="hybrid",
        visibility_check=js_compute_enabled,
    )
)


# ---- delete_buffer_keys (partial free) -----------------------------------


async def _delete_buffer_keys(args: dict[str, Any], ctx: ToolCtx) -> Any:
    return await _call("buffer_delete_keys", args, ctx)


registry.register(
    ToolSpec(
        name="delete_buffer_keys",
        description=(
            "Drop specific paths from a buffer's data without losing the "
            "rest of it. Useful for shedding heavy series after you've "
            "extracted what you need.\n"
            "\n"
            "Path syntax (lodash-style): "
            "'data.curves[5].series[1].values', 'curves[12]', "
            "'metadata.parsingReport'. Each path that exists is deleted; "
            "missing paths are reported in `not_found`. Returns "
            "{ok, dropped, not_found, bytes_before, bytes_after}."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "handle": {"type": "string"},
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "description": "Dot/bracket-notation paths into the buffer's data.",
                },
            },
            "required": ["handle", "paths"],
            "additionalProperties": False,
        },
        handler=_delete_buffer_keys,
        side="hybrid",
        visibility_check=js_compute_enabled,
    )
)


# ---- clear_buffers -------------------------------------------------------


async def _clear_buffers(args: dict[str, Any], ctx: ToolCtx) -> Any:
    return await _call("buffer_clear", {}, ctx)


registry.register(
    ToolSpec(
        name="clear_buffers",
        description=(
            "Free every browser-side buffer. Returns "
            "{freed_count, freed_bytes}. Use sparingly — it'll invalidate "
            "every handle in scope."
        ),
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=_clear_buffers,
        side="hybrid",
        visibility_check=js_compute_enabled,
    )
)


# ---- query_buffer_curves -------------------------------------------------


async def _query_buffer_curves(args: dict[str, Any], ctx: ToolCtx) -> Any:
    return await _call("buffer_query_curves", args, ctx)


registry.register(
    ToolSpec(
        name="query_buffer_curves",
        description=(
            "Filter the curves in a buffer by metadata equality and return "
            "small projection rows (id, name, requested metadata keys, "
            "seriesNames). Curve VALUES are NEVER included — that's what "
            "plot_buffer_curves and buffer_eval are for.\n"
            "\n"
            "filter is {metadataKey: exactValue, ...}. project is the "
            "metadata keys to return per row (default: all metadata keys, "
            "no series). limit is row cap (default 200, max 2000)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "handle": {"type": "string"},
                "filter": {"type": "object", "description": "e.g. {Name: 'FR'}"},
                "project": {"type": "array", "items": {"type": "string"}},
                "limit": {"type": "integer", "minimum": 1, "maximum": 2000, "default": 200},
            },
            "required": ["handle"],
            "additionalProperties": False,
        },
        handler=_query_buffer_curves,
        side="hybrid",
        visibility_check=js_compute_enabled,
    )
)


# ---- plot_buffer_curves --------------------------------------------------


async def _plot_buffer_curves(args: dict[str, Any], ctx: ToolCtx) -> Any:
    # Browser primitive name is `plot_xy_from_buffer`; the LLM-facing name
    # mirrors the plugin's `plot_buffer_curves` for familiarity.
    return await _call("plot_xy_from_buffer", args, ctx)


registry.register(
    ToolSpec(
        name="plot_buffer_curves",
        description=(
            "Plot curves from a buffer in chat — extracts x/y series "
            "locally without sending any numbers through your context.\n"
            "\n"
            "Workflow: filter curves by metadata (e.g. {Name: 'FR'}), pick "
            "xSeries/ySeries names, plot. Each matching curve becomes one "
            "trace, labelled by labelFromMetadata.\n"
            "\n"
            "For a frequency response: curveFilter={Name: 'FR'}, "
            "xSeries='Frequency', ySeries='Magnitude', "
            "xAxis={log: true}, labelFromMetadata='s/n'."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "handle": {"type": "string"},
                "curveFilter": {
                    "type": "object",
                    "description": "Metadata predicates, e.g. {Name: 'FR'}.",
                },
                "xSeries": {
                    "type": "string",
                    "description": "Series name for X axis (e.g. 'Frequency').",
                },
                "ySeries": {
                    "type": "string",
                    "description": "Series name for Y axis (e.g. 'Magnitude').",
                },
                "labelFromMetadata": {
                    "type": "string",
                    "description": "Metadata key to use as trace label (e.g. 's/n').",
                },
                "title": {"type": "string"},
                "xAxis": {"type": "object"},
                "yAxisLeft": {"type": "object"},
                "yAxisRight": {"type": "object"},
                "defaultType": {
                    "type": "string",
                    "enum": ["line", "scatter", "area"],
                },
                "legend": {"type": "object"},
                "height": {"type": "integer"},
                "maxTraces": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 200,
                    "default": 50,
                },
            },
            "required": ["handle", "xSeries", "ySeries"],
            "additionalProperties": False,
        },
        handler=_plot_buffer_curves,
        side="hybrid",
        visibility_check=js_compute_enabled,
    )
)


# ---- plot_bars_from_buffer -----------------------------------------------


async def _plot_bars_from_buffer(args: dict[str, Any], ctx: ToolCtx) -> Any:
    return await _call("plot_bars_from_buffer", args, ctx)


registry.register(
    ToolSpec(
        name="plot_bars_from_buffer",
        description=(
            "Group-by histogram from a curves buffer. Counts curves by the "
            "given metadata key, sorts, optionally truncates to topN, "
            "draws a bar chart in chat.\n"
            "\n"
            "Use to answer 'how many curves of each Name are there?' "
            "without round-tripping the curve list through your context."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "handle": {"type": "string"},
                "groupBy": {
                    "type": "string",
                    "description": "Metadata key to group by (e.g. 'Name').",
                },
                "curveFilter": {"type": "object"},
                "topN": {"type": "integer", "minimum": 1},
                "showOthers": {"type": "boolean", "default": False},
                "sort": {
                    "type": "string",
                    "enum": [
                        "value-desc",
                        "value-asc",
                        "category-asc",
                        "category-desc",
                        "none",
                    ],
                    "default": "value-desc",
                },
                "title": {"type": "string"},
                "categoryLabel": {"type": "string"},
                "valueLabel": {"type": "string"},
                "orientation": {
                    "type": "string",
                    "enum": ["vertical", "horizontal"],
                },
                "height": {"type": "integer"},
            },
            "required": ["handle", "groupBy"],
            "additionalProperties": False,
        },
        handler=_plot_bars_from_buffer,
        side="hybrid",
        visibility_check=js_compute_enabled,
    )
)


# ---- plot (declarative; inline data) -------------------------------------


async def _plot(args: dict[str, Any], ctx: ToolCtx) -> Any:
    return await _call("plot", args, ctx)


registry.register(
    ToolSpec(
        name="plot",
        description=(
            "Render a chart from inline data (no buffer involved). Use "
            "this only for SMALL data the user dictated or that you "
            "computed inline (e.g. five KPIs from a single tool result). "
            "For anything bigger, fetch into a buffer first.\n"
            "\n"
            "kind is one of: xy, bars, heatmap, pie, radar, chartjs. "
            "spec follows the matching schema (see the plot-spec.ts in "
            "the frontend for full structure). 'chartjs' is the escape "
            "hatch for declarative Chart.js configs not covered by the "
            "predefined kinds — function fields are stripped at the "
            "boundary."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["xy", "bars", "heatmap", "pie", "radar", "chartjs"],
                },
                "spec": {"type": "object"},
            },
            "required": ["kind", "spec"],
            "additionalProperties": False,
        },
        handler=_plot,
        side="hybrid",
        visibility_check=js_compute_enabled,
    )
)


# ---- buffer_eval (the most powerful tool) --------------------------------


async def _buffer_eval(args: dict[str, Any], ctx: ToolCtx) -> Any:
    return await _call("buffer_eval", args, ctx, timeout_ms=60_000)


registry.register(
    ToolSpec(
        name="buffer_eval",
        description=(
            "Run JavaScript against a buffer's data in a sandboxed Worker. "
            "THIS IS THE MOST POWERFUL TOOL — use it when no declarative "
            "tool fits (custom aggregates, derivatives, FFTs, magnitude = "
            "sqrt(re² + im²), arbitrary predicates).\n"
            "\n"
            "Sandbox guarantees:\n"
            "  • NO network. fetch / XMLHttpRequest / WebSocket / "
            "    EventSource / importScripts / sendBeacon are all undefined.\n"
            "  • NO localStorage (Workers don't have it).\n"
            "  • NO ambient `self` exposed to the function — only the four "
            "    named arguments.\n"
            "  • Timeout (default 5 s, max 30 s) terminates the worker.\n"
            "\n"
            "Your code is the body of `function(buffer, draw, log, helpers) "
            "{ ... }`. The `buffer` argument is the buffer's `data` field "
            "directly — for a curves buffer that's typically `{curves: "
            "[...]}` (sometimes a bare array). `draw.{xy, bars, heatmap, "
            "pie, radar, chartjs, text}(spec)` emits inline plots/text. "
            "`log(...)` captures up to 200 log lines (1 KB each).\n"
            "\n"
            "Helpers (always available):\n"
            "  • helpers.curves(buffer) -> Array — unwraps `{curves: "
            "[...]}` OR `[...]`. ALWAYS use this to get the curves array.\n"
            "  • helpers.meta(curve, key) -> string|null — read metadata "
            "value by key.\n"
            "  • helpers.series(curve, name) -> number[]|null — read a "
            "series' values by series name.\n"
            "  • helpers.filterCurves(input, filter) -> Array — `input` "
            "may be a curves array OR a buffer object; `filter` may be "
            "{metadataKey: exactValue} OR a predicate function "
            "(Array.filter-compatible). Use the predicate form for regex "
            "/ multi-key / negation.\n"
            "\n"
            "For anything not covered by helpers, use ordinary Array / "
            "Object methods on `helpers.curves(buffer)` directly.\n"
            "\n"
            "Return any small JSON value — it's serialised and clamped at "
            "50 KB. Don't return the whole buffer (it'll get refused).\n"
            "\n"
            "Anti-pattern: looping with buffer_eval to inspect tiny pieces. "
            "After at most ONE inspection, commit to a draw call or a "
            "structured return."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "handle": {"type": "string"},
                "code": {
                    "type": "string",
                    "description": "JS function body — receives (buffer, draw, log, helpers).",
                },
                "timeout_ms": {
                    "type": "integer",
                    "minimum": 100,
                    "maximum": 30_000,
                    "default": 5_000,
                },
            },
            "required": ["handle", "code"],
            "additionalProperties": False,
        },
        handler=_buffer_eval,
        side="hybrid",
        visibility_check=js_compute_enabled,
    )
)
