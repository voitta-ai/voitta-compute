// Settings — persisted on the FastAPI backend at GET/PUT /api/settings,
// edited via the in-pane Settings view, sent with every chat request.
//
// API keys live HERE (in the backend's user-config dir), not in the
// browser. The backend is still a key-less *relay* for chat traffic:
// each /api/chat/stream request includes the chosen provider's key in
// its body. The key never appears in any log line (the backend redacts
// at the DTO level — `api_key` is consumed, then the DTO is dropped at
// end of request).
//
// Why backend storage instead of localStorage: localStorage is
// partitioned per host origin, so keys saved when the bookmarklet was
// run on one site would be invisible when run on another. The backend
// lives at a single origin (127.0.0.1:12358) shared across every host,
// so keys persist.
//
// Bootstrap protocol:
//   • The bridge (lib/bridge.ts) calls bootstrapSettings(backendOrigin)
//     once on widget mount. That fires GET /api/settings and updates
//     the in-memory cache + notifies subscribers (so React re-renders).
//   • Until bootstrap returns, loadSettings() yields DEFAULT_SETTINGS.
//   • saveSettings(patch) updates the cache synchronously and fires
//     PUT /api/settings in the background.

import { log } from "./logger";

export type ProviderId = "anthropic" | "openai" | "gemini";

export interface Settings {
  provider: ProviderId;
  // Per-provider keys; the chat request only sends the chosen one.
  anthropicApiKey: string;
  openaiApiKey: string;
  geminiApiKey: string;
  // Per-provider models so switching providers doesn't lose your model
  // choice for the inactive provider.
  anthropicModel: string;
  openaiModel: string;
  geminiModel: string;
  maxTokens: number;
  maxToolIterations: number;
  // Browser-side compute paradigm (JS buffers, in-browser plotting,
  // buffer_eval JS sandbox, parser-to-buffer flow). Default OFF —
  // the project's default workflow is Python-only
  // (download_to_python_storage + compute scripts). When OFF, the
  // 16 JS-compute tools are hidden from the LLM.
  jsCompute: boolean;
  // Open-web retrieval tool (web_fetch). Default ON — disable to hide
  // the tool from the LLM (e.g. on a host where you don't want any
  // outbound traffic to third parties).
  webFetch: boolean;
}

export const MODELS_BY_PROVIDER: Record<ProviderId, string[]> = {
  anthropic: [
    "claude-opus-4-7",
    "claude-sonnet-4-6",
    "claude-haiku-4-5-20251001",
  ],
  openai: ["gpt-5", "gpt-5-mini", "gpt-4.1", "gpt-4o"],
  gemini: [
    "gemini-3.1-pro-preview",
    "gemini-3-pro-preview",
    "gemini-pro-latest",
    "gemini-3.1-flash-lite-preview",
    "gemini-3-flash-preview",
    "gemini-flash-latest",
    "gemini-flash-lite-latest",
    "gemini-2.5-pro",
    "gemini-2.5-flash",
  ],
};

export const DEFAULT_MODEL: Record<ProviderId, string> = {
  anthropic: "claude-sonnet-4-6",
  openai: "gpt-5",
  gemini: "gemini-3.1-pro-preview",
};

export const DEFAULT_SETTINGS: Settings = {
  provider: "anthropic",
  anthropicApiKey: "",
  openaiApiKey: "",
  geminiApiKey: "",
  anthropicModel: DEFAULT_MODEL.anthropic,
  openaiModel: DEFAULT_MODEL.openai,
  geminiModel: DEFAULT_MODEL.gemini,
  maxTokens: 16384,
  maxToolIterations: 25,
  jsCompute: false,
  webFetch: true,
};

const listeners = new Set<(s: Settings) => void>();
let cached: Settings = { ...DEFAULT_SETTINGS };
let backendOrigin = "";

/** Synchronous accessor — returns whatever's currently cached. Until
 * `bootstrapSettings` resolves on widget mount this yields defaults;
 * subscribers (`subscribeSettings`) will be notified once the real blob
 * arrives. */
export function loadSettings(): Settings {
  return cached;
}

/** Apply a patch and persist. Updates the cache + notifies subscribers
 * synchronously; PUT to the backend is fire-and-forget (errors logged,
 * not thrown — there's no UX path for "save failed" yet). */
export function saveSettings(patch: Partial<Settings>): Settings {
  const next = sanitise({ ...cached, ...patch });
  cached = next;
  notify(next);
  void putToBackend(next);
  return next;
}

export function subscribeSettings(fn: (s: Settings) => void): () => void {
  listeners.add(fn);
  return () => {
    listeners.delete(fn);
  };
}

/** Called once by the bridge when the widget mounts. Fetches the
 * persisted blob from the backend and updates the cache. Safe to call
 * before, during, or after first render. */
export async function bootstrapSettings(origin: string): Promise<Settings> {
  backendOrigin = origin.replace(/\/$/, "");
  try {
    const res = await fetch(`${backendOrigin}/api/settings`, {
      method: "GET",
      credentials: "omit",
    });
    if (!res.ok) {
      log.warn("settings", "GET /api/settings failed", { status: res.status });
      return cached;
    }
    const body = await res.json();
    const merged = sanitise({
      ...DEFAULT_SETTINGS,
      ...(body && typeof body === "object" ? body : {}),
    });
    cached = merged;
    notify(merged);
    return merged;
  } catch (err) {
    log.warn("settings", "GET /api/settings threw; keeping defaults", {
      err: String(err),
    });
    return cached;
  }
}

function notify(s: Settings) {
  for (const fn of listeners) {
    try {
      fn(s);
    } catch {
      /* listeners must not break the producer */
    }
  }
}

async function putToBackend(s: Settings): Promise<void> {
  if (!backendOrigin) {
    log.warn("settings", "save before bootstrap; PUT skipped");
    return;
  }
  try {
    const res = await fetch(`${backendOrigin}/api/settings`, {
      method: "PUT",
      headers: { "content-type": "application/json" },
      credentials: "omit",
      body: JSON.stringify(s),
      keepalive: true,
    });
    if (!res.ok) {
      log.warn("settings", "PUT /api/settings failed", { status: res.status });
    }
  } catch (err) {
    log.warn("settings", "PUT /api/settings threw", { err: String(err) });
  }
}

function sanitise(s: Settings): Settings {
  // Coerce + clamp numeric fields; collapse unknown providers to default.
  const provider: ProviderId = (
    ["anthropic", "openai", "gemini"] as const
  ).includes(s.provider)
    ? s.provider
    : DEFAULT_SETTINGS.provider;
  const maxTokens = clampInt(s.maxTokens, 256, 65536, DEFAULT_SETTINGS.maxTokens);
  const maxToolIterations = clampInt(
    s.maxToolIterations,
    1,
    200,
    DEFAULT_SETTINGS.maxToolIterations,
  );
  return {
    provider,
    anthropicApiKey: (s.anthropicApiKey || "").trim(),
    openaiApiKey: (s.openaiApiKey || "").trim(),
    geminiApiKey: (s.geminiApiKey || "").trim(),
    anthropicModel: s.anthropicModel || DEFAULT_MODEL.anthropic,
    openaiModel: s.openaiModel || DEFAULT_MODEL.openai,
    geminiModel: s.geminiModel || DEFAULT_MODEL.gemini,
    maxTokens,
    maxToolIterations,
    jsCompute: !!s.jsCompute,
    // Default-on: an undefined value (older blob) → true. Only an
    // explicit `false` keeps it off.
    webFetch: s.webFetch !== false,
  };
}

function clampInt(v: unknown, lo: number, hi: number, fallback: number): number {
  const n = typeof v === "number" ? v : parseInt(String(v), 10);
  if (!Number.isFinite(n)) return fallback;
  return Math.max(lo, Math.min(hi, Math.round(n)));
}

// Convenience accessors used by the chat request layer.
export function activeApiKey(s: Settings): string {
  return s.provider === "anthropic"
    ? s.anthropicApiKey
    : s.provider === "openai"
      ? s.openaiApiKey
      : s.geminiApiKey;
}

export function activeModel(s: Settings): string {
  return s.provider === "anthropic"
    ? s.anthropicModel
    : s.provider === "openai"
      ? s.openaiModel
      : s.geminiModel;
}
