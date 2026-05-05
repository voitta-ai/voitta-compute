# Documentation

This folder is the agent's authoritative reference. The chat backend
exposes it via the local hybrid (dense + BM25) RAG index built by
`rag/build_rag.py`.

## Index

| File | What it covers |
| ---- | -------------- |
| [00-overview.md](00-overview.md) | What this project is, the four-layer architecture in one picture |
| [01-architecture.md](01-architecture.md) | Backend layout, request flow, tool dispatch, host gating, cancellation |
| [02-frontend.md](02-frontend.md) | Bookmarklet, Preact widget, Shadow DOM, browser primitives, theme |
| [03-providers.md](03-providers.md) | LLM provider abstraction (Anthropic / OpenAI / Gemini) |
| [04-tool-catalog.md](04-tool-catalog.md) | Tool catalogue + the "Adding a provider" recipe |
| [05-bridge-protocol.md](05-bridge-protocol.md) | Wire protocol for `/tools/inbox` / `/tools/result` / `/tools/register` |
| [07-report-scripts.md](07-report-scripts.md) | Authoring `compute` and `report` Python scripts |
| [08-drive-tools.md](08-drive-tools.md) | Google Drive provider — listing, search, download, export |

## How the agent uses these files

The chat backend ships two RAG tools — `rag_query` (hybrid search) and
`rag_get_chunk_range` (stitch contiguous chunks of one file). To add
new content, drop markdown in this folder and re-run
`rag/build_rag.py`.
