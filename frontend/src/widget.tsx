/// <reference types="vite/client" />
// Bookmarklet entry. Mounts the React app into a CLOSED shadow root
// on the host page so the widget's CSS can't leak in either direction.
// ``mode: "closed"`` also defeats some anti-injection scanners that
// walk every element and hide hosts with non-null shadowRoot.

import { createRoot } from "react-dom/client";
import App from "./App";
import { cssText } from "./styles";

// Plugin frontends — every ``plugins/<name>/frontend/widget.ts`` is
// glob-imported so its ``registerPrimitive`` calls run at module load.
// ``eager: true`` makes Vite inline the modules into our bundle rather
// than chunk-split them, which keeps the bookmarklet a single file.
// Adding a plugin = creating ``plugins/<name>/frontend/widget.ts``;
// no edits to this file required. Path is relative to this file:
// ``frontend/src/widget.tsx`` → ``..`` → ``frontend/src/`` parent =
// ``frontend/`` → ``../..`` = repo root → + ``/plugins/...``.
import.meta.glob("../../plugins/*/frontend/widget.ts", { eager: true });

const HOST_ID = "voitta-compute-host";

function deriveBackendOrigin(): string {
  const cur = document.currentScript as HTMLScriptElement | null;
  if (cur?.src) {
    try {
      const u = new URL(cur.src);
      return `${u.protocol}//${u.host}`;
    } catch {
      /* fall through */
    }
  }
  return window.location.origin;
}

function mount(): void {
  if (document.getElementById(HOST_ID)) return;

  const host = document.createElement("div");
  host.id = HOST_ID;
  // No ``all: initial`` — the closed shadow boundary already isolates
  // styles, and some anti-injection scanners use that property as a
  // fingerprint. ``pointer-events:none`` on the host lets clicks on the
  // host page through; the drawer reverses it.
  host.style.cssText = "position:fixed;inset:0;pointer-events:none;z-index:2147483647;";
  document.documentElement.appendChild(host);

  const shadow = host.attachShadow({ mode: "closed" });

  // One combined <style> node: tokens → themes → components.
  // See styles/index.ts for the concatenation order.
  const styleEl = document.createElement("style");
  styleEl.textContent = cssText;
  shadow.appendChild(styleEl);

  const mountPoint = document.createElement("div");
  mountPoint.className = "voitta-mount";
  shadow.appendChild(mountPoint);

  // Expose the closed shadow root via a window-scoped getter so
  // browser-side primitives that need DOM access (screenshot_report)
  // can find it without a standard `host.shadowRoot` (which is null
  // by design — mode is "closed").
  type VoittaApi = { getShadowRoot: () => ShadowRoot };
  (window as unknown as { VoittaBookmarklet?: VoittaApi }).VoittaBookmarklet = {
    getShadowRoot: () => shadow,
  };

  const backendOrigin = deriveBackendOrigin();
  const root = createRoot(mountPoint);
  root.render(<App backendOrigin={backendOrigin} />);
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", mount, { once: true });
} else {
  mount();
}

export const __voitta_widget__ = true;
