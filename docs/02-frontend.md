# Frontend

## Widget bundle

A single Vite IIFE build at `frontend/dist/widget.js`. The bookmarklet injects it as a `<script>` tag; the script self-mounts on first load and is a no-op on subsequent injections (idempotent guard on the shadow root).

The bundle is also served at `/` by FastAPI for convenience, but the primary delivery mechanism is the bookmarklet.

## Shadow DOM

The widget mounts inside a `<div id="voitta-root">` that hosts an `attachShadow({mode: "open"})`. This isolates Voitta's CSS from the host page completely — no z-index wars, no font leakage, no style collisions. The Chainlit `<iframe>` for the chat UI loads inside the shadow root.

## Bookmarklet

```javascript
javascript:(function(){
  var s=document.createElement('script');
  s.src='https://127.0.0.1:12358/widget.js';
  document.head.appendChild(s);
})();
```

The bookmarklet is generated and displayed in the Settings panel. It points to the local backend. TLS is required (mkcert self-signed cert, `127.0.0.1+1.pem`).

## Thread picker

The widget header includes a conversation history dropdown. It calls the `/chainlit/project/threads` endpoint (patched on the backend to skip Chainlit's auth check) and lets the user switch between or resume past threads.

## Report pane

An `<iframe>` below the chat renders the HTML returned by report scripts. The iframe is same-origin (served from `/api/html-report?id=<slug>`). The backend injects a shim script (`_panel_shim.js`) that:
- Signals iframe readiness via a POST to `/api/report-render-events`.
- Handles `voittaAction: measure` postMessages (content height measurement).
- Handles `voittaAction: screenshot_multi` postMessages (html-to-image capture + stash upload).
- Composites Three.js canvas snapshots into the final image.

## Plugin frontend bundles

Plugins can contribute frontend code via `frontend_bundle` in their manifest. Vite globs all plugin `frontend/widget.ts` files at build time and includes them in the IIFE. Each plugin registers its own React components / primitives.

## Layout

Two modes controlled by the `layout` setting:
- `"chat-right"` (default) — report pane on the left, chat on the right.
- `"chat-left"` — reversed.
