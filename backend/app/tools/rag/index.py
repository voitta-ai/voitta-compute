"""Lazy-loaded singletons for the RAG indexes (dense Chroma + sparse BM25).

The chat backend may run for a long time without anyone calling rag_query,
so we don't open Chroma or load BM25 at startup. The first call lazily
opens both, then subsequent calls reuse the same handles. Indexes are
read-only; rebuilds happen out-of-process via ``rag/build_*.py``.

Two corpora are supported, each with its own pair of index directories so
they never collide:

* ``"docs"``  — the project's own ``docs/`` markdown, built by
                 ``rag/build_rag.py``.
* ``"panel"`` — the holoviz/panel source tree at ``libs-info/panel/``,
                 built by ``rag/build_panel_rag.py``.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any


from app.config import PROJECT_ROOT

# In source-checkout mode PROJECT_ROOT is the repo root, so rag/ already
# exists with the dev's pre-built indexes. In packaged-.app mode
# PROJECT_ROOT is the user data dir (writable) and rag/ is created here
# at first-launch by the installer. Either way, rag_query reads from
# the same place — keeps the runtime path resolution flat.
RAG_DIR = PROJECT_ROOT / "rag"


def docs_source_dir() -> Path:
    """Return the read-only docs/ directory the indexer should walk.

    Source-checkout: ``<repo>/docs/``. Packaged .app: the docs are
    staged into ``src/voitta/resources/docs/`` by ``build_app.sh``,
    which briefcase auto-includes as package data; we resolve via
    importing ``voitta``.
    """
    candidate = PROJECT_ROOT / "docs"
    if candidate.is_dir():
        return candidate
    try:
        import voitta
        bundled = Path(voitta.__file__).resolve().parent / "resources" / "docs"
        if bundled.is_dir():
            return bundled
    except ImportError:
        pass
    return Path(__file__).resolve().parents[4] / "docs"


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
        description="this project's own docs/ markdown (overview, architecture, providers, tool catalogue, bridge protocol)",
    ),
    "panel": CorpusConfig(
        name="panel",
        chroma_dir=RAG_DIR / ".chroma_panel",
        bm25_dir=RAG_DIR / ".bm25_panel",
        collection_name="panel_source",
        description="the holoviz/panel library source tree at libs-info/panel/ — Python source, official docs, and examples",
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
    manifest_path = cfg.bm25_dir / "manifest.json"
    if not cfg.chroma_dir.exists() or not cfg.bm25_dir.exists() or not manifest_path.exists():
        builder = "build_rag.py" if cfg.name == "docs" else "build_panel_rag.py"
        raise RagNotBuilt(
            f"RAG index for corpus {cfg.name!r} not built. "
            f"Run: python {RAG_DIR / builder}"
        )


def load(corpus: str = DEFAULT_CORPUS) -> State:
    """Open Chroma + bm25s indexes for *corpus* (idempotent, thread-safe)."""

    cfg = _resolve(corpus)
    with _lock:
        st = _states.get(corpus)
        if st is not None:
            return st
        _ensure_built(cfg)
        # Heavy imports here.
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
    """Diagnostic for /health-style routes.

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
