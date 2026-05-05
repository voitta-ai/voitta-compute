// Generic browser primitives the server can dispatch via the bridge.
// Provider-agnostic; provider-specific primitives live with their
// provider in `app.tools.providers`.

import { PrimitiveError, registerPrimitive } from "./bridge";
import { getActiveReportIframe, getActiveReportInfo } from "./report-iframe";

const DOM_CAP = 200_000;

// ---- get_url ---------------------------------------------------------------

registerPrimitive("get_url", async () => ({
  href: location.href,
  pathname: location.pathname,
  search: location.search,
  hash: location.hash,
  title: document.title,
}));

// ---- read_selection --------------------------------------------------------

registerPrimitive("read_selection", async () => {
  const text = window.getSelection()?.toString() ?? "";
  return { text };
});

// ---- read_dom --------------------------------------------------------------

registerPrimitive("read_dom", async (args) => {
  const selector = String(args.selector ?? "");
  const kind = args.kind === "html" ? "html" : "text";
  if (!selector) throw new PrimitiveError("invalid_args", "selector is required");
  let el: Element | null;
  try {
    el = document.querySelector(selector);
  } catch (err) {
    throw new PrimitiveError("invalid_selector", String((err as Error).message));
  }
  if (!el) throw new PrimitiveError("not_found", `no element matches ${selector}`);
  const value =
    kind === "html"
      ? el.outerHTML
      : (el as HTMLElement).innerText ?? el.textContent ?? "";
  if (value.length > DOM_CAP) {
    throw new PrimitiveError("too_large", `${value.length} > ${DOM_CAP}`, {
      size: value.length,
    });
  }
  return { value, kind };
});

// ─────────────────────────────────────────────────────────────────────────
// screenshot_report — rasterise the active HoloViz Panel report iframe.
// Returns a base64 dataURL covering the entire scrollHeight, not just
// the visible viewport. The actual rasterisation runs INSIDE the iframe
// (via html2canvas loaded by the Panel shim) so it has same-origin
// access to all canvases. We just shuttle a postMessage round-trip.
// ─────────────────────────────────────────────────────────────────────────

interface ScreenshotArgs {
  scale?: number;
  quality?: number;
  format?: "webp" | "png";
  timeout_ms?: number;
}

interface ScreenshotResponse {
  type: "voitta_report_event";
  kind: "screenshot_response";
  requestId: string;
  ok: boolean;
  message?: string;
  dataUrl?: string;
  width?: number;
  height?: number;
  full_width?: number;
  full_height?: number;
  scale?: number;
  format?: string;
}

let screenshotCounter = 0;

registerPrimitive("screenshot_report", async (rawArgs) => {
  const args = (rawArgs || {}) as ScreenshotArgs;
  const iframe = getActiveReportIframe();
  if (!iframe || !iframe.contentWindow) {
    throw new PrimitiveError("no_report", "no report is currently open");
  }
  const info = getActiveReportInfo();
  // Block screenshots in edit mode: html2canvas chokes on Panel's
  // editable template CSS (notably the ``color-mix()`` box-shadow on
  // ``.muuri-grid-item``), throwing "unsupported color function". The
  // edit-mode handles also visually clutter the captured image. Exit
  // edit mode first, then retry. iframe.src is preferred over the
  // cached info.url because ReportPane mutates iframe.src on toggle
  // without re-pushing info.
  const url = iframe.src || info?.url || "";
  if (/[?&]editable=true(?:&|$|#)/.test(url)) {
    throw new PrimitiveError(
      "edit_mode",
      "screenshot_report does not work while the report is in edit mode (the editable template uses CSS that html2canvas cannot rasterise). Ask the user to leave edit mode, then retry.",
    );
  }
  const requestId = `ss_${Date.now().toString(36)}_${++screenshotCounter}`;
  const scale = typeof args.scale === "number" ? args.scale : 1;
  const quality = typeof args.quality === "number" ? args.quality : 0.85;
  const format = args.format === "png" ? "png" : "webp";
  const timeout = typeof args.timeout_ms === "number" ? args.timeout_ms : 60_000;

  return await new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      window.removeEventListener("message", onMessage);
      reject(
        new PrimitiveError(
          "timeout",
          `report screenshot did not return within ${timeout}ms`,
        ),
      );
    }, timeout);

    function onMessage(e: MessageEvent) {
      const data = e.data as ScreenshotResponse | null;
      if (
        !data ||
        typeof data !== "object" ||
        data.type !== "voitta_report_event" ||
        data.kind !== "screenshot_response" ||
        data.requestId !== requestId
      ) {
        return;
      }
      // Only accept from our own iframe.
      if (e.source !== iframe!.contentWindow) return;
      clearTimeout(timer);
      window.removeEventListener("message", onMessage);
      if (!data.ok) {
        reject(
          new PrimitiveError(
            "screenshot_failed",
            data.message || "iframe screenshot returned ok=false",
          ),
        );
        return;
      }
      resolve({
        ok: true,
        data_url: data.dataUrl,
        width: data.width,
        height: data.height,
        full_width: data.full_width,
        full_height: data.full_height,
        scale: data.scale,
        format: data.format,
        report: info
          ? { report_id: info.report_id, url: info.url, title: info.title }
          : null,
      });
    }
    window.addEventListener("message", onMessage);
    iframe.contentWindow!.postMessage(
      {
        voittaAction: "screenshot",
        requestId,
        scale,
        quality,
        format,
      },
      "*",
    );
  });
});

// ─────────────────────────────────────────────────────────────────────────
// get_report_edits — read the live edit state of the report iframe.
//
// 100% client-side. The iframe holds the truth (Muuri grid + Bokeh
// document + our shim's selection state); the shim posts back a
// snapshot. Three return shapes:
//
//   { status: "no_active_report", message }
//     — no iframe mounted, or the URL doesn't have ?editable=true
//
//   { status: "active_no_edits", report_id, message }
//     — iframe is in edit mode but every card is at default
//       width (100%) / height (Bokeh-natural) / visible, original
//       order, and nothing is selected
//
//   { status: "active", report_id, elements, selected_id, order_changed }
//     — anything else; `elements[]` carries name/title/index/width_pct/
//       height_px/visible/selected for each card. `name` is the Python
//       `name=` argument the script set (null when unset — the LLM is
//       expected to set names on each main component for stable refs).
// ─────────────────────────────────────────────────────────────────────────

interface EditsElement {
  index: number;
  id: string;
  name: string | null;
  title: string | null;
  width_pct: number;
  height_px: number | null;
  visible: boolean;
  selected: boolean;
}

interface EditsResponse {
  type: "voitta_report_event";
  kind: "edits_response";
  requestId: string;
  ok: boolean;
  message?: string;
  elements?: EditsElement[];
  selected_id?: string | null;
  order_changed?: boolean;
}

let editsCounter = 0;

registerPrimitive("get_report_edits", async () => {
  const iframe = getActiveReportIframe();
  if (!iframe || !iframe.contentWindow) {
    return {
      status: "no_active_report",
      message: "no report is currently open in the iframe pane",
    };
  }
  const info = getActiveReportInfo();
  // Live ``iframe.src`` is the source of truth: ReportPane updates it
  // in-place when the user toggles edit mode, but the cached
  // ``info.url`` (set on mount) doesn't get re-pushed. Reading the
  // iframe directly catches the toggle without needing a ReportPane
  // change.
  const url = iframe.src || info?.url || "";
  if (!/[?&]editable=true(?:&|$|#)/.test(url)) {
    return {
      status: "no_active_report",
      message:
        "the active report is not in edit mode (open the report and toggle the edit affordance)",
    };
  }

  const requestId = `ge_${Date.now().toString(36)}_${++editsCounter}`;
  const response: EditsResponse = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      window.removeEventListener("message", onMessage);
      reject(
        new PrimitiveError(
          "timeout",
          "report iframe did not respond to getEdits within 5s",
        ),
      );
    }, 5_000);
    function onMessage(e: MessageEvent) {
      const data = e.data as EditsResponse | null;
      if (
        !data ||
        typeof data !== "object" ||
        data.type !== "voitta_report_event" ||
        data.kind !== "edits_response" ||
        data.requestId !== requestId
      ) {
        return;
      }
      if (e.source !== iframe!.contentWindow) return;
      clearTimeout(timer);
      window.removeEventListener("message", onMessage);
      resolve(data);
    }
    window.addEventListener("message", onMessage);
    iframe.contentWindow!.postMessage(
      { voittaAction: "getEdits", requestId },
      "*",
    );
  });

  if (!response.ok) {
    return {
      status: "no_active_report",
      message:
        response.message ||
        "report iframe responded but reported no editable grid",
    };
  }

  const elements = response.elements || [];
  const reportId = info?.report_id ?? null;
  const selectedId = response.selected_id ?? null;
  const orderChanged = !!response.order_changed;

  const anyResize = elements.some(
    (el) => el.width_pct !== 100 || el.height_px != null,
  );
  const anyHidden = elements.some((el) => !el.visible);
  const anySelection = selectedId != null;

  if (!anyResize && !anyHidden && !anySelection && !orderChanged) {
    return {
      status: "active_no_edits",
      report_id: reportId,
      message:
        "report is open in edit mode but the user hasn't moved, resized, hidden, or selected anything yet",
    };
  }

  return {
    status: "active",
    report_id: reportId,
    elements,
    selected_id: selectedId,
    order_changed: orderChanged,
  };
});

// ─────────────────────────────────────────────────────────────────────────
// CLI back-channel primitives — invoked by the FastAPI `/cli/*` routes
// from local automation (Claude Code, curl, scripts). Deliberately NOT
// exposed to the in-pane LLM (no ToolSpec wraps them). They give the
// developer a way to drive the bookmarked page from outside the chat UI.
// ─────────────────────────────────────────────────────────────────────────

// Single-round-trip page dump: URL + title + full outerHTML. We use a
// dedicated primitive (rather than chaining get_url + read_dom) because
// `read_dom` enforces a 200 KB cap suited to LLM consumption. Real-world
// pages routinely exceed that, and Claude Code handles big payloads
// fine, so this primitive is uncapped.
registerPrimitive("get_page_dump", async () => ({
  url: location.href,
  title: document.title,
  pathname: location.pathname,
  search: location.search,
  hash: location.hash,
  html: document.documentElement?.outerHTML ?? "",
  user_agent: navigator.userAgent,
  ts: Date.now(),
}));

// JSON-safe serialiser. Faithfully encodes things JSON.stringify can't:
// undefined, BigInt, Symbol, functions, Errors, DOM nodes, Map/Set,
// Date, RegExp, ArrayBuffer/TypedArray, and cyclic graphs. Each
// non-plain value is wrapped in `{__type, ...}` so the consumer can
// reconstruct intent. Plain JSON values pass through untouched.
function _serialize(v: unknown): unknown {
  const seen = new WeakSet<object>();
  function walk(x: unknown): unknown {
    if (x === undefined) return { __type: "undefined" };
    if (x === null) return null;
    const t = typeof x;
    if (t === "string" || t === "boolean") return x;
    if (t === "number") {
      const n = x as number;
      if (!Number.isFinite(n)) return { __type: "Number", value: String(n) };
      return n;
    }
    if (t === "bigint") return { __type: "BigInt", value: String(x) };
    if (t === "symbol") {
      const s = x as symbol;
      return { __type: "Symbol", description: s.description ?? null };
    }
    if (t === "function") {
      const fn = x as { name?: string; toString(): string };
      let src = "";
      try {
        src = fn.toString();
      } catch {
        src = "[unsourceable]";
      }
      return {
        __type: "Function",
        name: fn.name || "(anonymous)",
        source: src.slice(0, 4096),
      };
    }
    if (t !== "object") return String(x);

    const o = x as object;
    if (seen.has(o)) return { __type: "Cycle" };
    seen.add(o);

    if (typeof Element !== "undefined" && o instanceof Element) {
      const el = o as Element;
      let outer = "";
      try {
        outer = el.outerHTML.slice(0, 8192);
      } catch {
        /* some shadow nodes throw */
      }
      return {
        __type: "Element",
        tagName: el.tagName,
        id: el.id || null,
        className:
          typeof el.className === "string" ? el.className : null,
        attributes: Array.from(el.attributes).map((a) => ({
          name: a.name,
          value: a.value,
        })),
        outerHTML: outer,
        textContent: (el.textContent || "").slice(0, 2048),
      };
    }
    if (typeof Node !== "undefined" && o instanceof Node) {
      const n = o as Node;
      return {
        __type: "Node",
        nodeName: n.nodeName,
        nodeValue: n.nodeValue,
      };
    }
    if (o instanceof Error) {
      return {
        __type: "Error",
        name: o.name,
        message: o.message,
        stack: o.stack ?? null,
      };
    }
    if (Array.isArray(o)) return o.map(walk);
    if (o instanceof Map) {
      return {
        __type: "Map",
        entries: Array.from(o.entries()).map(([k, vv]) => [walk(k), walk(vv)]),
      };
    }
    if (o instanceof Set) {
      return {
        __type: "Set",
        values: Array.from(o.values()).map(walk),
      };
    }
    if (o instanceof Date) {
      return { __type: "Date", value: o.toISOString() };
    }
    if (o instanceof RegExp) {
      return { __type: "RegExp", source: o.source, flags: o.flags };
    }
    if (o instanceof ArrayBuffer) {
      return { __type: "ArrayBuffer", byteLength: o.byteLength };
    }
    if (ArrayBuffer.isView(o)) {
      const tav = o as ArrayBufferView & { length?: number };
      return {
        __type: o.constructor?.name ?? "TypedArray",
        byteLength: tav.byteLength,
        length: tav.length ?? null,
      };
    }
    // Plain / unknown object — walk own enumerable string keys.
    const out: Record<string, unknown> = {};
    let keys: string[];
    try {
      keys = Object.keys(o as Record<string, unknown>);
    } catch {
      return { __type: "OpaqueObject", constructor: o.constructor?.name ?? null };
    }
    for (const k of keys) {
      try {
        out[k] = walk((o as Record<string, unknown>)[k]);
      } catch (e) {
        out[k] = {
          __type: "AccessError",
          message: String((e as Error)?.message ?? e),
        };
      }
    }
    if (o.constructor && o.constructor.name && o.constructor.name !== "Object") {
      out.__constructor = o.constructor.name;
    }
    return out;
  }
  return walk(v);
}

interface EvalArgs {
  js?: string;
  await_ms?: number;
}

registerPrimitive("eval_js", async (rawArgs) => {
  const args = rawArgs as EvalArgs;
  const src = String(args.js ?? "");
  if (!src) throw new PrimitiveError("invalid_args", "js is required");
  const awaitMs =
    typeof args.await_ms === "number" && args.await_ms > 0
      ? args.await_ms
      : 30_000;

  // Capture console.log/info/warn/error/debug for the duration of the
  // eval. We record level + serialised args + timestamp; restore after.
  const captured: Array<{ level: string; args: unknown[]; ts: number }> = [];
  const levels = ["log", "info", "warn", "error", "debug"] as const;
  type ConsoleLevel = (typeof levels)[number];
  const orig: Partial<Record<ConsoleLevel, (...a: unknown[]) => void>> = {};
  for (const lv of levels) {
    orig[lv] = console[lv].bind(console);
    console[lv] = (...a: unknown[]) => {
      try {
        captured.push({
          level: lv,
          args: a.map((x) => _serialize(x)),
          ts: Date.now(),
        });
      } catch {
        /* must not break console */
      }
      return orig[lv]!(...a);
    };
  }

  const t0 = performance.now();
  let value: unknown = undefined;
  let errorObj: { name: string; message: string; stack: string | null } | null =
    null;
  let timedOut = false;

  try {
    const AsyncFunction = Object.getPrototypeOf(async function () {})
      .constructor as new (...a: string[]) => (...a: unknown[]) => Promise<unknown>;
    const fn = new AsyncFunction(src);
    const result = fn.call(window);
    const timeout = new Promise<never>((_, reject) => {
      setTimeout(
        () => reject(new Error(`eval_js await_ms timeout after ${awaitMs}ms`)),
        awaitMs,
      );
    });
    value = await Promise.race([result, timeout]);
  } catch (e) {
    const err = e as { name?: string; message?: string; stack?: string };
    errorObj = {
      name: err?.name || "Error",
      message: err?.message || String(e),
      stack: err?.stack ?? null,
    };
    if (errorObj.message.includes("await_ms timeout")) timedOut = true;
  } finally {
    for (const lv of levels) {
      if (orig[lv]) console[lv] = orig[lv]!;
    }
  }
  const elapsed_ms = Math.round(performance.now() - t0);
  return {
    ok: errorObj === null,
    value: _serialize(value),
    console: captured,
    elapsed_ms,
    timed_out: timedOut,
    error: errorObj,
  };
});

