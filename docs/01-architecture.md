# Architecture

## Backend layout

```
backend/
├── pyproject.toml
├── run.sh                     ← starts uvicorn (HTTPS via mkcert)
├── certs/                     ← mkcert-generated cert+key (gitignored)
├── tests/                     ← pytest: registry, providers, drive context, config
└── app/
    ├── main.py                ← FastAPI app, CORS, /widget.js, /health, OAuth callbacks
    ├── config.py              ← server constants + the Voitta system prompt
    ├── routes/
    │   ├── chat.py            ← provider-agnostic tool-use loop, SSE to browser
    │   ├── tools.py           ← bridge endpoints (/tools/inbox, /tools/result, /tools/register)
    │   ├── providers.py       ← LLM provider config readback for the Settings panel
    │   └── cli.py             ← localhost-only back-channel for external automation
    ├── services/
    │   ├── llm/               ← Anthropic / OpenAI / Gemini adapters
    │   ├── google_oauth.py    ← Drive OAuth state + token refresh
    │   ├── panel_app.py       ← Bokeh+Panel app factory mounted under /panel/reports
    │   ├── panel_renderer.py  ← LLM-authored report scripts → Panel layout
    │   ├── python_storage.py  ← server-side snapshot directory ops
    │   ├── render_events.py   ← /api/report-render-events bus
    │   ├── scripts.py         ← compute + report script storage / runner
    │   └── user_settings.py   ← ~/.config/voitta-bookmarklet/settings.json I/O
    ├── bridge/
    │   ├── bus.py             ← in-memory ToolBridge (sessions + futures + queues)
    │   └── models.py          ← wire types for /tools/* routes
    └── tools/
        ├── registry.py        ← LLM-facing ToolRegistry (host gating, visibility check)
        ├── browser.py         ← thin wrapper over bridge.call()
        ├── rag/               ← Chroma + BM25 index loader + query/range
        ├── domain/            ← provider-agnostic tools (rag, web, screenshot, …)
        └── providers/         ← provider-specific tools (drive today, more later)
            └── drive/
                ├── context.py ← drive_get_page_context, host-gated
                └── tools.py   ← drive_list_files / search / get / download / export
```

## Request flow (one chat turn)

1. Browser POSTs `/api/chat/stream` with `{messages, session_id, provider?, model?}`.
2. Server opens an SSE response. The first event is `start`.
3. Server picks the provider and runs the **tool-use loop**:
   - Build the normalised request: messages + system + tool list filtered
     by host (see "Host-gated tool exposure" below).
   - Call `provider.create_message(req)`.
   - Emit one `delta` event per text block.
   - If `stop_reason == "tool_use"`: dispatch every `tool_use` block in
     parallel through the registry, append `tool_result` blocks, loop.
   - Otherwise emit `done` and close.
4. Hard cap: `MAX_TOOL_ITERATIONS = 25` per turn (configurable, ceilinged
   server-side).

### Tool dispatch — single registry, two execution sides

The registry holds `ToolSpec(name, description, input_schema, handler,
side, host_pattern?, visibility_check?)`.

- `side="server"` — handler runs entirely in Python.
- `side="hybrid"` — handler is Python but composes browser primitives
  via `app.tools.browser.call_browser`.

The LLM sees a single flat tool list and doesn't know which side runs
each tool.

### Host-gated tool exposure

Provider page-context tools (e.g. `drive_get_page_context`) only make
sense on the matching host. They declare `host_pattern="drive.google.com"`;
the chat route filters them out when the bookmarklet's reported
`page.host` doesn't match (strict suffix rule — see
`ToolRegistry.visible_for_host`).

Action tools (`drive_list_files`, etc.) are NOT host-gated — the LLM
can use them from any page once the user has connected the provider's
OAuth in Settings (`visibility_check=google_oauth.is_connected`).

## Browser primitives

Generic primitives exposed via the bridge. The browser implements them;
hybrid Python tools call them.

| Primitive | Purpose |
| --------- | ---- |
| `get_url` | URL/title of the active page |
| `read_dom` | `querySelector`-style read with a 200 KB cap |
| `read_selection` | The user's current text selection |
| `screenshot_report` | Rasterise the active HoloViz Panel iframe |
| `get_report_edits` | Read live drag/resize state out of the editable report iframe |
| `eval_js` | Sandboxed `new AsyncFunction(...)` evaluator with console capture |
| `get_page_dump` | URL + title + full outerHTML (CLI back-channel only) |

Provider-specific primitives (Drive download, etc.) live with their
provider package, not in the global primitives surface.

## Provider abstraction (LLM)

Each LLM provider implements:

```python
class Provider(Protocol):
    id: Literal["anthropic", "openai", "gemini"]
    async def create_message(self, req: NormalisedRequest) -> NormalisedResponse: ...
```

`NormalisedRequest` / `NormalisedResponse` use the Anthropic block shape
as the canonical interchange. Each adapter converts in/out. See
[03-providers.md](03-providers.md).

## RAG (local, hybrid)

Dense (Chroma) + sparse (bm25s), fused with
`final = w * dense + (1 - w) * sparse`. Two corpora:

- `docs` — this project's own `docs/`.
- `panel` — the HoloViz Panel source under `libs-info/panel/`.

Indexes are built out-of-process by `rag/build_rag.py` and
`rag/build_panel_rag.py`, lazy-loaded on first query.

Two server-side tools expose it: `rag_query`, `rag_get_chunk_range`.

## Cancellation chain

User clicks Stop, or closes the pane mid-turn:

1. Browser closes the chat-stream SSE; aborts every in-flight primitive.
2. Server's chat handler raises `asyncio.CancelledError` through the
   orchestrator.
3. Orchestrator cancels the active provider call, then for every still-
   pending bridge `call_id`:
   - emits `event: cancel { call_id }` on the inbox,
   - resolves the corresponding `Future` so the awaiting coroutine unwinds.

Every pending Future is also drained on inbox SSE close (full pane close).
