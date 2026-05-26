# Overview

Voitta is an LLM assistant injected into any web page via a
bookmarklet. The user clicks the bookmark, a closed shadow-DOM widget
mounts in the corner of the page, and the user can chat with a model
that has both **server-side tools** (running in the local FastAPI/
Chainlit backend) and **browser-side tools** (running inside the
host page via primitives in the widget).

## Two halves

- **Backend** ([`backend/`](../backend)) — a [Chainlit](https://docs.chainlit.io/)
  app driving an agent loop. The loop streams tokens, executes tool
  calls (server or browser), and feeds results back to the LLM until
  the model produces a final answer or hits the iteration cap. LLM
  provider is pluggable: Anthropic, OpenAI, and Gemini are wired today.

- **Frontend** ([`frontend/`](../frontend)) — a Vite IIFE bundle
  injected by the bookmarklet. It mounts React into a closed shadow
  root, talks to the backend via [`@chainlit/react-client`](https://www.npmjs.com/package/@chainlit/react-client),
  and exposes browser-side **primitives** the BE can call via Chainlit's
  `CopilotFunction` round-trip.

## What it's NOT

- Not a Chrome extension. The bookmarklet is a single `<script>` tag —
  no install, no manifest v3, works on any browser that supports ES2020.
- Not multi-tenant. The backend binds to `127.0.0.1:12358` and trusts
  the local user. There's no auth layer.
- Not a Slack-style chat history server. Conversation state lives in
  Chainlit's session; restarting the BE clears it.

## Where things live

```
voitta-compute/
├── backend/      FastAPI + Chainlit, agent loop, tool registry
├── frontend/     Vite IIFE bundle, React widget, primitives
├── plugins/      Host-scoped extensions (manifest + BE module + FE widget + docs + prompt)
├── docs/         This folder
├── scripts/      Dev tools (RAG builder)
└── rag/          Built RAG indexes (gitignored)
```

See [`01-architecture.md`](01-architecture.md) for the request flow,
[`05-plugins.md`](05-plugins.md) for the extension model.
