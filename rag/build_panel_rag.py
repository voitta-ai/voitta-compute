#!/usr/bin/env python3
"""Build dense + sparse RAG indexes for the holoviz/panel source tree.

Sibling to ``build_rag.py``, but indexes ``libs-info/panel/`` instead of
``docs/`` and writes to a separate pair of directories so the two corpora
never collide:

* rag/.chroma_panel/   — Chroma persistent client store (collection
                         "panel_source"), default all-MiniLM-L6-v2 dense
                         embeddings, cosine distance.
* rag/.bm25_panel/     — bm25s index + manifest.json with the canonical
                         chunk list (file, chunk_id, char_start/end, text,
                         kind, symbol).

What gets indexed:

* ``libs-info/panel/panel/**/*.py`` — the package source.
* ``libs-info/panel/doc/**/*.md`` and ``*.rst`` — official documentation.
* ``libs-info/panel/examples/**/*.py`` — usage examples.
* Top-level README.md and CHANGELOG.md.

What is skipped: ``tests/``, ``__pycache__/``, ``_static/``, ``_templates/``,
``dist/``, build artefacts, notebooks (``.ipynb``).

Chunking is content-type-aware:

* ``.py`` → AST-aware. One chunk per top-level function, plus one per class
  header (decorators + signature + body up to the first method) and one
  per method of the class. Module-level imports/constants between defs are
  collected into a single "module preamble" chunk. Each chunk carries a
  short symbolic header (``# File: ...`` / ``# Symbol: ...``) so retrieval
  surfaces the location info even when the body is small. If a single
  function/method exceeds the hard cap, it is char-windowed with overlap.
* ``.md``/``.rst`` → reuse the markdown-aware splitter from ``build_rag``.

Each run is a full rewrite (both index dirs are deleted and recreated).
"""

from __future__ import annotations

import argparse
import ast
import json
import shutil
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

# Reuse the prose chunker (markdown / blank-line splits + greedy merger)
# from the docs builder. Import is path-local — both scripts live in rag/.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_rag import merge_blocks, split_into_blocks  # noqa: E402


REPO_ROOT = Path(__file__).resolve().parents[1]
PANEL_ROOT = REPO_ROOT / "libs-info" / "panel"
RAG_DIR = REPO_ROOT / "rag"
CHROMA_DIR = RAG_DIR / ".chroma_panel"
BM25_DIR = RAG_DIR / ".bm25_panel"
MANIFEST_PATH = BM25_DIR / "manifest.json"
COLLECTION_NAME = "panel_source"

# Code-friendly chunk sizes — methods can be a couple of hundred lines.
PY_HARD_MAX = 3500
PY_OVERLAP = 200

# Doc/prose chunk sizes mirror docs corpus.
DOC_TARGET = 800
DOC_OVERLAP = 150
DOC_MIN = 200
DOC_HARD_MAX = 1500

INCLUDE_DIRS: list[tuple[Path, set[str]]] = [
    (PANEL_ROOT / "panel", {".py"}),
    (PANEL_ROOT / "doc", {".md", ".rst"}),
    (PANEL_ROOT / "examples", {".py"}),
]
INCLUDE_TOP_LEVEL = ["README.md", "CHANGELOG.md"]
SKIP_DIR_NAMES = {
    "tests",
    "__pycache__",
    "_static",
    "_templates",
    "dist",
    "node_modules",
    ".pixi",
    ".ipynb_checkpoints",
}


@dataclass
class Chunk:
    file: str  # relative to libs-info/panel/
    chunk_id: int  # 0-based, dense within a file
    char_start: int
    char_end: int
    text: str
    kind: str  # "module" | "function" | "class_header" | "method" | "class" | "doc" | "code_fallback"
    symbol: str  # e.g. "Button.on_click", "<module>", "section: Layout"


# ---- file discovery ------------------------------------------------------


def discover_files() -> list[tuple[Path, str]]:
    """Yield (absolute_path, ext) for every file we want to index."""
    out: list[tuple[Path, str]] = []
    for root, exts in INCLUDE_DIRS:
        if not root.exists():
            continue
        for p in sorted(root.rglob("*")):
            if not p.is_file():
                continue
            if any(part in SKIP_DIR_NAMES for part in p.relative_to(root).parts):
                continue
            ext = p.suffix.lower()
            if ext in exts:
                out.append((p, ext))
    for name in INCLUDE_TOP_LEVEL:
        p = PANEL_ROOT / name
        if p.is_file():
            out.append((p, p.suffix.lower()))
    return out


# ---- python AST chunking -------------------------------------------------


def _line_offsets(text: str) -> list[int]:
    """Return [start_of_line_1, start_of_line_2, ...] in chars."""
    offs = [0]
    for i, ch in enumerate(text):
        if ch == "\n":
            offs.append(i + 1)
    return offs


def _node_span(node: ast.AST, line_offs: list[int], src_len: int) -> tuple[int, int]:
    """Char [start, end) of a top-level AST node, including decorators."""
    decos = getattr(node, "decorator_list", []) or []
    start_line = decos[0].lineno if decos else node.lineno
    end_line = getattr(node, "end_lineno", None) or node.lineno
    end_col = getattr(node, "end_col_offset", 0) or 0
    s = line_offs[start_line - 1]
    e = (line_offs[end_line - 1] if end_line - 1 < len(line_offs) else src_len) + end_col
    return s, min(e, src_len)


def _emit_code_chunks(
    rel_path: str, body: str, char_start: int, char_end: int, kind: str, symbol: str
) -> list[Chunk]:
    """Emit one chunk for a code unit, char-windowing if it overflows hard max.

    Each chunk text is prefixed with a ``# File: ... / # Symbol: ...`` header
    so the LLM (and BM25) see the location even on small bodies.
    """
    header = f"# File: {rel_path}\n# Symbol: {symbol}  ({kind})\n\n"
    if len(header) + len(body) <= PY_HARD_MAX:
        return [
            Chunk(
                file=rel_path,
                chunk_id=-1,
                char_start=char_start,
                char_end=char_end,
                text=header + body,
                kind=kind,
                symbol=symbol,
            )
        ]
    # Char-window with overlap. We keep the synthetic header on every part.
    out: list[Chunk] = []
    body_budget = PY_HARD_MAX - len(header)
    if body_budget < 500:
        body_budget = 500  # pathological symbol name; let the chunk be a bit bigger.
    pos = 0
    part = 1
    n = len(body)
    while pos < n:
        end = min(pos + body_budget, n)
        part_header = f"# File: {rel_path}\n# Symbol: {symbol}  ({kind}, part {part})\n\n"
        out.append(
            Chunk(
                file=rel_path,
                chunk_id=-1,
                char_start=char_start + pos,
                char_end=char_start + end,
                text=part_header + body[pos:end],
                kind=kind,
                symbol=symbol,
            )
        )
        if end == n:
            break
        pos = end - PY_OVERLAP
        part += 1
    return out


def _chunk_class(rel_path: str, source: str, line_offs: list[int], node: ast.ClassDef) -> list[Chunk]:
    cls_s, cls_e = _node_span(node, line_offs, len(source))
    methods = [b for b in node.body if isinstance(b, (ast.FunctionDef, ast.AsyncFunctionDef))]
    if not methods:
        return _emit_code_chunks(
            rel_path, source[cls_s:cls_e], cls_s, cls_e, "class", node.name
        )

    chunks: list[Chunk] = []
    method_spans = [(m, *_node_span(m, line_offs, len(source))) for m in methods]
    method_spans.sort(key=lambda t: t[1])

    # Class header: from class start through start of first method.
    first_m_s = method_spans[0][1]
    header_text = source[cls_s:first_m_s]
    chunks.extend(
        _emit_code_chunks(rel_path, header_text, cls_s, first_m_s, "class_header", node.name)
    )
    for m, s, e in method_spans:
        chunks.extend(
            _emit_code_chunks(
                rel_path, source[s:e], s, e, "method", f"{node.name}.{m.name}"
            )
        )
    return chunks


def chunk_python(rel_path: str, source: str) -> list[Chunk]:
    try:
        tree = ast.parse(source, filename=rel_path)
    except SyntaxError:
        # Treat the whole file as one prose-ish blob.
        return _emit_code_chunks(rel_path, source, 0, len(source), "code_fallback", "<unparseable>")

    line_offs = _line_offsets(source)
    src_len = len(source)

    top_defs = [
        n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]
    spans = sorted(_node_span(d, line_offs, src_len) for d in top_defs)

    # Module preamble: every char NOT covered by a top-level def, concatenated.
    preamble_parts: list[tuple[int, int, str]] = []
    cursor = 0
    for s, e in spans:
        if s > cursor:
            preamble_parts.append((cursor, s, source[cursor:s]))
        cursor = max(cursor, e)
    if cursor < src_len:
        preamble_parts.append((cursor, src_len, source[cursor:src_len]))

    chunks: list[Chunk] = []
    preamble_text = "".join(p[2] for p in preamble_parts).strip()
    if preamble_text:
        # Use enclosing span; this represents discontinuous slices but is good
        # enough for `get_range` (we never use a preamble as a stitch source
        # alone — we recover full files via consecutive chunks).
        first_s = preamble_parts[0][0]
        last_e = preamble_parts[-1][1]
        body = "\n\n".join(p[2].rstrip() for p in preamble_parts if p[2].strip())
        chunks.extend(
            _emit_code_chunks(rel_path, body, first_s, last_e, "module", "<module>")
        )

    for d in top_defs:
        if isinstance(d, ast.ClassDef):
            chunks.extend(_chunk_class(rel_path, source, line_offs, d))
        else:
            s, e = _node_span(d, line_offs, src_len)
            chunks.extend(
                _emit_code_chunks(rel_path, source[s:e], s, e, "function", d.name)
            )

    chunks.sort(key=lambda c: c.char_start)
    for i, c in enumerate(chunks):
        c.chunk_id = i
    return chunks


# ---- prose chunking (.md / .rst) -----------------------------------------


def chunk_prose(rel_path: str, source: str) -> list[Chunk]:
    blocks = split_into_blocks(source)
    merged = merge_blocks(
        blocks,
        target=DOC_TARGET,
        overlap=DOC_OVERLAP,
        minimum=DOC_MIN,
        hard_max=DOC_HARD_MAX,
    )
    out: list[Chunk] = []
    for i, (s, e, t) in enumerate(merged):
        # Symbol = the leading heading on the chunk, if any.
        first_line = t.lstrip().split("\n", 1)[0].lstrip("# ").strip()
        symbol = first_line[:80] if first_line else f"chunk {i}"
        header = f"# File: {rel_path}\n# Section: {symbol}\n\n"
        out.append(
            Chunk(
                file=rel_path,
                chunk_id=i,
                char_start=s,
                char_end=e,
                text=header + t,
                kind="doc",
                symbol=symbol,
            )
        )
    return out


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
    try:
        client.delete_collection(COLLECTION_NAME)
    except Exception:
        pass
    coll = client.create_collection(name=COLLECTION_NAME, metadata={"hnsw:space": "cosine"})

    ids = [f"{c.file}#{c.chunk_id}" for c in chunks]
    documents = [c.text for c in chunks]
    metadatas = [
        {
            "file": c.file,
            "chunk_id": c.chunk_id,
            "char_start": c.char_start,
            "char_end": c.char_end,
            "kind": c.kind,
            "symbol": c.symbol,
        }
        for c in chunks
    ]
    BATCH = 256
    for i in range(0, len(ids), BATCH):
        coll.add(
            ids=ids[i : i + BATCH],
            documents=documents[i : i + BATCH],
            metadatas=metadatas[i : i + BATCH],
        )
        if (i // BATCH) % 4 == 0:
            print(f"    embedded {min(i + BATCH, len(ids))}/{len(ids)}")
    print(f"  dense: indexed {len(ids)} chunks → {CHROMA_DIR}")


def build_sparse(chunks: list[Chunk]) -> None:
    import bm25s

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
        "corpus": "panel_source",
        "embedder": "all-MiniLM-L6-v2 (chroma default)",
        "chunk_count": len(chunks),
        "files": sorted({c.file for c in chunks}),
        "chunks": [asdict(c) for c in chunks],
    }
    MANIFEST_PATH.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    print(
        f"  manifest: {MANIFEST_PATH}  "
        f"({len(chunks)} chunks across {len(manifest['files'])} files)"
    )


# ---- entry point ---------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Build dense + sparse RAG indexes from libs-info/panel/.")
    p.add_argument("--limit", type=int, default=0, help="If > 0, only index this many files (debug).")
    args = p.parse_args(argv)

    if not PANEL_ROOT.exists():
        print(f"panel source not found at {PANEL_ROOT}", file=sys.stderr)
        print(
            "  hint: cd libs-info && git clone --depth 1 https://github.com/holoviz/panel",
            file=sys.stderr,
        )
        return 2

    files = discover_files()
    if args.limit:
        files = files[: args.limit]
    if not files:
        print(f"no indexable files found under {PANEL_ROOT}", file=sys.stderr)
        return 2

    print(f"libs-info/panel: {len(files)} files to index")
    chunks: list[Chunk] = []
    n_skipped = 0
    for path, ext in files:
        rel = str(path.relative_to(PANEL_ROOT))
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            print(f"  skip {rel}: {exc}")
            n_skipped += 1
            continue
        if ext == ".py":
            cs = chunk_python(rel, source)
        else:
            cs = chunk_prose(rel, source)
        chunks.extend(cs)

    print(f"total chunks: {len(chunks)}  (skipped {n_skipped} file(s))")
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
