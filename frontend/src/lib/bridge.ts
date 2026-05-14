// Tool-channel bridge: server↔browser tool dispatch over SSE + POST.
//
// Boot sequence:
//   1. Generate a 128-bit session_id.
//   2. Open EventSource(`${BACKEND}/tools/inbox?session_id=...`).
//   3. On the `ready` event, POST `${BACKEND}/tools/register` with the
//      capability list + identification payload.
//   4. For each `call` event: dispatch to the registered primitive, POST the
//      result back to `${BACKEND}/tools/result`.
//   5. For each `cancel` event: abort the matching in-flight primitive.
//   6. On SPA navigation: re-POST `register` so the server's view of `page`
//      stays current.

import { log } from "./logger";
import { bootstrapSettings } from "./settings";

export interface PrimitiveContext {
  signal: AbortSignal;
}

export type Primitive = (
  args: Record<string, unknown>,
  ctx: PrimitiveContext,
) => Promise<unknown>;

export class PrimitiveError extends Error {
  kind: string;
  details?: Record<string, unknown>;
  constructor(kind: string, message: string, details?: Record<string, unknown>) {
    super(message);
    this.kind = kind;
    this.details = details;
  }
}

const primitives = new Map<string, Primitive>();
const inflight = new Map<string, AbortController>();

let started = false;
let SESSION_ID = "";
let BACKEND_ORIGIN = "";

function newSessionId(): string {
  const buf = new Uint8Array(16);
  crypto.getRandomValues(buf);
  return Array.from(buf, (b) => b.toString(16).padStart(2, "0")).join("");
}

export function getSessionId(): string {
  return SESSION_ID;
}

/** The FastAPI backend origin captured by `startBridge`. Empty until the
 * widget mounts; useful for primitives that need to load resources from
 * the backend (e.g. iframes pointing at /api/reports/...). */
export function getBackendOrigin(): string {
  return BACKEND_ORIGIN;
}

export function registerPrimitive(name: string, fn: Primitive): void {
  primitives.set(name, fn);
}

export function startBridge(
  backendOrigin: string,
  pluginDefaults?: Parameters<typeof bootstrapSettings>[1],
): void {
  if (started) return;
  started = true;
  SESSION_ID = newSessionId();
  BACKEND_ORIGIN = backendOrigin;

  log.info("bridge", "starting", { backendOrigin, sessionId: SESSION_ID });

  // Pull persisted user settings from the backend. Plugin defaults are
  // applied beneath the user's saved blob so a user preference always wins.
  void bootstrapSettings(backendOrigin, pluginDefaults);

  // Self-healing inbox: keep ONE EventSource alive at any time. If
  // the browser's built-in retry stalls (Chrome occasionally gives
  // up after long tab-suspends), we proactively reopen and
  // re-register. Always uses the SAME ``SESSION_ID`` so the
  // backend's session bucket is preserved across reconnects.
  let currentEs: EventSource | null = null;
  let reopenTimer: number | null = null;
  let reopenAttempt = 0;
  const MAX_REOPEN_DELAY_MS = 10_000;

  function openInbox(): void {
    // Tear down any prior connection.
    if (currentEs) {
      try {
        currentEs.close();
      } catch {
        /* ignore */
      }
      currentEs = null;
    }
    if (reopenTimer != null) {
      clearTimeout(reopenTimer);
      reopenTimer = null;
    }

    const url = `${backendOrigin}/tools/inbox?session_id=${SESSION_ID}`;
    // ``withCredentials: true`` so cookies (incl. the auth session
    // cookie set by /api/auth/login) ride on the SSE connection in
    // non-localhost mode. In localhost mode the auth gate is bypassed
    // server-side so this is a harmless no-op.
    const es = new EventSource(url, { withCredentials: true });
    currentEs = es;

    es.addEventListener("ready", () => {
      log.info("bridge", reopenAttempt > 0 ? "inbox ready (reconnected)" : "inbox ready", {
        attempt: reopenAttempt,
      });
      reopenAttempt = 0;
      // Always re-POST register on every (re)open so the backend has
      // the latest capabilities + page info, AND so a backend that
      // was restarted recreates the session bucket via the
      // server-side tolerance added to ``bridge.register``.
      postRegister(backendOrigin).catch((err) => {
        log.error("bridge", "register failed", err);
      });
    });

    es.addEventListener("call", (e) => {
      let payload: {
        call_id: string;
        name: string;
        args: Record<string, unknown>;
        timeout_ms?: number;
      };
      try {
        payload = JSON.parse((e as MessageEvent).data);
      } catch {
        return;
      }
      void runCall(backendOrigin, payload);
    });

    es.addEventListener("cancel", (e) => {
      let payload: { call_id: string };
      try {
        payload = JSON.parse((e as MessageEvent).data);
      } catch {
        return;
      }
      const ac = inflight.get(payload.call_id);
      if (ac) {
        ac.abort();
        inflight.delete(payload.call_id);
      }
    });

    es.addEventListener("ping", () => {
      /* keep-alive only */
    });

    es.onerror = () => {
      // EventSource state machine — CONNECTING (0), OPEN (1), CLOSED (2).
      // The spec says CONNECTING means the browser will auto-retry;
      // we leave that path alone. CLOSED means the browser gave up;
      // we manually reopen with backoff.
      log.warn("bridge", "inbox SSE error", { readyState: es.readyState });
      if (es.readyState === 2 /* CLOSED */) {
        scheduleReopen();
      }
    };
  }

  function scheduleReopen(): void {
    if (reopenTimer != null) return; // already scheduled
    reopenAttempt++;
    const delay = Math.min(500 * 2 ** Math.min(reopenAttempt - 1, 5), MAX_REOPEN_DELAY_MS);
    log.info("bridge", `inbox CLOSED — reopening in ${delay}ms`, { attempt: reopenAttempt });
    reopenTimer = window.setTimeout(() => {
      reopenTimer = null;
      openInbox();
    }, delay);
  }

  // If the tab returns from background, force-check the inbox
  // immediately rather than waiting for the next exponential tick.
  // Chrome often kills the SSE silently while the tab is hidden.
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && currentEs?.readyState === 2) {
      log.info("bridge", "tab visible + inbox closed — reopening");
      openInbox();
    }
  });

  openInbox();

  // Re-post register on SPA navigation so server-side `page` stays current.
  let lastHref = location.href;
  const reregisterIfNavigated = () => {
    if (location.href !== lastHref) {
      lastHref = location.href;
      postRegister(backendOrigin).catch(() => {
        /* ignore */
      });
    }
  };
  window.addEventListener("popstate", reregisterIfNavigated);
  // History API push/replace don't fire popstate; patch them.
  patchHistory(reregisterIfNavigated);
}

function patchHistory(onChange: () => void): void {
  for (const method of ["pushState", "replaceState"] as const) {
    const orig = history[method];
    history[method] = function (...args: Parameters<typeof orig>) {
      // eslint-disable-next-line prefer-spread
      const ret = orig.apply(this, args);
      // Defer so the URL is committed before we look at it.
      setTimeout(onChange, 0);
      return ret;
    };
  }
}

async function runCall(
  backendOrigin: string,
  payload: {
    call_id: string;
    name: string;
    args: Record<string, unknown>;
    timeout_ms?: number;
  },
): Promise<void> {
  const fn = primitives.get(payload.name);
  let envelope: {
    ok: boolean;
    result?: unknown;
    error?: { kind: string; message: string; details?: unknown };
  };

  if (!fn) {
    log.warn("bridge", `unknown primitive ${payload.name}`, {
      call_id: payload.call_id,
    });
    envelope = {
      ok: false,
      error: {
        kind: "unknown_primitive",
        message: `no primitive named ${payload.name}`,
      },
    };
  } else {
    const ac = new AbortController();
    inflight.set(payload.call_id, ac);
    const t0 = performance.now();
    try {
      const result = await fn(payload.args, { signal: ac.signal });
      envelope = { ok: true, result };
      log.info("bridge", `${payload.name} ok`, {
        call_id: payload.call_id,
        ms: Math.round(performance.now() - t0),
      });
    } catch (err) {
      if (err instanceof PrimitiveError) {
        envelope = {
          ok: false,
          error: { kind: err.kind, message: err.message, details: err.details },
        };
        log.error("bridge", `${payload.name} ${err.kind}: ${err.message}`, {
          call_id: payload.call_id,
          details: err.details,
          args: payload.args,
        });
      } else if (err && (err as Error).name === "AbortError") {
        envelope = { ok: false, error: { kind: "cancelled", message: "aborted" } };
        log.info("bridge", `${payload.name} cancelled`, {
          call_id: payload.call_id,
        });
      } else {
        const msg = err instanceof Error ? err.message : String(err);
        envelope = { ok: false, error: { kind: "exception", message: msg } };
        log.error("bridge", `${payload.name} threw: ${msg}`, {
          call_id: payload.call_id,
          stack: err instanceof Error ? err.stack : undefined,
          args: payload.args,
        });
      }
    } finally {
      inflight.delete(payload.call_id);
    }
  }

  await fetch(`${backendOrigin}/tools/result`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      session_id: SESSION_ID,
      call_id: payload.call_id,
      ...envelope,
    }),
    // Cookie-based auth in non-localhost mode; harmless in localhost mode.
    credentials: "include",
  }).catch((err) => {
    log.warn("bridge", "POST /tools/result failed", {
      call_id: payload.call_id,
      err: String(err),
    });
  });
}

async function postRegister(backendOrigin: string): Promise<void> {
  const releaseTag =
    (window as unknown as { releaseTag?: string }).releaseTag ??
    document.querySelector('meta[name="release-tag"]')?.getAttribute("content") ??
    null;

  const payload = {
    session_id: SESSION_ID,
    capabilities: Array.from(primitives.keys()),
    page: {
      href: location.href,
      host: location.host,
      origin: location.origin,
      pathname: location.pathname,
      search: location.search,
      hash: location.hash,
      title: document.title,
      referrer: document.referrer,
      loaded_at: new Date().toISOString(),
    },
    user_agent: navigator.userAgent,
    viewport: {
      w: window.innerWidth,
      h: window.innerHeight,
      dpr: window.devicePixelRatio,
    },
    release_tag: releaseTag,
  };
  const res = await fetch(`${backendOrigin}/tools/register`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
    credentials: "include",
  });
  if (!res.ok) {
    log.warn("bridge", `register HTTP ${res.status}`, { sessionId: SESSION_ID });
  } else {
    log.info("bridge", "registered with backend", {
      caps: payload.capabilities.length,
      page: payload.page.pathname,
    });
  }
}
