# browser_eval — JavaScript in the user's tab

`browser_eval(js, await_ms?)` executes arbitrary JavaScript in the user's currently-bookmarklet'd browser tab and returns the result.

## How it works

The tool is `side="hybrid"`: the server-side handler calls `call_browser("eval_js", ...)`, which uses Chainlit's `cl.CopilotFunction.acall()` to round-trip through the widget's `call_fn` socket event. The widget runs the JS in the page origin and returns the result back through the socket.

## What the script can access

The JS body runs in the page's origin with full access to:
- `document` / DOM
- `localStorage`, `sessionStorage`
- `document.cookie` (non-HttpOnly cookies)
- `fetch` with the page's credentials (session cookies, auth headers)
- `window` globals (frameworks, analytics objects, etc.)
- `performance` APIs

Top-level `await` is supported — the body is wrapped in an `async function`.

## Return value

The script must `return` the value it wants the model to receive.

```javascript
// Example: return all product names from a page
return Array.from(document.querySelectorAll('.product-name')).map(el => el.textContent.trim());
```

Success response:
```json
{"ok": true, "result": [...], "logs": [{"level": "log", "args": [...]}], "ms": 42}
```

Error response (script threw):
```json
{"ok": false, "error": "eval_threw", "message": "...", "stack": "...", "logs": [...], "ms": 12}
```

Transport error (no active tab):
```json
{"ok": false, "error": "no_session", "message": "..."}
```

Console output (`console.log/warn/error`) is always captured into `logs[]`.

## Timeout

`await_ms` — hard timeout in milliseconds. Default 30000, max 120000. If the script hasn't returned by then, the tool returns an error.

## Limitations

- Only works when the bookmarklet is active on the target tab. If the user has navigated away or the widget was injected on a different tab, the call will fail with `no_session` or `not_available`.
- Runs in the page origin — cross-origin iframes are not accessible.
- HttpOnly cookies are not readable (browser security).

## Use cases

- Scraping structured data from any page without a dedicated plugin.
- Reading auth tokens or session state from `localStorage`.
- Injecting content or triggering UI actions (`document.querySelector('button').click()`).
- Inspecting framework state (`window.__vue_app__`, `window.React`, etc.).
- Measuring layout or performance metrics.

## When to use a plugin instead

If you need to scrape the same site repeatedly with a stable result shape, write a plugin tool (a `ToolSpec` registered via `python_module`). `browser_eval` is the escape hatch when no purpose-built primitive exists.
