// Sandboxed Web Worker for buffer_eval.
//
// The LLM writes JavaScript that operates on a buffer's contents and pushes
// `draw.xy/bars/heatmap/pie/radar/chartjs/text` commands or returns a
// small JSON result. We run that code inside a Worker and clamp it down:
//
//   1. Network APIs (fetch / XMLHttpRequest / WebSocket / EventSource /
//      sendBeacon / importScripts) are nulled out *before* the user code
//      runs. Workers don't have localStorage at all, so any host-page
//      auth tokens are unreachable from inside the worker even before
//      this step.
//   2. The user code is wrapped in `new Function(...)` — there's no
//      ambient `self` / `globalThis` reference that resolves to the worker
//      scope; the wrapper only sees the four named arguments
//      (buffer, draw, log, helpers).
//   3. A timeout in the main thread terminates the worker if it runs long.
//
// We pass the buffer data via structured-clone postMessage. For typical
// curve buffers (a few MB) this is fine; for very large buffers it'd be
// slower than direct in-main-thread access, but that's the price of
// isolation.

const WORKER_BODY = `
"use strict";

// 1. Lock down network access. Assigning undefined shadows the prototype's
//    method on the WorkerGlobalScope instance.
for (const k of [
  "fetch",
  "XMLHttpRequest",
  "WebSocket",
  "EventSource",
  "importScripts",
  "Request",
  "Response",
]) {
  try { self[k] = undefined; } catch (_) { /* ignore */ }
}
try { if (self.navigator) self.navigator.sendBeacon = undefined; } catch (_) {}

self.onmessage = (e) => {
  const { buffer, code } = e.data;
  const drawCommands = [];
  const logLines = [];

  const draw = {
    xy: (spec) => { drawCommands.push({ kind: "xy", spec }); },
    bars: (spec) => { drawCommands.push({ kind: "bars", spec }); },
    heatmap: (spec) => { drawCommands.push({ kind: "heatmap", spec }); },
    pie: (spec) => { drawCommands.push({ kind: "pie", spec }); },
    radar: (spec) => { drawCommands.push({ kind: "radar", spec }); },
    chartjs: (config, opts) => { drawCommands.push({ kind: "chartjs", spec: { config, ...(opts || {}) } }); },
    text: (markdown) => { drawCommands.push({ kind: "text", markdown: String(markdown) }); },
  };

  function safeStr(a) {
    if (typeof a === "string") return a;
    try { return JSON.stringify(a); } catch (_) { return String(a); }
  }
  function log(...args) {
    if (logLines.length >= 200) return;
    logLines.push(args.map(safeStr).join(" ").slice(0, 1000));
  }

  // Helpers — mirror the ones in lib/buffers.ts so worker code can share
  // semantics with primitives running on the main thread. Both arguments
  // are forgiving:
  //   • curves(): unwraps {curves: [...]} or [...] to the curve array.
  //   • filterCurves(input, filter): input may be an array OR a buffer-
  //     shaped object; filter may be {key: value} OR a predicate function
  //     (Array.filter-compatible).
  function unwrapCurves(input) {
    if (Array.isArray(input)) return input;
    if (input && Array.isArray(input.curves)) return input.curves;
    return [];
  }
  const helpers = {
    meta: (curve, key) => {
      const m = (curve?.metadata || []).find((x) => x.key === key);
      return m ? m.value : null;
    },
    series: (curve, name) => {
      const s = (curve?.series || []).find((x) => x.name === name);
      return s ? (Array.isArray(s.values) ? s.values : null) : null;
    },
    curves: unwrapCurves,
    filterCurves: (input, filter) => {
      const arr = unwrapCurves(input);
      if (typeof filter === "function") return arr.filter(filter);
      if (!filter || !Object.keys(filter).length) return arr;
      return arr.filter((c) => {
        for (const k of Object.keys(filter)) {
          const m = (c?.metadata || []).find((x) => x.key === k);
          if (!m || m.value !== filter[k]) return false;
        }
        return true;
      });
    },
  };

  let result;
  let ok = true;
  let error = null;
  try {
    const fn = new Function("buffer", "draw", "log", "helpers", code);
    result = fn(buffer, draw, log, helpers);
  } catch (err) {
    ok = false;
    error = err && err.stack ? String(err.stack).slice(0, 2000) : String(err);
  }

  // Allow the user's code to return a Promise.
  Promise.resolve(result).then(
    (value) => {
      // Cap the size of the returned JSON (the LLM has a context budget).
      let serialised;
      try {
        serialised = JSON.stringify(value);
      } catch (_) {
        serialised = null;
      }
      let truncated = false;
      if (serialised && serialised.length > 50000) {
        serialised = serialised.slice(0, 50000);
        truncated = true;
      }
      self.postMessage({
        ok,
        error,
        result: serialised && !truncated ? value : serialised,
        truncated,
        drawCommands,
        logLines,
      });
    },
    (err) => {
      self.postMessage({
        ok: false,
        error: String((err && err.message) || err),
        drawCommands,
        logLines,
      });
    },
  );
};
`;

let cachedUrl: string | null = null;

export function getWorkerUrl(): string {
  if (cachedUrl) return cachedUrl;
  const blob = new Blob([WORKER_BODY], { type: "application/javascript" });
  cachedUrl = URL.createObjectURL(blob);
  return cachedUrl;
}

export interface DrawCommand {
  kind: "xy" | "bars" | "heatmap" | "pie" | "radar" | "chartjs" | "text";
  spec?: unknown;
  markdown?: string;
}

export interface EvalOutcome {
  ok: boolean;
  result: unknown;
  error: string | null;
  truncated: boolean;
  drawCommands: DrawCommand[];
  logLines: string[];
  elapsed_ms: number;
}

export function runEvalWorker(
  buffer: unknown,
  code: string,
  timeoutMs: number,
): Promise<EvalOutcome> {
  return new Promise((resolve) => {
    const url = getWorkerUrl();
    const worker = new Worker(url);
    const started = performance.now();
    let settled = false;
    const settle = (out: EvalOutcome) => {
      if (settled) return;
      settled = true;
      try {
        worker.terminate();
      } catch {
        /* ignore */
      }
      resolve(out);
    };

    const timer = setTimeout(() => {
      settle({
        ok: false,
        result: null,
        error: `eval timed out after ${timeoutMs}ms`,
        truncated: false,
        drawCommands: [],
        logLines: [],
        elapsed_ms: timeoutMs,
      });
    }, timeoutMs);

    worker.onmessage = (e: MessageEvent) => {
      clearTimeout(timer);
      const data: any = e.data || {};
      settle({
        ok: !!data.ok,
        result: data.result ?? null,
        error: data.error ?? null,
        truncated: !!data.truncated,
        drawCommands: Array.isArray(data.drawCommands) ? data.drawCommands : [],
        logLines: Array.isArray(data.logLines) ? data.logLines : [],
        elapsed_ms: Math.round(performance.now() - started),
      });
    };

    worker.onerror = (e: ErrorEvent) => {
      clearTimeout(timer);
      settle({
        ok: false,
        result: null,
        error: e.message || "worker crashed",
        truncated: false,
        drawCommands: [],
        logLines: [],
        elapsed_ms: Math.round(performance.now() - started),
      });
    };

    worker.postMessage({ buffer, code });
  });
}
