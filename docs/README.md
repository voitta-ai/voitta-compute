# Voitta Bookmarklet (Chainlit) — Docs

Project-wide prose docs. Each markdown file gets chunked and indexed
by `scripts/build_rag.py` into the `docs` RAG corpus alongside every
plugin's `docs/` tree; the LLM queries them via `rag_query`.

| File | Topic |
|---|---|
| `00-overview.md` | What this project is and what it isn't |
| `01-architecture.md` | Chainlit BE shape, agent loop, plugin layer |
| `02-frontend.md` | Bookmarklet widget, Chainlit-client, shadow DOM |
| `03-providers.md` | LLM provider abstraction (Anthropic / OpenAI / Gemini) |
| `04-tool-catalog.md` | Server + browser tools shipped today |
| `05-plugins.md` | Top-level `plugins/` tree, manifest fields, FE glob |
| `06-reports.md` | User-authored Python scripts → mountable panes |
| `07-panel-reports.md` | HoloViz / Panel reports, `ctx.three_scene`, theming, layout footguns |

## Conventions

- Files use a `NN-topic.md` numeric prefix so they sort predictably.
- One H1 per file (the title). Use H2/H3 for sections — the RAG
  chunker splits on those boundaries.
- Keep paragraphs short. The chunker merges greedily up to ~800 chars
  with ~150 char overlap; a 2000-char paragraph forces a split mid-text.
- Don't write content you haven't verified against the code. Wrong docs
  poison RAG more than missing docs do. Stub + TODO is the right move
  when in doubt.
