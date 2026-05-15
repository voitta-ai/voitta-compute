// Buffer-aware primitives.
//
// These compose with the primitives in `primitives.ts`. They live in
// a separate file mostly for readability — the registration call still
// happens at module-load time via the side-effect import in `widget.tsx`.
//
// Two groups:
//
//   • Buffer management — list / get_summary / delete / delete_keys /
//     clear / query_curves.
//   • Plot rendering — `plot` (declarative) + `plot_xy_from_buffer` /
//     `plot_bars_from_buffer` (curves-aware sugar) + `buffer_eval`
//     (sandboxed Worker, the most powerful tool).
//
// Data lands in a buffer via `buffer_eval` (returning a value into a
// buffer) or via a provider tool that opts in to writing one.

import { getBackendOrigin, PrimitiveError, registerPrimitive } from "./bridge";
import {
  bufferClear,
  bufferDelete,
  bufferDeleteKeys,
  bufferGet,
  bufferList,
  bufferTotals,
  curveSeries,
  curvesPayload,
  filterCurves,
  getMetaValue,
} from "./buffers";
import { runEvalWorker } from "./eval-worker";
import { log } from "./logger";
import type { PlotSpec, RichOutput } from "./plot-spec";
import type { FlowDefinition } from "./flow-types";

// ---- buffer_list ----------------------------------------------------------

registerPrimitive("buffer_list", async () => ({
  totals: bufferTotals(),
  buffers: bufferList(),
}));

// ---- buffer_get_summary ---------------------------------------------------

registerPrimitive("buffer_get_summary", async (rawArgs) => {
  const args = rawArgs as { handle?: string };
  if (!args.handle) throw new PrimitiveError("invalid_args", "handle required");
  const rec = bufferGet(args.handle);
  if (!rec) throw new PrimitiveError("not_found", `no buffer ${args.handle}`);
  return {
    handle: rec.handle,
    kind: rec.kind,
    bytes: rec.bytes,
    summary: rec.summary,
    created_at: rec.createdAt,
    meta: rec.meta,
  };
});

// ---- buffer_delete --------------------------------------------------------

registerPrimitive("buffer_delete", async (rawArgs) => {
  const args = rawArgs as { handle?: string };
  if (!args.handle) throw new PrimitiveError("invalid_args", "handle required");
  return { deleted: bufferDelete(args.handle) };
});

// ---- buffer_delete_keys (partial free) ------------------------------------

registerPrimitive("buffer_delete_keys", async (rawArgs) => {
  const args = rawArgs as { handle?: string; paths?: string[] };
  if (!args.handle) throw new PrimitiveError("invalid_args", "handle required");
  if (!Array.isArray(args.paths) || !args.paths.length) {
    throw new PrimitiveError("invalid_args", "paths[] required");
  }
  return bufferDeleteKeys(args.handle, args.paths);
});

// ---- buffer_clear ---------------------------------------------------------

registerPrimitive("buffer_clear", async () => bufferClear());

// ---- buffer_query_curves --------------------------------------------------
// Filter a `curves` buffer by metadata equality, return small projection
// rows. Curves' `series.values[]` are NEVER included — that's what plot /
// eval primitives are for.

interface QueryCurvesArgs {
  handle?: string;
  filter?: Record<string, string>;
  project?: string[];
  limit?: number;
}

registerPrimitive("buffer_query_curves", async (rawArgs) => {
  const args = rawArgs as QueryCurvesArgs;
  if (!args.handle) throw new PrimitiveError("invalid_args", "handle required");
  const rec = bufferGet(args.handle);
  if (!rec) throw new PrimitiveError("not_found", `no buffer ${args.handle}`);
  const all = curvesPayload(rec);
  const matched = filterCurves(all, args.filter || {});
  const limit = Math.max(1, Math.min(args.limit ?? 200, 2000));
  const rows: Array<Record<string, unknown>> = [];
  for (let i = 0; i < matched.length && rows.length < limit; i++) {
    const c = matched[i];
    const row: Record<string, unknown> = { idx: i, id: c.id, name: c.name };
    if (args.project && args.project.length) {
      for (const k of args.project) row[k] = getMetaValue(c, k);
    } else {
      for (const m of c.metadata || []) row[m.key] = m.value;
    }
    row.seriesNames = (c.series || []).map((s: any) => s.name);
    rows.push(row);
  }
  return { matched: matched.length, returned: rows.length, curves: rows };
});

// ---- plot rendering -------------------------------------------------------
//
// The `plot` primitive validates+normalises a spec and pushes it onto the
// chat-pane's "rich output" callback. The actual Chart.js render happens in
// the `PlotCard` component so plots survive UI re-renders.

let richOutputSink: ((output: RichOutput) => void) | null = null;

export function setRichOutputSink(
  fn: ((output: RichOutput) => void) | null,
): void {
  richOutputSink = fn;
}

let plotIdCounter = 0;
function newPlotId(): string {
  plotIdCounter += 1;
  return `plot_${plotIdCounter.toString(36)}`;
}

function emitPlot(spec: PlotSpec): { plot_id: string } {
  if (!richOutputSink) {
    throw new PrimitiveError(
      "no_chat_pane",
      "rich output sink not registered",
    );
  }
  const plot_id = newPlotId();
  richOutputSink({ kind: "plot", plot: spec, plot_id });
  return { plot_id };
}

function emitText(markdown: string): void {
  if (!richOutputSink) {
    throw new PrimitiveError(
      "no_chat_pane",
      "rich output sink not registered",
    );
  }
  richOutputSink({ kind: "text", markdown });
}

function emitLog(lines: string[]): void {
  if (!richOutputSink || !lines.length) return;
  richOutputSink({ kind: "log", log_lines: lines });
}

// ---- per-kind validators (no deep copy — Chart.js sees the same object) --

function normaliseXy(spec: any) {
  if (!Array.isArray(spec?.traces) || spec.traces.length === 0) {
    throw new PrimitiveError("invalid_spec", "xy plot needs at least one trace");
  }
  let totalPoints = 0;
  for (const t of spec.traces) {
    if (!Array.isArray(t.x) || !Array.isArray(t.y)) {
      throw new PrimitiveError("invalid_spec", "each trace needs x[] and y[]");
    }
    totalPoints += Math.min(t.x.length, t.y.length);
  }
  return { traceCount: spec.traces.length, pointCount: totalPoints };
}

function normaliseBars(spec: any) {
  if (!Array.isArray(spec?.categories) || !spec.categories.length) {
    throw new PrimitiveError("invalid_spec", "bars plot needs categories[]");
  }
  if (!Array.isArray(spec?.series) || !spec.series.length) {
    throw new PrimitiveError(
      "invalid_spec",
      "bars plot needs at least one series",
    );
  }
  for (const s of spec.series) {
    if (!Array.isArray(s.values) || s.values.length !== spec.categories.length) {
      throw new PrimitiveError(
        "invalid_spec",
        "series.values length must match categories length",
      );
    }
  }
  return {
    categoryCount: spec.categories.length,
    seriesCount: spec.series.length,
    barCount: spec.categories.length * spec.series.length,
  };
}

function normaliseHeatmap(spec: any) {
  if (
    !Array.isArray(spec?.x) ||
    !Array.isArray(spec?.y) ||
    !Array.isArray(spec?.z)
  ) {
    throw new PrimitiveError("invalid_spec", "heatmap needs x[], y[], z[][]");
  }
  if (spec.z.length !== spec.y.length) {
    throw new PrimitiveError("invalid_spec", "z must have y.length rows");
  }
  for (const row of spec.z) {
    if (!Array.isArray(row) || row.length !== spec.x.length) {
      throw new PrimitiveError(
        "invalid_spec",
        "every z row must have x.length cells",
      );
    }
  }
  return {
    xCount: spec.x.length,
    yCount: spec.y.length,
    cellCount: spec.x.length * spec.y.length,
  };
}

function normalisePie(spec: any) {
  if (!Array.isArray(spec?.slices) || !spec.slices.length) {
    throw new PrimitiveError("invalid_spec", "pie plot needs at least one slice");
  }
  for (const s of spec.slices) {
    if (typeof s.value !== "number") {
      throw new PrimitiveError(
        "invalid_spec",
        "each slice needs a numeric value",
      );
    }
  }
  return { sliceCount: spec.slices.length };
}

function normaliseRadar(spec: any) {
  if (!Array.isArray(spec?.axes) || !spec.axes.length) {
    throw new PrimitiveError("invalid_spec", "radar plot needs axes[]");
  }
  if (!Array.isArray(spec?.series) || !spec.series.length) {
    throw new PrimitiveError(
      "invalid_spec",
      "radar plot needs at least one series",
    );
  }
  for (const s of spec.series) {
    if (!Array.isArray(s.values) || s.values.length !== spec.axes.length) {
      throw new PrimitiveError(
        "invalid_spec",
        "series.values length must match axes length",
      );
    }
  }
  return {
    axisCount: spec.axes.length,
    seriesCount: spec.series.length,
  };
}

function normaliseChartJs(spec: any) {
  if (!spec?.config || typeof spec.config !== "object") {
    throw new PrimitiveError(
      "invalid_spec",
      "chartjs plot needs a `config` object",
    );
  }
  const cfg = spec.config;
  if (typeof cfg.type !== "string") {
    throw new PrimitiveError(
      "invalid_spec",
      "chartjs config.type must be a string",
    );
  }
  if (!cfg.data || typeof cfg.data !== "object") {
    throw new PrimitiveError(
      "invalid_spec",
      "chartjs config.data must be an object",
    );
  }
  return { type: cfg.type };
}

const ALL_KINDS = new Set(["xy", "bars", "heatmap", "pie", "radar", "chartjs"]);

const NORMS: Record<string, (s: any) => unknown> = {
  xy: normaliseXy,
  bars: normaliseBars,
  heatmap: normaliseHeatmap,
  pie: normalisePie,
  radar: normaliseRadar,
  chartjs: normaliseChartJs,
};

// ---- plot (declarative — model emits the spec inline) -------------------

registerPrimitive("plot", async (rawArgs) => {
  const args = rawArgs as { kind?: string; spec?: any };
  const kind = args.kind;
  if (!kind || !ALL_KINDS.has(kind)) {
    throw new PrimitiveError("invalid_kind", `unknown plot kind ${kind}`);
  }
  const spec = { ...(args.spec || {}), kind };
  const stats = NORMS[kind](spec);
  const { plot_id } = emitPlot(spec as PlotSpec);
  return { plot_id, kind, ...(stats as Record<string, unknown>) };
});

// ---- plot_xy_from_buffer (curves-aware: filter by metadata, plot) -------

interface PlotXyFromBufferArgs {
  handle?: string;
  curveFilter?: Record<string, string>;
  xSeries?: string;
  ySeries?: string;
  labelFromMetadata?: string;
  maxTraces?: number;
  // pass-throughs for plot_xy options
  title?: string;
  xAxis?: any;
  yAxisLeft?: any;
  yAxisRight?: any;
  defaultType?: "line" | "scatter" | "area";
  legend?: any;
  height?: number;
}

registerPrimitive("plot_xy_from_buffer", async (rawArgs) => {
  const args = rawArgs as PlotXyFromBufferArgs;
  if (!args.handle || !args.xSeries || !args.ySeries) {
    throw new PrimitiveError(
      "invalid_args",
      "handle, xSeries, ySeries required",
    );
  }
  const rec = bufferGet(args.handle);
  if (!rec) throw new PrimitiveError("not_found", `no buffer ${args.handle}`);
  const all = curvesPayload(rec);
  const matched = filterCurves(all, args.curveFilter || {});
  const maxTraces = Math.max(1, Math.min(args.maxTraces ?? 50, 200));
  const traces: any[] = [];
  let dropped = 0;

  for (const c of matched) {
    if (traces.length >= maxTraces) {
      dropped = matched.length - traces.length;
      break;
    }
    const x = curveSeries(c, args.xSeries);
    const y = curveSeries(c, args.ySeries);
    if (!x || !y) continue;
    const n = Math.min(x.length, y.length);
    traces.push({
      label: args.labelFromMetadata
        ? getMetaValue(c, args.labelFromMetadata) || c.name
        : c.name,
      x: x.slice(0, n),
      y: y.slice(0, n),
      type: args.defaultType || "line",
    });
  }
  if (!traces.length) {
    throw new PrimitiveError(
      "no_traces",
      `no curves matched filter ${JSON.stringify(args.curveFilter || {})} with series ${args.xSeries}/${args.ySeries}`,
    );
  }
  const spec: any = {
    kind: "xy",
    title: args.title,
    traces,
    xAxis: args.xAxis,
    yAxisLeft: args.yAxisLeft,
    yAxisRight: args.yAxisRight,
    defaultType: args.defaultType,
    legend: args.legend,
    height: args.height,
  };
  const stats = normaliseXy(spec);
  const { plot_id } = emitPlot(spec);
  return { plot_id, kind: "xy", ...stats, dropped };
});

// ---- plot_bars_from_buffer (group-by metadata histogram) ------------------

interface PlotBarsFromBufferArgs {
  handle?: string;
  groupBy?: string;
  curveFilter?: Record<string, string>;
  topN?: number;
  showOthers?: boolean;
  sort?:
    | "value-desc"
    | "value-asc"
    | "category-asc"
    | "category-desc"
    | "none";
  title?: string;
  categoryLabel?: string;
  valueLabel?: string;
  orientation?: "vertical" | "horizontal";
  height?: number;
}

registerPrimitive("plot_bars_from_buffer", async (rawArgs) => {
  const args = rawArgs as PlotBarsFromBufferArgs;
  if (!args.handle || !args.groupBy) {
    throw new PrimitiveError("invalid_args", "handle and groupBy required");
  }
  const rec = bufferGet(args.handle);
  if (!rec) throw new PrimitiveError("not_found", `no buffer ${args.handle}`);
  const all = curvesPayload(rec);
  const matched = filterCurves(all, args.curveFilter || {});
  const counts: Record<string, number> = {};
  for (const c of matched) {
    const v = getMetaValue(c, args.groupBy) ?? "(missing)";
    counts[v] = (counts[v] || 0) + 1;
  }
  let entries = Object.entries(counts);
  const sort = args.sort ?? "value-desc";
  if (sort === "value-desc") entries.sort((a, b) => b[1] - a[1]);
  else if (sort === "value-asc") entries.sort((a, b) => a[1] - b[1]);
  else if (sort === "category-asc") entries.sort((a, b) => a[0].localeCompare(b[0]));
  else if (sort === "category-desc") entries.sort((a, b) => b[0].localeCompare(a[0]));

  let truncated = false;
  if (args.topN && entries.length > args.topN) {
    const head = entries.slice(0, args.topN);
    if (args.showOthers) {
      const others = entries.slice(args.topN).reduce((a, [, v]) => a + v, 0);
      entries = [
        ...head,
        [`Others (${entries.length - args.topN})`, others],
      ];
    } else {
      entries = head;
    }
    truncated = true;
  }

  const spec: any = {
    kind: "bars",
    title: args.title,
    categories: entries.map(([k]) => k),
    series: [{ label: "count", values: entries.map(([, v]) => v) }],
    orientation: args.orientation || "vertical",
    colorByCategory: true,
    showValueLabels: true,
    categoryLabel: args.categoryLabel || args.groupBy,
    valueLabel: args.valueLabel || "count",
    height: args.height,
  };
  normaliseBars(spec);
  const { plot_id } = emitPlot(spec);
  return {
    plot_id,
    kind: "bars",
    matched: matched.length,
    distinctValues: entries.length,
    truncated,
  };
});

// ---- buffer_eval (sandboxed Web Worker) -----------------------------------

interface BufferEvalArgs {
  handle?: string;
  code?: string;
  timeout_ms?: number;
}

registerPrimitive("buffer_eval", async (rawArgs) => {
  const args = rawArgs as BufferEvalArgs;
  if (!args.handle) throw new PrimitiveError("invalid_args", "handle required");
  if (!args.code || typeof args.code !== "string") {
    throw new PrimitiveError("invalid_args", "code (string) required");
  }
  const rec = bufferGet(args.handle);
  if (!rec) {
    const available = bufferList().map((b) => ({
      handle: b.handle,
      kind: b.kind,
      bytes: b.bytes,
    }));
    throw new PrimitiveError(
      "not_found",
      `no buffer named '${args.handle}'. Buffer handles look like 'buf_XXXX' and are returned by buffer_eval (or any provider tool that opts in to writing a buffer). Call that FIRST, then pass the returned handle here.`,
      { available_handles: available },
    );
  }
  const timeout = Math.max(100, Math.min(args.timeout_ms ?? 5000, 30000));
  const out = await runEvalWorker(rec.data, args.code, timeout);
  if (!out.ok) {
    log.error("buffer_eval", out.error || "eval failed", {
      handle: args.handle,
      elapsed_ms: out.elapsed_ms,
      code_preview: (args.code || "").slice(0, 200),
    });
  } else {
    log.info("buffer_eval", "ok", {
      handle: args.handle,
      elapsed_ms: out.elapsed_ms,
      drew: out.drawCommands.length,
      log_lines: out.logLines.length,
    });
  }

  let drewPlots = 0;
  for (const cmd of out.drawCommands) {
    if (cmd.kind === "text" && typeof cmd.markdown === "string") {
      emitText(cmd.markdown);
      continue;
    }
    if (!cmd.spec || !cmd.kind || !NORMS[cmd.kind]) {
      out.logLines.push(`[draw.${cmd.kind ?? "?"}: unknown kind]`);
      continue;
    }
    const spec: any = { ...(cmd.spec as any), kind: cmd.kind };
    try {
      NORMS[cmd.kind](spec);
      emitPlot(spec as PlotSpec);
      drewPlots += 1;
    } catch (e) {
      out.logLines.push(
        `[draw.${cmd.kind} rejected: ${(e as Error).message}]`,
      );
    }
  }
  if (out.logLines.length) emitLog(out.logLines);

  return {
    ok: out.ok,
    error: out.error,
    result: out.result,
    truncated: out.truncated,
    drew_plots: drewPlots,
    log_lines: out.logLines.length,
    elapsed_ms: out.elapsed_ms,
  };
});

// ---- show_report (HoloViz Panel iframe pane) -----------------------------
//
// Distinct from the rich-output sink: rich outputs go INLINE in the chat
// stream as TurnItems. The report pane is a separate persistent overlay
// to the LEFT of the chat drawer with its own close button.
//
// Args:
//   path        — backend-relative path (resolved against `getBackendOrigin()`)
//   report_id   — string id; surfaced in the pane title fallback
//   title       — optional friendly title for the pane header

interface ReportInfo {
  url: string;
  report_id: string;
  title?: string;
}

let reportSink: ((info: ReportInfo | null) => void) | null = null;

/** Wired by `<ChatPane>`. The hook receives the active report info, or
 * `null` when the user closes the pane externally (we don't currently
 * call it with `null`, but the type allows it for future cleanup paths). */
export function setReportSink(
  fn: ((info: ReportInfo | null) => void) | null,
): void {
  reportSink = fn;
}

// ---- show_flow_report (mermaid-rendered flow chart, no iframe) ----------
//
// Sibling to show_report. The server has already built the JSON flow
// definition; we just mount it in the report slot. No URL/path — the
// definition arrives in-band.

export interface FlowReportInfo {
  report_id: string;
  title?: string;
  render_id?: string;
  definition: FlowDefinition;
}

let flowReportSink: ((info: FlowReportInfo | null) => void) | null = null;

/** Wired by `<ChatPane>`. The sink replaces any currently-visible
 *  report (holoviz or flow) with this flow definition. */
export function setFlowReportSink(
  fn: ((info: FlowReportInfo | null) => void) | null,
): void {
  flowReportSink = fn;
}

registerPrimitive("show_flow_report", async (rawArgs) => {
  const args = rawArgs as {
    definition?: FlowDefinition;
    report_id?: string;
    title?: string;
    render_id?: string;
  };
  if (!args.definition || typeof args.definition !== "object") {
    throw new PrimitiveError("invalid_args", "definition required");
  }
  if (!flowReportSink) {
    throw new PrimitiveError(
      "no_chat_pane",
      "report sink not registered — open the chat pane first",
    );
  }
  flowReportSink({
    report_id: String(args.report_id || ""),
    title: args.title ? String(args.title) : undefined,
    render_id: args.render_id ? String(args.render_id) : undefined,
    definition: args.definition,
  });
  return { ok: true, opened: true };
});


registerPrimitive("show_report", async (rawArgs) => {
  const args = rawArgs as {
    path?: string;
    url?: string;
    report_id?: string;
    title?: string;
  };
  const path = String(args.path || args.url || "").trim();
  if (!path) throw new PrimitiveError("invalid_args", "path required");
  if (!reportSink) {
    throw new PrimitiveError(
      "no_chat_pane",
      "report sink not registered — open the chat pane first",
    );
  }
  // Resolve the path against the FastAPI backend origin (NOT the host
  // page's origin). The backend mounts a Panel app at /panel/reports
  // (websocket-backed); see show_holoviz_report tool for the URL shape.
  let url: string;
  if (/^https?:\/\//i.test(path)) {
    url = path;
  } else {
    const origin = getBackendOrigin();
    if (!origin) {
      throw new PrimitiveError(
        "no_backend_origin",
        "bridge not started; cannot resolve relative report path",
      );
    }
    url = origin.replace(/\/$/, "") + (path.startsWith("/") ? path : "/" + path);
  }
  reportSink({
    url,
    report_id: String(args.report_id || ""),
    title: args.title ? String(args.title) : undefined,
  });
  return { ok: true, opened: true, url };
});
