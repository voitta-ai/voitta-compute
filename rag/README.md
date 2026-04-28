# rag/

Retrieval-augmented index over the project's own documentation. Indexes
everything under [`docs/`](../docs/).

## Layout

```
rag/
├── build_rag.py          ← run this to (re)build the indexes
├── README.md             ← you are here
├── .chroma/              ← persistent Chroma vector store (gitignored)
└── .bm25/
    ├── bm25/             ← serialised bm25s index
    └── manifest.json     ← canonical chunk list (file/chunk_id/offsets/text)
```

## Building / rebuilding

```bash
backend/.venv/bin/python rag/build_rag.py
```

Each run is a **full rewrite** — both `.chroma/` and `.bm25/` are deleted
and recreated. There is no incremental update path.

Tunable via flags: `--target`, `--overlap`, `--min`, `--max` (chunk-size
parameters in characters).

## Indexes

- **Dense** — Chroma persistent client, default embedder
  (`all-MiniLM-L6-v2`, 384-dim). Cosine distance.
- **Sparse** — `bm25s` with English stopwords and (optional)
  `PyStemmer`-backed stemming.

Both indexes are addressed by the same chunk ordering — the manifest's
chunk array IS the BM25 corpus order, so fusion at query time is a simple
two-lookup join.

## Hybrid query

```python
from app.tools import rag

results = rag.query(
    query_text="how does the bookmarklet authenticate?",
    top_k=5,
    dense_weight=0.9,   # 1.0 = pure semantic, 0.0 = pure BM25
)
```

Internals: each side returns a candidate pool of `max(top_k * 4, 20)`,
each score set is min-max normalised into [0, 1], fused as
`final = α · dense + (1 - α) · sparse`, then re-sorted to top `top_k`.

## When `docs/` changes

Re-run `build_rag.py`. The chat backend lazily re-opens the indexes on
the next `rag_query`, so a manual restart isn't strictly required.
