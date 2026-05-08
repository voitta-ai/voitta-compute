# Overview

Voitta is a right-side chat pane you inject into any web page with a
one-click bookmarklet. The pane talks to a local FastAPI backend that
runs the LLM tool-use loop. Server-side **provider** modules turn the
chat into action (Google Drive today, more on the roadmap).

## Project pillars

- **Run anywhere.** Click the bookmark on any HTTPS page; the widget
  mounts itself in a Shadow DOM and never leaks styles either way.
- **Server-side tools.** The browser exposes a small set of generic
  primitives (URL, DOM, screenshot, sandboxed JS eval). Domain logic
  lives in Python so the LLM gets one place to reason about it and
  user data never has to round-trip through the LLM context.
- **Provider abstraction.** Adding a data source means adding a
  Python package under `app/tools/providers/<name>/`. Tools register
  themselves with `host_pattern` (so they only show on the matching
  site) and an OAuth-aware `visibility_check` (so they only show
  once the user has connected the provider in Settings).
- **Reports as Python.** Persistent dashboards live in
  `scripts/reports/<name>/script.py` and render into a HoloViz Panel
  iframe pane next to the chat. The LLM can author them via
  `define_report(name, code)` and refresh them via
  `show_holoviz_report(name)`.
- **Hybrid local RAG.** A Chroma + BM25 index over `docs/` (this
  project's own docs) and `libs-info/panel/` (HoloViz Panel source +
  examples) gives the LLM authoritative context for answering
  questions and writing report code.

## Four layers

```
┌─────────────────────────────────────────────────────────────────────┐
│  Server (Python / FastAPI, this repo)                               │
│  ┌──────────────────┐    ┌─────────────────────┐                    │
│  │  Domain tools    │    │  LLM orchestrator   │                    │
│  │  + providers/    │◀──▶│  (Anthropic /       │                    │
│  │  (RAG, web,      │    │   OpenAI / Gemini)  │                    │
│  │   Drive, …)      │    └──────────┬──────────┘                    │
│  └──────────────────┘               │                               │
│         tool calls dispatched to    │                               │
│         server-side OR browser-side │                               │
│                                     ▼                               │
│  ┌──────────────────────────────────────────────┐                   │
│  │  Browser-tool gateway                        │                   │
│  │  • SSE inbox: server → browser requests      │                   │
│  │  • POST result: browser → server             │                   │
│  └────────────────────┬─────────────────────────┘                   │
└───────────────────────┼─────────────────────────────────────────────┘
                        │ HTTPS (127.0.0.1:12358 in dev)
                        ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Browser pane (Preact, injected via bookmarklet)                    │
│  ┌──────────────────┐   ┌────────────────────────┐                  │
│  │  Chat UI         │   │  Browser tool runner   │                  │
│  └──────────────────┘   │  • generic primitives  │                  │
│                         │  • report iframe       │                  │
│                         │  • buffers + eval      │                  │
│                         └────────────────────────┘                  │
└─────────────────────────────────────────────────────────────────────┘
```

## Tech stack at a glance

| Layer | Stack |
| ----- | ----- |
| Backend | Python 3.11+, FastAPI, sse-starlette |
| LLM | `anthropic`, `openai`, `google-genai` SDKs, behind a `Provider` protocol |
| RAG | Local `chromadb` (dense) + `bm25s` (sparse), hybrid score fusion |
| Frontend | Vite + Preact + TypeScript, single IIFE in a Shadow DOM |
| Bookmarklet | One-line URL that loads `widget.js` from `https://127.0.0.1:12358` |
| Reports | HoloViz Panel rendered server-side, iframed into the chat pane |

## Where to read next

- Backend architecture? [01-architecture.md](01-architecture.md)
- Front-end widget? [02-frontend.md](02-frontend.md)
- Tool catalogue (what the LLM can do)? [04-tool-catalog.md](04-tool-catalog.md)
- Adding a new LLM provider? [03-providers.md](03-providers.md)
- Adding a new data provider (next to Drive)? See "Adding a provider"
  in [04-tool-catalog.md](04-tool-catalog.md).
- Bridge protocol? [05-bridge-protocol.md](05-bridge-protocol.md)
- Authoring `compute` / `report` scripts? [07-report-scripts.md](07-report-scripts.md)
- Drive specifics? [08-drive-tools.md](08-drive-tools.md)
