import { render } from "preact";
import { ChatPane } from "./components/ChatPane";
import { startBridge } from "./lib/bridge";
import { log } from "./lib/logger";
import "./lib/primitives"; // side-effect: registers the browser primitives
import "./lib/primitives-buffers"; // side-effect: registers buffer + plot + eval primitives
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

export function mount(): void {
  if (document.getElementById(HOST_ID)) return;

  const host = document.createElement("div");
  host.id = HOST_ID;
  // `all: initial` insulates the host from page-level cascade. Shadow DOM
  // handles the inverse direction (our styles staying inside).
  host.style.cssText =
    "all:initial;position:fixed;inset:0;pointer-events:none;z-index:2147483647;";
  document.documentElement.appendChild(host);

  const shadow = host.attachShadow({ mode: "open" });

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
  render(<ChatPane backendOrigin={backendOrigin} />, mountPoint);

  // Open the server↔browser tool channel. Idempotent — a second mount() call
  // is a no-op for the bridge as well as the DOM host.
  startBridge(backendOrigin);
}
