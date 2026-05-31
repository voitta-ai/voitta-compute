"""Server-side RAG tools — hybrid search and chunk-range stitcher.

Both run entirely in the chat backend against the persisted Chroma +
BM25 indexes built by ``scripts/build_rag.py``. No browser primitive
involved.
"""

from __future__ import annotations

from typing import Any

from app.tools import rag as rag_index
from app.tools.registry import ToolCtx, ToolSpec, registry

_CORPUS_SCHEMA = {
    "type": "string",
    "enum": sorted(rag_index.CORPORA.keys()),
    "default": rag_index.DEFAULT_CORPUS,
    "description": (
        "Which corpus to search. 'docs' = project documentation (default). "
        "'code' = source code of vendored libraries under lib-sources/ "
        "(elk, elkjs, jinja, three.js)."
    ),
}


# ---- rag_query -----------------------------------------------------------


async def _rag_query(args: dict[str, Any], _ctx: ToolCtx) -> dict[str, Any]:
    corpus = str(args.get("corpus") or rag_index.DEFAULT_CORPUS)
    try:
        results = rag_index.query(
            query_text=str(args.get("query") or ""),
            top_k=int(args.get("top_k") or 5),
            dense_weight=float(
                args.get("dense_weight")
                if args.get("dense_weight") is not None
                else 0.9
            ),
            corpus=corpus,
        )
    except rag_index.UnknownCorpus as exc:
        return {"ok": False, "error": "unknown_corpus", "message": str(exc)}
    except rag_index.RagNotBuilt as exc:
        return {
            "ok": False,
            "error": "rag_not_built",
            "corpus": corpus,
            "message": str(exc),
        }
    return {"ok": True, "corpus": corpus, "count": len(results), "results": results}


registry.register(
    ToolSpec(
        name="rag_query",
        description=(
            "Hybrid dense + sparse search over a RAG corpus. Two corpora:\n"
            "  • 'docs' (default) — this project's documentation (docs/ + "
            "every plugin's docs/).\n"
            "  • 'code'           — source of vendored libraries under "
            "lib-sources/: elk, elkjs, jinja, three.js. "
            "Python is AST-chunked (module / class / function / "
            "method); JS/TS is regex-chunked at top-level boundaries.\n"
            "\n"
            "Returns ranked chunks with the fused score, the individual "
            "dense/sparse scores, and the total chunk count in the same "
            "file. For the 'code' corpus each chunk also carries: repo, "
            "path (relative to the repo root), folder, file (= repo/path), "
            "lang, kind (module / class / class_header / function / method "
            "/ code_fallback), and symbol. The 'file' field is what you "
            "feed to rag_get_chunk_range to stitch neighbouring chunks.\n"
            "\n"
            "``dense_weight`` is a 0..1 dial: 1.0 = pure semantic, 0.0 = "
            "pure keyword/BM25, 0.5 = balanced. Default 0.9 "
            "(semantic-leaning). Drop to ~0.2 when hunting an exact "
            "identifier — useful in 'code' for finding a specific symbol."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "top_k": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 20,
                    "default": 5,
                },
                "dense_weight": {
                    "type": "number",
                    "minimum": 0.0,
                    "maximum": 1.0,
                    "default": 0.9,
                },
                "corpus": _CORPUS_SCHEMA,
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        handler=_rag_query,
        side="server",
    )
)


# ---- rag_get_chunk_range -------------------------------------------------


async def _rag_get_chunk_range(args: dict[str, Any], _ctx: ToolCtx) -> dict[str, Any]:
    corpus = str(args.get("corpus") or rag_index.DEFAULT_CORPUS)
    try:
        return rag_index.get_range(
            file=str(args.get("file") or ""),
            first=int(args.get("first") or 0),
            last=int(args.get("last") or 0),
            max_bytes=int(args.get("max_bytes") or 50_000),
            corpus=corpus,
        )
    except rag_index.UnknownCorpus as exc:
        return {"ok": False, "error": "unknown_corpus", "message": str(exc)}
    except rag_index.RagNotBuilt as exc:
        return {
            "ok": False,
            "error": "rag_not_built",
            "corpus": corpus,
            "message": str(exc),
        }


registry.register(
    ToolSpec(
        name="rag_get_chunk_range",
        description=(
            "Stitch contiguous chunks [first..last] from one corpus file "
            "into a single text blob (overlap de-duplicated). Use after "
            "rag_query when one result hints there's more context in "
            "neighbouring chunks of the same file. ``max_bytes`` caps the "
            "returned text; the response sets ``truncated: true`` on cap."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "file": {"type": "string"},
                "first": {"type": "integer", "minimum": 0},
                "last": {"type": "integer", "minimum": 0},
                "max_bytes": {
                    "type": "integer",
                    "minimum": 1000,
                    "maximum": 200_000,
                    "default": 50_000,
                },
                "corpus": _CORPUS_SCHEMA,
            },
            "required": ["file", "first", "last"],
            "additionalProperties": False,
        },
        handler=_rag_get_chunk_range,
        side="server",
    )
)
