# Frontend

A single Vite IIFE bundle at `dist/widget.js`. The bookmarklet
injects this as a `<script>` tag; the script self-mounts on first
load and is a no-op on subsequent loads.

## Mount sequence

1. [`widget.tsx`](../frontend/src/widget.tsx) is the entry. It runs at
   the bottom of itself, either immediately or on `DOMContentLoaded`.
2. Creates a host `<div>` at `z-index: 2147483647` with
   `pointer-events: none` (clicks pass through to the host page) and
   `attachShadow({ mode: "closed" })`. Closed shadow defeats both CSS
   leakage and most anti-injection scanners.
3. Injects the bundled stylesheet into the shadow root.
4. Exposes `window.VoittaBookmarklet.getShadowRoot()` so primitives that
   need DOM access (e.g. `screenshot_report`) can find the (closed)
   shadow.
5. Renders `<App>` via `react-dom/client.createRoot`.

## React tree

```
<ChainlitContext.Provider value={ChainlitAPI(`${origin}/chainlit`, "webapp")}>
  <RecoilRoot>
    <Drawer>
      <ChatPane>      ← @chainlit/react-client messages, streaming
      <SettingsView>  ← provider/api-key/model selection
      <ReportPane>    ← script reports (matplotlib/plotly/react)
    </Drawer>
  </RecoilRoot>
</ChainlitContext.Provider>
```

[`@chainlit/react-client`](https://docs.chainlit.io/copilot/customize)
owns the Socket.IO connection, message streaming, and the `call_fn`
round-trip protocol. We don't reinvent any of that.

## Browser-side primitives

The BE invokes browser tools via `cl.CopilotFunction(name, args).acall()`,
which round-trips through the Chainlit socket and lands in
[`CallFnRouter.tsx`](../frontend/src/lib/CallFnRouter.tsx). The router
looks up `name` in the `primitives` map from
[`primitives.ts`](../frontend/src/lib/primitives.ts) and ACKs with the
result.

Two kinds of primitives:

- **Pure functions** in [`primitives.ts`](../frontend/src/lib/primitives.ts).
  Shape: `(args: Record<string, unknown>) => Promise<unknown> | unknown`.
- **React-stateful primitives** inside `CallFnRouter.tsx` itself, so
  they can use Recoil setters in scope (e.g. `show_report`,
  `close_report`).

## Plugin frontends

`widget.tsx` calls `import.meta.glob("../../../plugins/*/frontend/widget.ts", { eager: true })`.
Vite walks the sibling `plugins/` tree at build time and inlines every
plugin's `widget.ts` into the IIFE. Each plugin widget calls
`registerPrimitive(name, fn)` to add browser tools — see
[`05-plugins.md`](05-plugins.md).

## Build

```bash
cd frontend && npm run build
```

Outputs `dist/widget.js` (a multi-MB IIFE — Chainlit-client + Recoil +
ELK.js layout worker are heavy). Served by the BE at `/widget.js`.

## Bookmarklet

```js
javascript:(()=>{const s=document.createElement('script');s.src='https://127.0.0.1:12358/widget.js';document.head.appendChild(s);})();
```

That's it — no manifest, no install. Drag to bookmarks bar.
