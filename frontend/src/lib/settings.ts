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
  // Hacky no-OAuth Drive download fallback. When true AND Google OAuth
  // is NOT connected, the LLM gets `drive_pickup_to_python_storage`
  // which opens the Drive download URL in a new tab and watches the
  // configured Downloads directory for the resulting file. Default OFF
  // — racy, only opt in if you know what you're doing.
  driveDownloadViaPickup: boolean;
  // Where the pickup tool watches for downloaded files. Tilde and env
  // vars are expanded server-side. Empty string → ~/Downloads.
  pickupDownloadsDir: string;
  // Panel layout. "chat-right" (default): chat drawer on the right, report
  // on the left. "chat-left": mirrored. The server-side default is set via
  // VOITTA_DEFAULT_LAYOUT; the user can override here.
  layout: "chat-right" | "chat-left";
  // Plugin-namespaced settings tree. Each loaded plugin gets its own
  // sub-object — the manifest's ``settings_schema`` fields reference
  // dot-paths like ``plugins.<name>.<...>``. The backend reads these
  // same dot-paths in :func:`app.services.mcp.registry._dotted_get`
  // when resolving an MCP server's URL + bearer token.
  //
  // Stored as an opaque ``unknown``-valued dict so the core type
  // doesn't need to learn about every plugin's fields. Plugins access
  // their slice via :func:`getPluginSetting` / :func:`setPluginSetting`.
  plugins: Record<string, Record<string, unknown>>;
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
  driveDownloadViaPickup: false,
  pickupDownloadsDir: "~/Downloads",
  layout: "chat-right",
  plugins: {},
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
export async function bootstrapSettings(
  origin: string,
  pluginDefaults?: Partial<Settings>,
): Promise<Settings> {
  backendOrigin = origin.replace(/\/$/, "");
  try {
    const res = await fetch(`${backendOrigin}/api/settings`, {
      method: "GET",
      // ``include`` so the auth cookie attaches in non-localhost mode.
      // GET /api/settings is gated; the chat won't bootstrap without it.
      credentials: "include",
    });
    if (!res.ok) {
      log.warn("settings", "GET /api/settings failed", { status: res.status });
      return cached;
    }
    const body = await res.json();
    // Plugin defaults sit between the hardcoded DEFAULT_SETTINGS and the
    // user's persisted blob — they apply only when the user hasn't saved
    // an explicit preference.
    const merged = sanitise({
      ...DEFAULT_SETTINGS,
      ...(pluginDefaults ?? {}),
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
      credentials: "include",
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
    driveDownloadViaPickup: !!s.driveDownloadViaPickup,
    pickupDownloadsDir:
      typeof s.pickupDownloadsDir === "string" && s.pickupDownloadsDir.trim()
        ? s.pickupDownloadsDir.trim()
        : DEFAULT_SETTINGS.pickupDownloadsDir,
    layout: s.layout === "chat-left" ? "chat-left" : "chat-right",
    // Plugins tree: shallow-copy the object so identity-based listeners
    // notice changes; per-plugin shape is left opaque because the
    // schemas are plugin-defined.
    plugins:
      s.plugins && typeof s.plugins === "object" && !Array.isArray(s.plugins)
        ? { ...(s.plugins as Record<string, Record<string, unknown>>) }
        : {},
  };
}

/** Walk a dot-path like ``plugins.voitta-enterprise.mcp.url`` through
 * the settings blob. Returns ``undefined`` for any missing segment.
 *
 * Mirror of the backend's ``_dotted_get`` helper in
 * ``app.services.mcp.registry`` — same path syntax, same shape. Plugin
 * settings_schema fields reference dot-paths so the same string works
 * on both sides of the wire. */
export function getDotted(s: Settings, path: string): unknown {
  let cur: unknown = s as unknown;
  for (const part of path.split(".")) {
    if (cur === null || typeof cur !== "object") return undefined;
    cur = (cur as Record<string, unknown>)[part];
    if (cur === undefined) return undefined;
  }
  return cur;
}

/** Apply a dot-path patch onto a deep-cloned settings blob and return
 * the new value. Intermediate objects are created on demand; existing
 * sibling fields are preserved. Pass via :func:`saveSettings` to persist.
 *
 * Only used for plugin settings today (``plugins.<name>.<key>``). The
 * core top-level fields stay flat and use the regular ``saveSettings``
 * patch shape — no reason to route them through this. */
export function setDotted(s: Settings, path: string, value: unknown): Settings {
  // Cheap deep-clone for the path we're touching: walk a copy. We don't
  // structuredClone the whole settings blob because most of it is
  // primitives and shallow copies are fine for listener identity.
  const out: Record<string, unknown> = { ...(s as unknown as Record<string, unknown>) };
  const parts = path.split(".");
  let cur: Record<string, unknown> = out;
  for (let i = 0; i < parts.length - 1; i++) {
    const k = parts[i];
    const existing = cur[k];
    const next: Record<string, unknown> =
      existing && typeof existing === "object" && !Array.isArray(existing)
        ? { ...(existing as Record<string, unknown>) }
        : {};
    cur[k] = next;
    cur = next;
  }
  cur[parts[parts.length - 1]] = value;
  return out as unknown as Settings;
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
