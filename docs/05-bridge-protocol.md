# Bridge wire protocol

Three endpoints on the chat backend (`https://127.0.0.1:12358` in dev),
all under `/tools/`. They form a request/response channel between the
server-side LLM orchestrator and the browser-side primitive runner.

## 1. `GET /tools/inbox?session_id=<sid>` — server → browser

Long-lived SSE stream. Browser opens it on bookmarklet load, before
`/tools/register`.

Events:

```
event: ready
data: {"session_id": "..."}

event: call
data: {"call_id":"c-7af1","name":"get_url","args":{},"timeout_ms":15000}

event: cancel
data: {"call_id":"c-7af1"}

event: ping
data: {}
```

The browser ignores `ping`. `ready` is the first event sent on every
inbox connect (so the browser knows the bucket is live before posting
`/tools/register`).

## 2. `POST /tools/register` — capability + identification handshake

Posted **after** the inbox SSE is open. If the bucket doesn't exist yet
(race), 409 is returned and the browser retries.

```json
{
  "session_id": "012df8b8d3d13c6f10b471bf218bc39c",
  "capabilities": ["get_url", "read_dom", "read_selection", "screenshot_report",
                   "get_report_edits", "eval_js", "buffer_list", "buffer_eval"],
  "page": {
    "href": "https://drive.google.com/drive/folders/1abcDEF234ghi-jkl",
    "host": "drive.google.com",
    "origin": "https://drive.google.com",
    "pathname": "/drive/folders/1abcDEF234ghi-jkl",
    "search": "",
    "hash": "",
    "title": "MyFolder - Google Drive",
    "referrer": "https://drive.google.com/drive/my-drive",
    "loaded_at": "2026-05-02T18:42:11.024Z"
  },
  "user_agent": "Mozilla/5.0 ...",
  "viewport": {"w": 1440, "h": 900, "dpr": 2},
  "release_tag": "Version 1.32.3 (dev)"
}
```

Server replies:

```json
{
  "session_id": "012df8b8d3d13c6f10b471bf218bc39c",
  "tools": ["rag_query", "web_fetch", "drive_list_files", "drive_get_page_context",
            "list_python_storage", "buffer_eval", ...]
}
```

The returned tool list reflects host gating — only tools whose
`host_pattern` matches `page.host` (or have no pattern) appear.

Re-registration: the browser re-POSTs `register` whenever the page URL
changes (SPA navigation) so the server's view of `page.pathname` and
`page.host` stays current. Idempotent.

## 3. `POST /tools/result` — browser → server

```json
{
  "session_id": "012df8b8d3d13c6f10b471bf218bc39c",
  "call_id": "c-7af1",
  "ok": true,
  "result": {
    "href": "https://drive.google.com/drive/folders/1abcDEF234ghi-jkl",
    "pathname": "/drive/folders/1abcDEF234ghi-jkl",
    "title": "MyFolder - Google Drive"
  }
}
```

Or, on failure:

```json
{
  "session_id": "...",
  "call_id": "c-7af1",
  "ok": false,
  "error": {"kind": "invalid_selector", "message": "..."}
}
```

## Debug endpoints

- `GET /tools/sessions` — list every active session and its summary
  (capabilities, identification, pending call ids).
- `POST /tools/test/echo` — invoke a primitive on the given session and
  return its result inline.

## Cancellation

Trigger sources:

- Browser closes the chat-stream SSE (Stop button or pane close).
- Server-side timeout on a single bridge call (`timeout_ms`, default 15s).
- Inbox SSE close (full pane close).

In all cases, every still-pending Future for that session is resolved
with a `ToolBridgeError` so awaiting coroutines unwind, and best-effort
`event: cancel` notifications are emitted on the inbox so the browser
aborts in-flight primitive calls.
