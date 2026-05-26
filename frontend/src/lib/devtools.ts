// Browser devtools capture: console, network (fetch + XHR), and JS errors.
//
// Stored on window.__voitta_devtools__ so it survives across eval_js calls
// and is accessible from any execution context in the page.
//
// Usage (via MCP):
//   install_devtools_capture  — install interceptors (idempotent)
//   get_devtools_data         — read / optionally clear the ring buffer
//   clear_devtools_data       — clear all captured data

const NAMESPACE = "__voitta_devtools__";
const MAX_ENTRIES = 300;         // per category
const MAX_BODY_BYTES = 8_192;    // request / response body truncation limit

// ── Types ─────────────────────────────────────────────────────────────────

export interface ConsoleEntry {
  ts: number;
  level: "log" | "info" | "warn" | "error" | "debug";
  message: string;
  stack?: string;
}

export interface NetworkEntry {
  ts: number;
  method: string;
  url: string;
  status: number | null;
  duration_ms: number | null;
  req_body: string | null;
  res_body: string | null;
  req_headers: Record<string, string>;
  res_headers: Record<string, string>;
  error?: string;
}

export interface ErrorEntry {
  ts: number;
  message: string;
  source?: string;
  lineno?: number;
  colno?: number;
  stack?: string;
}

interface DevtoolsStore {
  installed: boolean;
  console: ConsoleEntry[];
  network: NetworkEntry[];
  errors: ErrorEntry[];
  _origConsole: Partial<Record<string, (...a: unknown[]) => void>>;
  _origFetch: typeof fetch | null;
}

// ── Helpers ───────────────────────────────────────────────────────────────

function getStore(): DevtoolsStore {
  const w = window as unknown as Record<string, unknown>;
  if (!w[NAMESPACE]) {
    w[NAMESPACE] = {
      installed: false,
      console: [],
      network: [],
      errors: [],
      _origConsole: {},
      _origFetch: null,
    } as DevtoolsStore;
  }
  return w[NAMESPACE] as DevtoolsStore;
}

function push<T>(arr: T[], entry: T): void {
  arr.push(entry);
  if (arr.length > MAX_ENTRIES) arr.shift();
}

function truncate(s: string | null | undefined): string | null {
  if (s == null) return null;
  return s.length > MAX_BODY_BYTES ? s.slice(0, MAX_BODY_BYTES) + `…[+${s.length - MAX_BODY_BYTES}B]` : s;
}

function headersToObj(h: Headers): Record<string, string> {
  const out: Record<string, string> = {};
  h.forEach((v, k) => { out[k] = v; });
  return out;
}

function argsToMessage(args: unknown[]): string {
  return args.map(a => {
    if (typeof a === "string") return a;
    try { return JSON.stringify(a); } catch { return String(a); }
  }).join(" ");
}

// ── Console interceptor ───────────────────────────────────────────────────

function installConsole(store: DevtoolsStore): void {
  const levels = ["log", "info", "warn", "error", "debug"] as const;
  for (const level of levels) {
    if (store._origConsole[level]) continue; // already wrapped
    const orig = console[level].bind(console);
    store._origConsole[level] = orig as (...a: unknown[]) => void;
    console[level] = (...args: unknown[]) => {
      orig(...args);
      const entry: ConsoleEntry = {
        ts: Date.now(),
        level,
        message: argsToMessage(args),
      };
      // Capture stack for warn/error
      if (level === "warn" || level === "error") {
        try { entry.stack = new Error().stack?.split("\n").slice(2).join("\n"); } catch { /**/ }
      }
      push(store.console, entry);
    };
  }
}

// ── Fetch interceptor ─────────────────────────────────────────────────────

function installFetch(store: DevtoolsStore): void {
  if (store._origFetch) return; // already wrapped
  store._origFetch = window.fetch;

  window.fetch = async function (input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
    const t0 = Date.now();
    const method = (init?.method ?? "GET").toUpperCase();
    const url = typeof input === "string" ? input : input instanceof URL ? input.href : (input as Request).url;
    let reqBody: string | null = null;
    try {
      if (init?.body != null) {
        reqBody = truncate(typeof init.body === "string" ? init.body : JSON.stringify(init.body));
      }
    } catch { /**/ }

    const reqHeaders: Record<string, string> = {};
    try {
      new Headers(init?.headers).forEach((v, k) => { reqHeaders[k] = v; });
    } catch { /**/ }

    const entry: NetworkEntry = {
      ts: t0, method, url,
      status: null, duration_ms: null,
      req_body: reqBody, res_body: null,
      req_headers: reqHeaders, res_headers: {},
    };
    push(store.network, entry);

    try {
      const res = await store._origFetch!.call(window, input, init);
      entry.status = res.status;
      entry.duration_ms = Date.now() - t0;
      entry.res_headers = headersToObj(res.headers);
      // Clone so the caller's .json()/.text() still works
      try {
        const clone = res.clone();
        const text = await clone.text();
        entry.res_body = truncate(text);
      } catch { /**/ }
      return res;
    } catch (err) {
      entry.duration_ms = Date.now() - t0;
      entry.error = String(err);
      throw err;
    }
  };
}

// ── XHR interceptor ───────────────────────────────────────────────────────

function installXHR(store: DevtoolsStore): void {
  const OrigXHR = window.XMLHttpRequest;
  if ((OrigXHR as unknown as { __voitta_wrapped__?: boolean }).__voitta_wrapped__) return;

  function PatchedXHR(this: XMLHttpRequest) {
    const xhr = new OrigXHR();
    let method = "GET";
    let url = "";
    let reqBody: string | null = null;
    let t0 = 0;

    const entry: NetworkEntry = {
      ts: 0, method: "GET", url: "",
      status: null, duration_ms: null,
      req_body: null, res_body: null,
      req_headers: {}, res_headers: {},
    };

    const origOpen = xhr.open.bind(xhr);
    (this as unknown as Record<string, unknown>).open = (m: string, u: string, ...rest: unknown[]) => {
      method = m.toUpperCase();
      url = u;
      return (origOpen as (...a: unknown[]) => void)(m, u, ...rest);
    };

    const origSend = xhr.send.bind(xhr);
    (this as unknown as Record<string, unknown>).send = (body?: unknown) => {
      t0 = Date.now();
      reqBody = body != null ? truncate(typeof body === "string" ? body : JSON.stringify(body)) : null;
      Object.assign(entry, { ts: t0, method, url, req_body: reqBody });
      push(store.network, entry);
      xhr.addEventListener("loadend", () => {
        entry.status = xhr.status;
        entry.duration_ms = Date.now() - t0;
        try {
          const raw = xhr.getAllResponseHeaders();
          raw.trim().split(/[\r\n]+/).forEach(line => {
            const [k, ...v] = line.split(": ");
            if (k) entry.res_headers[k.toLowerCase()] = v.join(": ");
          });
        } catch { /**/ }
        entry.res_body = truncate(xhr.responseText);
      });
      return origSend(body);
    };

    return xhr;
  }
  (PatchedXHR as unknown as { __voitta_wrapped__: boolean }).__voitta_wrapped__ = true;
  window.XMLHttpRequest = PatchedXHR as unknown as typeof XMLHttpRequest;
}

// ── Error interceptor ─────────────────────────────────────────────────────

function installErrors(store: DevtoolsStore): void {
  window.addEventListener("error", (e) => {
    push(store.errors, {
      ts: Date.now(),
      message: e.message,
      source: e.filename,
      lineno: e.lineno,
      colno: e.colno,
      stack: e.error?.stack,
    });
  }, { capture: true });

  window.addEventListener("unhandledrejection", (e) => {
    push(store.errors, {
      ts: Date.now(),
      message: String(e.reason),
      stack: e.reason?.stack,
    });
  }, { capture: true });
}

// ── Public primitives ─────────────────────────────────────────────────────

export function installDevtoolsCapture(): { ok: boolean; already_installed: boolean } {
  const store = getStore();
  if (store.installed) return { ok: true, already_installed: true };
  installConsole(store);
  installFetch(store);
  installXHR(store);
  installErrors(store);
  store.installed = true;
  return { ok: true, already_installed: false };
}

export function getDevtoolsData(args: {
  kind?: "console" | "network" | "errors" | "all";
  limit?: number;
  clear?: boolean;
}): {
  ok: boolean;
  installed: boolean;
  console?: ConsoleEntry[];
  network?: NetworkEntry[];
  errors?: ErrorEntry[];
} {
  const store = getStore();
  const kind = args.kind ?? "all";
  const limit = typeof args.limit === "number" ? args.limit : MAX_ENTRIES;

  const result: ReturnType<typeof getDevtoolsData> = {
    ok: true,
    installed: store.installed,
  };

  if (kind === "all" || kind === "console") {
    result.console = store.console.slice(-limit);
  }
  if (kind === "all" || kind === "network") {
    result.network = store.network.slice(-limit);
  }
  if (kind === "all" || kind === "errors") {
    result.errors = store.errors.slice(-limit);
  }

  if (args.clear) {
    if (kind === "all" || kind === "console") store.console = [];
    if (kind === "all" || kind === "network") store.network = [];
    if (kind === "all" || kind === "errors") store.errors = [];
  }

  return result;
}

export function clearDevtoolsData(): { ok: boolean } {
  const store = getStore();
  store.console = [];
  store.network = [];
  store.errors = [];
  return { ok: true };
}
