#!/usr/bin/env python3
"""Build the RAG indexes from docs/.

Produces two parallel artefacts in rag/:

* rag/.chroma/      — Chroma persistent client store; dense embeddings via the
                      default all-MiniLM-L6-v2 model.
* rag/.bm25/        — BM25 index serialised by the bm25s package, plus a
                      manifest.json with the canonical chunk list (file,
                      chunk_id, char_start, char_end, text). Both indexes
                      reference chunks by the same chunk_id, so fusion at
                      query time is just two lookups.

Each run is a full rewrite — both directories are deleted and recreated.
There is no incremental update path.

Chunking is markdown-aware: split first on H2 (``## ``) and H3 (``### ``)
boundaries, then on blank lines within sections. Chunks are merged greedily
up to ~800 characters with ~150 characters of overlap between neighbours.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

# Heavy deps imported lazily inside main() so `--help` is fast and any
# missing-package errors are surfaced in context.


REPO_ROOT = Path(__file__).resolve().parents[1]
DOCS_DIR = REPO_ROOT / "docs"
RAG_DIR = REPO_ROOT / "rag"
CHROMA_DIR = RAG_DIR / ".chroma"
BM25_DIR = RAG_DIR / ".bm25"
MANIFEST_PATH = BM25_DIR / "manifest.json"
COLLECTION_NAME = "docs"

DEFAULT_TARGET = 800
DEFAULT_OVERLAP = 150
DEFAULT_MIN = 200
HARD_MAX = 1500  # never exceed this even if a paragraph is huge


# ---- chunking ------------------------------------------------------------


@dataclass
class Chunk:
    file: str  # relative to docs/
    chunk_id: int  # 0-based, dense within a file
    char_start: int
    char_end: int
    text: str


def split_into_blocks(text: str) -> list[tuple[int, int, str]]:
    """Recursive markdown-aware split into atomic *blocks*.

    Each block is a (start, end, text) triple where start/end are character
    offsets into the original text. The blocks are then merged greedily into
    chunks (see ``merge_blocks``). The block layer never splits inside a
    paragraph or table — that responsibility falls to the merger when a
    single block already exceeds the hard max.
    """

    # Pass 1: split on top-level headings (H1/H2). This keeps each section
    # logically grouped before further refinement.
    out: list[tuple[int, int, str]] = []
    pos = 0
    section_re = re.compile(r"^(#{1,3} .*)$", re.MULTILINE)
    matches = list(section_re.finditer(text))
    boundaries = [m.start() for m in matches] + [len(text)]
    if not matches or boundaries[0] > 0:
        boundaries = [0, *boundaries]
    boundaries = sorted(set(boundaries))

    for a, b in zip(boundaries, boundaries[1:]):
        section = text[a:b]
        if not section.strip():
            continue
        # Pass 2 within section: split on blank lines (paragraph / table /
        # code-block separators).
        offset = a
        for para in re.split(r"(\n\s*\n)", section):
            if not para:
                continue
            seg = (offset, offset + len(para), para)
            offset += len(para)
            if para.strip():
                out.append(seg)
    return out


def merge_blocks(
    blocks: list[tuple[int, int, str]],
    *,
    target: int,
    overlap: int,
    minimum: int,
    hard_max: int,
) -> list[tuple[int, int, str]]:
    """Greedy merger: keep concatenating blocks until we hit ``target`` chars
    (or would exceed ``hard_max``). Then emit a chunk and start the next one
    by re-including the *tail* of the previous chunk to provide overlap.
    """

    if not blocks:
        return []

    # Drop pure-whitespace blocks; they break the paragraph-glue heuristic.
    blocks = [b for b in blocks if b[2].strip()]

    chunks: list[tuple[int, int, str]] = []
    cur_start = blocks[0][0]
    cur_text = ""
    for s, e, t in blocks:
        prospective = (cur_text + t) if cur_text else t
        if len(cur_text) and len(prospective) > hard_max:
            chunks.append((cur_start, cur_start + len(cur_text), cur_text))
            cur_text = ""
        if not cur_text:
            cur_start = s
        cur_text = (cur_text + t) if cur_text else t
        if len(cur_text) >= target:
            chunks.append((cur_start, cur_start + len(cur_text), cur_text))
            cur_text = ""

    if cur_text and len(cur_text) >= minimum:
        chunks.append((cur_start, cur_start + len(cur_text), cur_text))
    elif cur_text and chunks:
        # Tail too small — glue it onto the previous chunk so we don't ship a
        # tiny scrap that hashes like noise.
        ps, _, pt = chunks[-1]
        chunks[-1] = (ps, cur_start + len(cur_text), pt + cur_text)
    elif cur_text:
        chunks.append((cur_start, cur_start + len(cur_text), cur_text))

    if overlap <= 0 or len(chunks) < 2:
        return chunks

    # Add overlap by prepending the *last ``overlap`` chars* of each chunk to
    # the next one. We don't shift offsets — the overlap ranges deliberately
    # overlap. Downstream get_chunk_range merges dedup on text.
    out: list[tuple[int, int, str]] = [chunks[0]]
    for i in range(1, len(chunks)):
        prev_text = chunks[i - 1][2]
        tail = prev_text[-overlap:] if len(prev_text) > overlap else prev_text
        s, e, t = chunks[i]
        # Try not to start mid-word.
        space_idx = tail.find(" ")
        if 0 < space_idx < 40:
            tail = tail[space_idx + 1 :]
        new_text = tail + t
        new_start = max(0, s - len(tail))
        out.append((new_start, e, new_text))
    return out


def chunk_file(path: Path, *, target: int, overlap: int, minimum: int, hard_max: int) -> list[Chunk]:
    text = path.read_text(encoding="utf-8")
    blocks = split_into_blocks(text)
    merged = merge_blocks(blocks, target=target, overlap=overlap, minimum=minimum, hard_max=hard_max)
    rel = str(path.relative_to(DOCS_DIR))
    return [
        Chunk(file=rel, chunk_id=i, char_start=s, char_end=e, text=t)
        for i, (s, e, t) in enumerate(merged)
    ]


def discover_docs() -> list[Path]:
    return sorted(p for p in DOCS_DIR.rglob("*.md") if p.is_file())


# ---- index builders ------------------------------------------------------


def reset_dirs() -> None:
    for d in (CHROMA_DIR, BM25_DIR):
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True, exist_ok=True)


def build_dense(chunks: list[Chunk]) -> None:
    import chromadb
    from chromadb.config import Settings

    client = chromadb.PersistentClient(
        path=str(CHROMA_DIR),
        settings=Settings(anonymized_telemetry=False),
    )
    # Always (re)create — full rewrite per spec.
    try:
        client.delete_collection(COLLECTION_NAME)
    except Exception:
        pass
    coll = client.create_collection(name=COLLECTION_NAME, metadata={"hnsw:space": "cosine"})

    ids = [f"{c.file}#{c.chunk_id}" for c in chunks]
    documents = [c.text for c in chunks]
    metadatas = [
        {"file": c.file, "chunk_id": c.chunk_id, "char_start": c.char_start, "char_end": c.char_end}
        for c in chunks
    ]
    # Add in batches — Chroma recommends ≤ ~5k per add() call.
    BATCH = 256
    for i in range(0, len(ids), BATCH):
        coll.add(
            ids=ids[i : i + BATCH],
            documents=documents[i : i + BATCH],
            metadatas=metadatas[i : i + BATCH],
        )
    print(f"  dense: indexed {len(ids)} chunks → {CHROMA_DIR}")


def build_sparse(chunks: list[Chunk]) -> None:
    import bm25s

    # bm25s tokenizer. Stopwords + (optional) stemming via PyStemmer if
    # installed. We try-except the stemmer so the build doesn't hard-require
    # PyStemmer just for marginal recall gains.
    try:
        import Stemmer  # type: ignore
        stemmer = Stemmer.Stemmer("english")
    except Exception:
        stemmer = None

    raw_corpus = [c.text for c in chunks]
    tokens = bm25s.tokenize(raw_corpus, stopwords="en", stemmer=stemmer, show_progress=False)
    retriever = bm25s.BM25()
    retriever.index(tokens, show_progress=False)
    retriever.save(str(BM25_DIR / "bm25"))
    print(f"  sparse: indexed {len(chunks)} chunks → {BM25_DIR}")


def write_manifest(chunks: list[Chunk]) -> None:
    manifest = {
        "version": 1,
        "embedder": "all-MiniLM-L6-v2 (chroma default)",
        "chunk_count": len(chunks),
        "files": sorted({c.file for c in chunks}),
        "chunks": [asdict(c) for c in chunks],
    }
    MANIFEST_PATH.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    print(f"  manifest: {MANIFEST_PATH}  ({len(chunks)} chunks across {len(manifest['files'])} files)")


# ---- entry point ---------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Build dense + sparse RAG indexes from docs/.")
    p.add_argument("--target", type=int, default=DEFAULT_TARGET, help="Target chunk size in chars.")
    p.add_argument("--overlap", type=int, default=DEFAULT_OVERLAP, help="Overlap between neighbouring chunks in chars.")
    p.add_argument("--min", dest="minimum", type=int, default=DEFAULT_MIN, help="Minimum chunk size before tail-glue.")
    p.add_argument("--max", dest="hard_max", type=int, default=HARD_MAX, help="Hard upper bound on chunk size.")
    args = p.parse_args(argv)

    if not DOCS_DIR.exists():
        print(f"docs/ not found at {DOCS_DIR}", file=sys.stderr)
        return 2

    files = discover_docs()
    if not files:
        print(f"no .md files under {DOCS_DIR}", file=sys.stderr)
        return 2

    print(f"docs/: {len(files)} markdown file(s)")
    chunks: list[Chunk] = []
    for f in files:
        cs = chunk_file(
            f, target=args.target, overlap=args.overlap, minimum=args.minimum, hard_max=args.hard_max
        )
        chunks.extend(cs)
        print(f"  {f.relative_to(DOCS_DIR)}: {len(cs)} chunks")

    print(f"total chunks: {len(chunks)}")
    print()
    print("rebuilding indexes (full rewrite)…")
    reset_dirs()
    build_dense(chunks)
    build_sparse(chunks)
    write_manifest(chunks)
    print()
    print("done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
