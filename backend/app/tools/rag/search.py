"""Hybrid (dense + sparse) search over the RAG corpora.

Score fusion:

  final = dense_weight * dense_norm + (1 - dense_weight) * sparse_norm

where each score set is normalised to [0, 1] via min-max before fusion.
Defaults to ``dense_weight = 0.9`` (semantic-leaning); drop to ~0.2 to
prefer exact-token matches.

One corpus is shipped today, ``"docs"``, indexing this project's
``docs/`` and every plugin's ``plugins/<name>/docs/`` tree.
"""

from __future__ import annotations

from app.tools.rag.index import DEFAULT_CORPUS, State, load


# ---- query ---------------------------------------------------------------


def query(
    query_text: str,
    top_k: int,
    dense_weight: float,
    corpus: str = DEFAULT_CORPUS,
) -> list[dict]:
    if not query_text or not query_text.strip():
        return []
    dense_weight = max(0.0, min(1.0, dense_weight))
    top_k = max(1, min(50, top_k))
    pool = max(top_k * 4, 20)

    st = load(corpus)

    dense_scores, dense_records = _dense_pool(st, query_text, pool, dense_weight)
    sparse_scores = _sparse_pool(st, query_text, pool, dense_weight)

    nd = _normalise(dense_scores)
    ns = _normalise(sparse_scores)
    fused: dict[tuple[str, int], float] = {}
    for key in set(nd) | set(ns):
        fused[key] = dense_weight * nd.get(key, 0.0) + (1.0 - dense_weight) * ns.get(key, 0.0)

    ranked = sorted(fused.items(), key=lambda kv: kv[1], reverse=True)[:top_k]

    out: list[dict] = []
    for (file, chunk_id), score in ranked:
        rec = dense_records.get((file, chunk_id))
        if rec is None:
            chunk = st.chunk_index.get((file, chunk_id))
            if chunk is None:
                continue
            rec = {
                "file": chunk["file"],
                "chunk_id": chunk["chunk_id"],
                "text": chunk["text"],
                "char_start": chunk["char_start"],
                "char_end": chunk["char_end"],
            }
        # Carry through any extra metadata the chunk recorded — code-
        # corpus chunks add repo/path/folder/lang/kind/symbol.
        chunk = st.chunk_index.get((file, chunk_id), {})
        extra = {}
        for k in ("kind", "symbol", "repo", "path", "folder", "lang"):
            v = chunk.get(k)
            if v not in (None, "", []):
                extra[k] = v
        out.append(
            {
                **rec,
                **extra,
                "corpus": st.corpus.name,
                "score": round(score, 6),
                "dense_score": round(nd.get((file, chunk_id), 0.0), 6),
                "sparse_score": round(ns.get((file, chunk_id), 0.0), 6),
                "total_chunks_in_file": len(st.file_chunks.get(file, [])),
            }
        )
    return out


def _dense_pool(
    st: State, query_text: str, pool: int, dense_weight: float
) -> tuple[dict[tuple[str, int], float], dict[tuple[str, int], dict]]:
    if dense_weight <= 0:
        return {}, {}
    res = st.chroma_collection.query(
        query_texts=[query_text],
        n_results=min(pool, st.chunk_count or 1),
        include=["metadatas", "documents", "distances"],
    )
    ids = res.get("ids", [[]])[0] or []
    metas = res.get("metadatas", [[]])[0] or []
    docs = res.get("documents", [[]])[0] or []
    dists = res.get("distances", [[]])[0] or []
    scores: dict[tuple[str, int], float] = {}
    records: dict[tuple[str, int], dict] = {}
    for i, _id in enumerate(ids):
        meta = metas[i] or {}
        f = meta.get("file")
        cid = meta.get("chunk_id")
        if f is None or cid is None:
            continue
        # Chroma's cosine distance ∈ [0, 2]. Convert to similarity ∈ [-1, 1]
        # via 1 - distance, then clamp to [0, 1] for safety.
        dist = float(dists[i] if i < len(dists) else 0.0)
        sim = max(0.0, 1.0 - dist)
        scores[(f, int(cid))] = sim
        records[(f, int(cid))] = {
            "file": f,
            "chunk_id": int(cid),
            "text": docs[i] if i < len(docs) else "",
            "char_start": meta.get("char_start"),
            "char_end": meta.get("char_end"),
        }
    return scores, records


def _sparse_pool(
    st: State, query_text: str, pool: int, dense_weight: float
) -> dict[tuple[str, int], float]:
    if dense_weight >= 1.0:
        return {}
    import bm25s

    tokens = bm25s.tokenize(
        [query_text], stopwords="en", stemmer=st.bm25_stemmer, show_progress=False
    )
    idx_arr, score_arr = st.bm25_retriever.retrieve(
        tokens, k=min(pool, st.chunk_count or 1), show_progress=False
    )
    scores: dict[tuple[str, int], float] = {}
    for j in range(idx_arr.shape[1]):
        i = int(idx_arr[0, j])
        score = float(score_arr[0, j])
        chunk = _chunk_at_corpus_index(st, i)
        if chunk is None:
            continue
        scores[(chunk["file"], chunk["chunk_id"])] = score
    return scores


def _normalise(
    scores: dict[tuple[str, int], float],
) -> dict[tuple[str, int], float]:
    if not scores:
        return {}
    vals = list(scores.values())
    lo, hi = min(vals), max(vals)
    if hi - lo < 1e-9:
        return {k: 1.0 for k in scores}
    return {k: (v - lo) / (hi - lo) for k, v in scores.items()}


def _chunk_at_corpus_index(st: State, i: int) -> dict | None:
    if i < 0 or i >= len(st.bm25_corpus_texts):
        return None
    text = st.bm25_corpus_texts[i]
    for key, chunk in st.chunk_index.items():
        if chunk["text"] == text and st.chunk_index[key] is chunk:
            return chunk
    return None


# ---- get_range -----------------------------------------------------------


def get_range(
    file: str,
    first: int,
    last: int,
    max_bytes: int = 50_000,
    corpus: str = DEFAULT_CORPUS,
) -> dict:
    """Return contiguous text spanning chunks [first..last] of *file* in *corpus*.

    Adjacent chunks may overlap (md/rst); we de-duplicate by trusting the
    recorded char offsets. Code chunks have no overlap by construction.
    """

    st = load(corpus)
    chunks = st.file_chunks.get(file)
    if chunks is None:
        return {
            "ok": False,
            "error": "unknown_file",
            "corpus": corpus,
            "file": file,
            "files_count": len(st.files),
        }
    n = len(chunks)
    if first < 0 or last < 0 or last < first:
        return {"ok": False, "error": "invalid_range", "first": first, "last": last}
    if first >= n:
        return {"ok": False, "error": "out_of_range", "first": first, "total_chunks_in_file": n}
    last = min(last, n - 1)
    span = chunks[first : last + 1]

    out_text = span[0]["text"]
    for prev, cur in zip(span, span[1:]):
        overlap_chars = max(0, prev["char_end"] - cur["char_start"])
        out_text += cur["text"][overlap_chars:] if overlap_chars < len(cur["text"]) else ""

    truncated = False
    if len(out_text) > max_bytes:
        out_text = out_text[:max_bytes]
        truncated = True

    return {
        "ok": True,
        "corpus": corpus,
        "file": file,
        "first_chunk": first,
        "last_chunk": last,
        "total_chunks_in_file": n,
        "char_start": span[0]["char_start"],
        "char_end": span[-1]["char_end"],
        "text": out_text,
        "bytes": len(out_text),
        "truncated": truncated,
    }
