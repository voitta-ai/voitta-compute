# Frontend

The Preact widget mounts in a Shadow DOM, opens the bridge inbox, and
talks to the FastAPI backend at `https://127.0.0.1:12358`. Build with
`cd frontend && npm install && npm run build` (produces
`frontend/dist/widget.js`, served by the backend at `/widget.js`).

## Bookmarklet

```
javascript:(function(){var B="https://127.0.0.1:12358";if(window.__voittaBookmarkletLoaded){if(window.VoittaBookmarklet&&typeof window.VoittaBookmarklet.mount==="function")window.VoittaBookmarklet.mount();return;}window.__voittaBookmarkletLoaded=true;var s=document.createElement("script");s.src=B+"/widget.js?t="+Date.now();s.async=true;s.onerror=function(){window.__voittaBookmarkletLoaded=false;alert("Voitta bookmarklet: could not load widget from "+B+"\n\nIs the backend running? (cd backend && ./run.sh)");};document.documentElement.appendChild(s);})();
```

Readable source lives in
[`bookmarklet/bookmarklet.js`](../bookmarklet/bookmarklet.js).

## Widget responsibilities

A single IIFE produced by Vite, loaded once per page. On load:

1. Generate a 128-bit hex `session_id`.
2. Open `EventSource("/tools/inbox?session_id=<sid>")`.
3. POST `/tools/register` with capabilities + page identification (URL,
   title, viewport).
4. Mount the chat UI inside a Shadow DOM (so host-page CSS can't bleed
   in either direction).
5. Listen for inbox events:
   - `event: call` → run a primitive, POST `/tools/result`.
   - `event: cancel` → abort the in-flight primitive.
   - `event: ping` → ignore.

## Browser primitives

Live in [`frontend/src/lib/primitives.ts`](../frontend/src/lib/primitives.ts)
and [`frontend/src/lib/primitives-buffers.ts`](../frontend/src/lib/primitives-buffers.ts).
Each registers itself via `registerPrimitive` as a side-effect of
import.

| Primitive | Purpose |
| --------- | ----- |
| `get_url` | `location.{href, pathname, search, hash}` + `document.title` |
| `read_dom` | `document.querySelector(selector)`, capped at 200 KB |
| `read_selection` | `window.getSelection().toString()` |
| `screenshot_report` | Rasterise the active HoloViz Panel iframe (full scrollHeight) |
| `get_report_edits` | Read live drag/resize state out of the editable report iframe |
| `eval_js` | Sandboxed `new AsyncFunction(...)` evaluator with console capture |
| `buffer_*` | Buffer registry (`list`, `get_summary`, `delete`, `clear`, `query_curves`) |
| `plot` / `plot_xy_from_buffer` / `plot_bars_from_buffer` | Chart.js renderers |
| `buffer_eval` | Sandboxed Web Worker, operates on a buffer, returns JSON + draw commands |
| `show_report` | Open a HoloViz Panel iframe in the report pane |
| `get_page_dump` | URL + title + full outerHTML (uncapped; CLI back-channel only) |

## Theme

Visual tokens live in [`frontend/src/theme.css`](../frontend/src/theme.css).
`styles.css` consumes them via `var(--voitta-…)`. To rebrand the widget,
swap the values in `theme.css` and rebuild — no other changes needed.

## SPA navigation

The widget is loaded once per page. If the host is a SPA that swaps
routes without reloading, the bridge re-POSTs `/tools/register` with
the updated `page.pathname` so the server's view stays current. Each
user message also gets a `(current url: …)` prefix before being sent.

## Chat-stream events

| Event | Data | When |
| ----- | ---- | ---- |
| `start` | `{model, provider, tools}` | First iteration begins |
| `delta` | `{text}` | Text from the model |
| `tool_use_start` | `{id, name}` | Model emitted a `tool_use` block |
| `tool_use_end` | `{id, name, ok, latency_ms, error?, input, result_preview}` | Tool resolved |
| `done` | `{stop_reason, usage, iterations}` | Final iteration finished with `end_turn` |
| `error` | `{message, type}` | Fatal error in the orchestrator or LLM |

`result_preview` is a truncated string (≤ 4 KB) for the inline
`<details>` in the UI; the full result still goes back to the model.
