import { render } from "preact";
import { ChatPane } from "./components/ChatPane";
import { startBridge } from "./lib/bridge";
import { log } from "./lib/logger";
import "./lib/primitives"; // side-effect: registers the core browser primitives
import "./lib/primitives-buffers"; // side-effect: registers buffer + plot + eval primitives

// Plugin frontends — every ``plugins/<name>/frontend/widget.ts`` is
// glob-imported so its ``registerPrimitive`` calls run at module load.
// ``eager: true`` makes Vite inline the modules into our bundle rather
// than chunk-split them, which keeps the bookmarklet a single file.
// Adding a plugin = creating ``plugins/<name>/frontend/widget.ts``;
// no edits to this file required.
import.meta.glob("../../plugins/*/frontend/widget.ts", { eager: true });
import themeCss from "./theme.css?raw";
import widgetCss from "./styles.css?raw";

const VOITTA_FONTS_URL =
  "https://fonts.googleapis.com/css2?family=Cormorant+Garamond:wght@500;600&family=Open+Sans:wght@400;500;600;700&display=swap";

const HOST_ID = "voitta-bookmarklet-host";
const BACKEND_FALLBACK = "https://127.0.0.1:12358";

// Resolve the backend origin from the URL the bundle was loaded from. The
// bookmarklet sets <script src="https://host:port/widget.js"> — we reuse that
// origin for all API calls so swapping ports / hosts only needs to happen in
// one place (the bookmarklet itself).
function detectBackendOrigin(): string {
  const current = document.currentScript as HTMLScriptElement | null;
  if (current?.src) {
    try {
      return new URL(current.src).origin;
    } catch {
      /* fall through */
    }
  }
  for (const s of Array.from(document.scripts)) {
    if (s.src && /\/widget\.js(\?|$)/.test(s.src)) {
      try {
        return new URL(s.src).origin;
      } catch {
        /* ignore */
      }
    }
  }
  return BACKEND_FALLBACK;
}

interface PluginInfo {
  name: string;
  agent_name: string;
  theme_url?: string;
  default_layout?: "chat-left" | "chat-right";
}

async function fetchPluginInfo(backendOrigin: string): Promise<PluginInfo | null> {
  try {
    const res = await fetch(
      `${backendOrigin}/api/plugin?host=${encodeURIComponent(window.location.hostname)}`,
      { credentials: "include", signal: AbortSignal.timeout(2000) },
    );
    if (!res.ok) return null;
    return (await res.json()) as PluginInfo;
  } catch {
    return null;
  }
}

export function mount(): void {
  if (document.getElementById(HOST_ID)) return;

  const host = document.createElement("div");
  host.id = HOST_ID;
  // ``all: initial`` is omitted on purpose. We used to set it for
  // double-isolation against page-level cascade, but it's the
  // canonical fingerprint anti-injection scripts (e.g. on ebay.com)
  // look for to find bookmarklets/userscripts and hide them with
  // ``display: none !important``. The closed shadow root attached
  // below already isolates the host from outside CSS — the shadow
  // boundary is what actually matters for style scoping; ``all:
  // initial`` was belt-and-braces.
  host.style.cssText =
    "position:fixed;inset:0;pointer-events:none;z-index:2147483647;";
  document.documentElement.appendChild(host);

  // ``mode: "closed"`` so ``host.shadowRoot`` returns null from
  // outside the original attachShadow call. Some hostile pages (e.g.
  // ebay.com's anti-injection scan) walk every element looking for
  // ``el.shadowRoot !== null`` and hide the host with
  // ``display: none !important`` — closed mode defeats that scanner.
  // Plugins / ChatPane / primitives that need the shadow root read
  // it from a non-enumerable property under a per-mount random key
  // exposed on ``window.VoittaBookmarklet`` rather than via the
  // standard ``shadowRoot`` accessor.
  const shadow = host.attachShadow({ mode: "closed" });
  // Stash on the host under a unique, non-enumerable property so
  // same-page code (us) can find it without the standard accessor.
  // The key is also exported via window.VoittaBookmarklet so plugin
  // primitives can locate the root without re-deriving it.
  Object.defineProperty(host, "__voittaShadowRoot", {
    value: shadow,
    enumerable: false,
    configurable: false,
    writable: false,
  });
  // Expose a getter on the global so other modules don't need the
  // host id constant. Idempotent — second mount() call wouldn't run
  // because of the early return at the top of this function.
  (window as unknown as { VoittaBookmarklet?: Record<string, unknown> }).VoittaBookmarklet ??= {};
  ((window as unknown as { VoittaBookmarklet: Record<string, unknown> }).VoittaBookmarklet)
    .getShadowRoot = () => shadow;

  // Voitta fonts. Fetched once from Google Fonts — failure is silent
  // (we fall back to system-ui / Georgia per --voitta-font-* tokens).
  const fontsLink = document.createElement("link");
  fontsLink.rel = "stylesheet";
  fontsLink.href = VOITTA_FONTS_URL;
  shadow.appendChild(fontsLink);

  // Theme tokens FIRST so styles.css can consume them.
  const themeEl = document.createElement("style");
  themeEl.textContent = themeCss;
  shadow.appendChild(themeEl);

  const styleEl = document.createElement("style");
  styleEl.textContent = widgetCss;
  shadow.appendChild(styleEl);

  const mountPoint = document.createElement("div");
  mountPoint.className = "voitta-mount";
  shadow.appendChild(mountPoint);

  const backendOrigin = detectBackendOrigin();
  log.info("widget", "mount", { backendOrigin });

  // Single plugin bootstrap fetch — drives theme injection, agent name,
  // and layout default. Bridge starts after so plugin defaults are in
  // place before bootstrapSettings fires.
  fetchPluginInfo(backendOrigin).then((plugin) => {
    if (plugin?.theme_url) {
      // Insert plugin theme BEFORE the base theme so its :host { } block
      // wins in the cascade (later rule = higher priority for equal specificity).
      const pluginThemeLink = document.createElement("link");
      pluginThemeLink.rel = "stylesheet";
      pluginThemeLink.href = `${backendOrigin}${plugin.theme_url}`;
      shadow.insertBefore(pluginThemeLink, themeEl);
    }
    render(
      <ChatPane backendOrigin={backendOrigin} agentName={plugin?.agent_name} />,
      mountPoint,
    );
    const pluginDefaults = plugin?.default_layout
      ? { layout: plugin.default_layout as "chat-left" | "chat-right" }
      : undefined;
    startBridge(backendOrigin, pluginDefaults);
  });
}
