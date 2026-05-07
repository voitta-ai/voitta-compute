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
| [09-panel-threejs-reports.md](09-panel-threejs-reports.md) | Interactive 3D / WebGL reports — iframe layout, postMessage relay, geometry pipeline |
| [10-a4db-files.md](10-a4db-files.md) | `.a4db` (ANSA / Animator crash database) — HDF5 schema, topology gotchas, rogue-element culling, animation pipeline |
| [12-dat-files.md](12-dat-files.md) | SoundCheck `.dat` / `.wfm` / `.res` (Listen, Inc.) — version dispatch, title parsing, end-to-end curves pipeline |

## How the agent uses these files

The chat backend ships two RAG tools — `rag_query` (hybrid search) and
`rag_get_chunk_range` (stitch contiguous chunks of one file). To add
new content, drop markdown in this folder and re-run
`rag/build_rag.py`.
