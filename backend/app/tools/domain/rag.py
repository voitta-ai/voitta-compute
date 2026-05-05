"""Server-side RAG tools — hybrid search and chunk-range stitcher.

Both run entirely in the chat backend against the persisted Chroma + BM25
indexes built by ``rag/build_rag.py`` (corpus ``"docs"``) or
``rag/build_panel_rag.py`` (corpus ``"panel"``). No browser primitive
involved.

The LLM picks which corpus to search against on each call via the
``corpus`` parameter — see the tool descriptions for guidance.
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
        "Which corpus to search. "
        "'docs' (default) = this project's own docs/ — overview, "
        "architecture, frontend, providers, tool catalogue, bridge "
        "protocol. "
        "'panel' = the holoviz/panel library at libs-info/panel/ — full "
        "Python source, official documentation, and usage examples. Pick "
        "'panel' when the question is about the Panel framework's API, "
        "widget classes, layouts, reactive params, server/Bokeh internals, "
        "or how Panel components are implemented; pick 'docs' for anything "
        "about THIS bookmarklet project."
    ),
}


# ---- rag_query -----------------------------------------------------------


async def _rag_query(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    corpus = str(args.get("corpus") or rag_index.DEFAULT_CORPUS)
    try:
        results = rag_index.query(
            query_text=str(args.get("query") or ""),
            top_k=int(args.get("top_k") or 5),
            dense_weight=float(
                args.get("dense_weight") if args.get("dense_weight") is not None else 0.9
            ),
            corpus=corpus,
        )
    except rag_index.UnknownCorpus as exc:
        return {"ok": False, "error": "unknown_corpus", "message": str(exc)}
    except rag_index.RagNotBuilt as exc:
        return {"ok": False, "error": "rag_not_built", "corpus": corpus, "message": str(exc)}
    return {"ok": True, "corpus": corpus, "count": len(results), "results": results}


registry.register(
    ToolSpec(
        name="rag_query",
        description=(
            "Hybrid dense + sparse search over a documentation/source corpus. "
            "Use the `corpus` argument to choose which body of knowledge to "
            "query:\n"
            "\n"
            "  • 'docs' (default) — THIS project's own documentation in docs/ "
            "(overview, architecture, frontend, providers, tool catalogue, "
            "bridge protocol). Use this for any question about how this "
            "bookmarklet, its backend, the tool bridge, or its providers "
            "work.\n"
            "  • 'panel' — the holoviz/panel library source tree at "
            "libs-info/panel/. Indexes Panel's full Python source (AST-chunked "
            "per function/class/method, with file path and qualified symbol "
            "name in each chunk header), its official documentation, and "
            "examples. Use this when you need to look up a Panel API, "
            "understand a widget/layout class internals, see how Panel "
            "exposes Bokeh, find a method signature, or read example "
            "code.\n"
            "\n"
            "Each result includes the source file, chunk_id, fused score, "
            "individual dense/sparse scores, the corpus it came from, the "
            "total number of chunks in the same file (so you can decide "
            "what neighbouring chunks to fetch via rag_get_chunk_range), "
            "and — for the panel corpus — `kind` (module|function|"
            "class_header|method|class|doc) and `symbol` (e.g. "
            "'Button.on_click').\n"
            "\n"
            "dense_weight is a 0..1 dial: 1.0 = pure semantic, 0.0 = pure "
            "keyword/BM25, 0.5 = balanced. Default 0.9 (semantic-leaning). "
            "Drop to ~0.2 when hunting an exact identifier (e.g. a Panel "
            "class or method name in the panel corpus)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "top_k": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5},
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


async def _rag_get_chunk_range(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    corpus = str(args.get("corpus") or rag_index.DEFAULT_CORPUS)
    try:
        return rag_index.get_range(
            file=str(args["file"]),
            first=int(args["first_chunk"]),
            last=int(args["last_chunk"]),
            max_bytes=int(args.get("max_bytes") or 50_000),
            corpus=corpus,
        )
    except rag_index.UnknownCorpus as exc:
        return {"ok": False, "error": "unknown_corpus", "message": str(exc)}
    except rag_index.RagNotBuilt as exc:
        return {"ok": False, "error": "rag_not_built", "corpus": corpus, "message": str(exc)}


registry.register(
    ToolSpec(
        name="rag_get_chunk_range",
        description=(
            "Return a contiguous slice of source text spanning chunks "
            "[first_chunk..last_chunk] of a given file in the chosen corpus. "
            "Use after rag_query to expand a hit's surrounding context — for "
            "example, to read a whole class after rag_query surfaced one of "
            "its methods, or to follow a doc section's continuation. "
            "Adjacent doc chunks overlap by ~150 chars and the returned text "
            "de-duplicates the overlap; code chunks (in the 'panel' corpus) "
            "do not overlap. Capped at 50 KB by default. The `corpus` "
            "argument MUST match the corpus the file came from in rag_query "
            "(file paths in 'docs' are relative to docs/, file paths in "
            "'panel' are relative to libs-info/panel/)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "file": {
                    "type": "string",
                    "description": (
                        "Path relative to the corpus root. For corpus='docs' "
                        "this is e.g. '04-tool-catalog.md'; for corpus='panel' "
                        "it's e.g. 'panel/widgets/button.py' or "
                        "'doc/api/index.rst'."
                    ),
                },
                "first_chunk": {"type": "integer", "minimum": 0},
                "last_chunk": {"type": "integer", "minimum": 0},
                "max_bytes": {"type": "integer", "minimum": 1024, "default": 50000},
                "corpus": _CORPUS_SCHEMA,
            },
            "required": ["file", "first_chunk", "last_chunk"],
            "additionalProperties": False,
        },
        handler=_rag_get_chunk_range,
        side="server",
    )
)
