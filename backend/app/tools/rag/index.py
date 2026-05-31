"""Lazy-loaded singletons for the RAG indexes (dense Chroma + sparse BM25).

The chat backend may run for a long time without anyone calling
``rag_query``, so we don't open Chroma or load BM25 at startup. The
first call lazily opens both, then subsequent calls reuse the same
handles. Indexes are read-only at runtime; rebuilds happen
out-of-process via ``scripts/build_rag.py``.

Phase 1 ships a single corpus, ``"docs"``. ``CORPORA`` is a dict so a
second corpus (e.g. a Panel-source corpus, a third-party-library
corpus) can be added with one entry plus a builder. The shape is
preserved for forward-compat.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import RAG_DIR


@dataclass(frozen=True)
class CorpusConfig:
    name: str
    chroma_dir: Path
    bm25_dir: Path
    collection_name: str
    description: str


CORPORA: dict[str, CorpusConfig] = {
    "docs": CorpusConfig(
        name="docs",
        chroma_dir=RAG_DIR / ".chroma",
        bm25_dir=RAG_DIR / ".bm25",
        collection_name="docs",
        description=(
            "this project's own docs/ markdown (overview, architecture, "
            "frontend, providers, tool catalogue, plugins, reports) plus "
            "every plugin's docs/ tree"
        ),
    ),
    "code": CorpusConfig(
        name="code",
        chroma_dir=RAG_DIR / ".chroma_code",
        bm25_dir=RAG_DIR / ".bm25_code",
        collection_name="code",
        description=(
            "source code of vendored libraries under lib-sources/ "
            "(elk, elkjs, jinja, three.js). "
            "Python chunks are AST-bounded (module / class / function / "
            "method); JS/TS chunks are regex-bounded at top-level "
            "function and class boundaries. Each chunk carries repo, "
            "path, file, lang, kind, and symbol metadata."
        ),
    ),
}

DEFAULT_CORPUS = "docs"


class RagNotBuilt(RuntimeError):
    pass


class UnknownCorpus(ValueError):
    pass


@dataclass
class State:
    corpus: CorpusConfig
    chroma_collection: Any
    bm25_retriever: Any
    bm25_stemmer: Any
    bm25_corpus_texts: list[str]
    chunk_index: dict[tuple[str, int], dict]
    file_chunks: dict[str, list[dict]]
    chunk_count: int
    files: list[str]


_states: dict[str, State] = {}
_lock = threading.Lock()


def _resolve(corpus: str) -> CorpusConfig:
    cfg = CORPORA.get(corpus)
    if cfg is None:
        raise UnknownCorpus(
            f"unknown corpus {corpus!r}; valid options: {sorted(CORPORA.keys())}"
        )
    return cfg


def _ensure_built(cfg: CorpusConfig) -> None:
    """Verify the three on-disk pieces an index needs: the Chroma
    persistent dir, the BM25 dir, and the BM25 manifest. If any is
    missing OR the BM25 dir is empty (the most common 'half-built'
    state — chroma rebuilt but bm25 never finished or was wiped), we
    raise a diagnostic ``RagNotBuilt`` that names which piece is
    missing and the exact rebuild command for THIS corpus."""
    manifest_path = cfg.bm25_dir / "manifest.json"
    chroma_present = cfg.chroma_dir.exists() and any(cfg.chroma_dir.iterdir()) if cfg.chroma_dir.exists() else False
    bm25_dir_present = cfg.bm25_dir.exists()
    bm25_files_present = bm25_dir_present and any(cfg.bm25_dir.iterdir())
    manifest_present = manifest_path.exists()

    if chroma_present and bm25_files_present and manifest_present:
        return

    missing: list[str] = []
    if not chroma_present:
        missing.append(f"chroma_dir={cfg.chroma_dir}")
    if not bm25_files_present:
        missing.append(f"bm25_dir={cfg.bm25_dir} (empty)" if bm25_dir_present else f"bm25_dir={cfg.bm25_dir}")
    if not manifest_present:
        missing.append(f"manifest={manifest_path}")

    fully_absent = (
        not chroma_present
        and not bm25_dir_present
        and not manifest_present
    )
    if fully_absent:
        state_desc = "never built"
    else:
        state_desc = "partial / stale — some pieces present, others missing"

    raise RagNotBuilt(
        f"RAG index for corpus {cfg.name!r} not usable ({state_desc}).\n"
        f"Missing: {', '.join(missing)}\n"
        f"Fix: python scripts/build_rag.py --corpus {cfg.name}"
    )


def load(corpus: str = DEFAULT_CORPUS) -> State:
    """Open Chroma + bm25s indexes for *corpus* (idempotent, thread-safe)."""
    cfg = _resolve(corpus)
    with _lock:
        st = _states.get(corpus)
        if st is not None:
            return st
        _ensure_built(cfg)
        # Heavy imports here so module import stays cheap when RAG
        # isn't touched.
        import bm25s
        import chromadb
        from chromadb.config import Settings

        manifest_path = cfg.bm25_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        chunks: list[dict] = manifest["chunks"]
        chunk_index: dict[tuple[str, int], dict] = {}
        file_chunks: dict[str, list[dict]] = {}
        for c in chunks:
            chunk_index[(c["file"], c["chunk_id"])] = c
            file_chunks.setdefault(c["file"], []).append(c)
        for v in file_chunks.values():
            v.sort(key=lambda x: x["chunk_id"])

        client = chromadb.PersistentClient(
            path=str(cfg.chroma_dir),
            settings=Settings(anonymized_telemetry=False),
        )
        coll = client.get_collection(name=cfg.collection_name)

        retriever = bm25s.BM25.load(str(cfg.bm25_dir / "bm25"), load_corpus=False)
        try:
            import Stemmer  # type: ignore
            stemmer = Stemmer.Stemmer("english")
        except Exception:
            stemmer = None

        corpus_texts = [c["text"] for c in chunks]

        st = State(
            corpus=cfg,
            chroma_collection=coll,
            bm25_retriever=retriever,
            bm25_stemmer=stemmer,
            bm25_corpus_texts=corpus_texts,
            chunk_index=chunk_index,
            file_chunks=file_chunks,
            chunk_count=manifest["chunk_count"],
            files=manifest["files"],
        )
        _states[corpus] = st
        return st


def index_status(corpus: str | None = None) -> dict:
    """Diagnostic for ``/health``-style routes.

    With no argument, reports status for all corpora.
    """
    if corpus is not None:
        cfg = _resolve(corpus)
        try:
            st = load(corpus)
        except RagNotBuilt as exc:
            return {"corpus": corpus, "built": False, "error": str(exc)}
        return {
            "corpus": corpus,
            "built": True,
            "chunk_count": st.chunk_count,
            "files_count": len(st.files),
            "chroma_dir": str(cfg.chroma_dir),
            "bm25_dir": str(cfg.bm25_dir),
        }
    return {name: index_status(name) for name in CORPORA}


def _reset_for_tests() -> None:
    """Drop cached State so tests with a fresh ``RAG_DIR`` re-open."""
    with _lock:
        _states.clear()
