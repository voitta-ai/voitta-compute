#!/usr/bin/env python3
"""Build the RAG indexes — unified entry point for both corpora.

Replaces the two separate builders that previously lived at
``rag/build_rag.py`` (docs corpus) and ``rag/build_panel_rag.py`` (Panel
source corpus). The output directory layout is unchanged so the runtime
RAG reader keeps working without modification.

Corpora
-------

``docs``
    Project's own prose docs from ``docs/`` plus every plugin's
    ``plugins/<name>/docs/`` tree. Output:

      * ``rag/.chroma/`` — Chroma persistent store, collection ``docs``.
      * ``rag/.bm25/``   — bm25s index + ``manifest.json``.

    Chunking: markdown-aware (split on H1/H2/H3, then on blank lines),
    greedy merger up to ~800 chars with ~150 char overlap.

``panel``
    HoloViz Panel library source — ``libs-info/panel/{panel,doc,examples}/``
    plus top-level README/CHANGELOG. Output:

      * ``rag/.chroma_panel/`` — Chroma store, collection ``panel_source``.
      * ``rag/.bm25_panel/``   — bm25s index + ``manifest.json``.

    Chunking is content-type-aware:
      * ``.py`` → AST-aware. One chunk per top-level function, class
        header, and method; module preamble collected separately. Each
        chunk gets a synthetic ``# File: ... / # Symbol: ...`` header
        so location info survives even on small bodies. Overflowing
        symbols are char-windowed with overlap.
      * ``.md`` / ``.rst`` → reuse the markdown prose splitter.

Usage
-----

    scripts/build_rag.py                  # builds both corpora
    scripts/build_rag.py --corpus docs    # docs only
    scripts/build_rag.py --corpus panel   # panel only

Each run is a full rewrite — the corpus's output directories are
deleted and recreated. No incremental update path.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import shutil
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

# Heavy deps (chromadb, bm25s, PyStemmer) are imported lazily inside the
# build helpers so `--help` stays fast and missing-package errors land in
# context.


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCS_DIR = REPO_ROOT / "docs"
PANEL_ROOT = REPO_ROOT / "libs-info" / "panel"
RAG_DIR = REPO_ROOT / "rag"


# ---------------------------------------------------------------------------
# Chunk model (shared across corpora)
# ---------------------------------------------------------------------------

@dataclass
class Chunk:
    file: str          # path relative to the corpus root
    chunk_id: int      # 0-based, dense within a file (or globally sorted for AST)
    char_start: int
    char_end: int
    text: str
    # Optional fields populated by the panel chunker. The docs chunker
    # leaves them empty — the runtime reader treats absence as "doc-style".
    kind: str = ""     # "module" | "function" | "class_header" | "method" |
                       # "class" | "code_fallback" | "doc"
    symbol: str = ""   # e.g. "Button.on_click", "<module>", section heading


# ---------------------------------------------------------------------------
# Prose / markdown chunking — shared
# ---------------------------------------------------------------------------

DOC_TARGET = 800
DOC_OVERLAP = 150
DOC_MIN = 200
DOC_HARD_MAX = 1500


def split_into_blocks(text: str) -> list[tuple[int, int, str]]:
    """Markdown-aware split into atomic *blocks* (char-offset triples).

    Two passes:
      1. Split on H1/H2/H3 boundaries to keep sections logically grouped.
      2. Within each section, split on blank lines (paragraph / table /
         code-block separators).
    Splits never cut inside a paragraph — that's the merger's job when a
    single block already exceeds the hard max.
    """

    out: list[tuple[int, int, str]] = []
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
    """Greedy merger: concatenate blocks up to ``target`` chars, emit a
    chunk, then re-include the tail of the previous chunk to provide
    overlap into the next."""

    if not blocks:
        return []

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
        # Tail too small — glue it onto the previous chunk so we don't
        # ship a tiny scrap that hashes like noise.
        ps, _, pt = chunks[-1]
        chunks[-1] = (ps, cur_start + len(cur_text), pt + cur_text)
    elif cur_text:
        chunks.append((cur_start, cur_start + len(cur_text), cur_text))

    if overlap <= 0 or len(chunks) < 2:
        return chunks

    out: list[tuple[int, int, str]] = [chunks[0]]
    for i in range(1, len(chunks)):
        prev_text = chunks[i - 1][2]
        tail = prev_text[-overlap:] if len(prev_text) > overlap else prev_text
        s, e, t = chunks[i]
        space_idx = tail.find(" ")
        if 0 < space_idx < 40:
            tail = tail[space_idx + 1:]
        new_text = tail + t
        new_start = max(0, s - len(tail))
        out.append((new_start, e, new_text))
    return out


# ---------------------------------------------------------------------------
# Python AST chunking — panel corpus
# ---------------------------------------------------------------------------

PY_HARD_MAX = 3500
PY_OVERLAP = 200


def _line_offsets(text: str) -> list[int]:
    offs = [0]
    for i, ch in enumerate(text):
        if ch == "\n":
            offs.append(i + 1)
    return offs


def _node_span(node: ast.AST, line_offs: list[int], src_len: int) -> tuple[int, int]:
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
    """Emit one or more chunks for a code unit, char-windowing if the
    unit's body + synthetic header exceeds the hard cap."""
    header = f"# File: {rel_path}\n# Symbol: {symbol}  ({kind})\n\n"
    if len(header) + len(body) <= PY_HARD_MAX:
        return [Chunk(
            file=rel_path, chunk_id=-1,
            char_start=char_start, char_end=char_end,
            text=header + body, kind=kind, symbol=symbol,
        )]
    out: list[Chunk] = []
    body_budget = max(PY_HARD_MAX - len(header), 500)
    pos = 0
    part = 1
    n = len(body)
    while pos < n:
        end = min(pos + body_budget, n)
        part_header = f"# File: {rel_path}\n# Symbol: {symbol}  ({kind}, part {part})\n\n"
        out.append(Chunk(
            file=rel_path, chunk_id=-1,
            char_start=char_start + pos, char_end=char_start + end,
            text=part_header + body[pos:end], kind=kind, symbol=symbol,
        ))
        if end == n:
            break
        pos = end - PY_OVERLAP
        part += 1
    return out


def _chunk_class(rel_path: str, source: str, line_offs: list[int], node: ast.ClassDef) -> list[Chunk]:
    cls_s, cls_e = _node_span(node, line_offs, len(source))
    methods = [b for b in node.body if isinstance(b, (ast.FunctionDef, ast.AsyncFunctionDef))]
    if not methods:
        return _emit_code_chunks(rel_path, source[cls_s:cls_e], cls_s, cls_e, "class", node.name)

    chunks: list[Chunk] = []
    method_spans = [(m, *_node_span(m, line_offs, len(source))) for m in methods]
    method_spans.sort(key=lambda t: t[1])

    first_m_s = method_spans[0][1]
    header_text = source[cls_s:first_m_s]
    chunks.extend(_emit_code_chunks(
        rel_path, header_text, cls_s, first_m_s, "class_header", node.name
    ))
    for m, s, e in method_spans:
        chunks.extend(_emit_code_chunks(
            rel_path, source[s:e], s, e, "method", f"{node.name}.{m.name}"
        ))
    return chunks


def chunk_python(rel_path: str, source: str) -> list[Chunk]:
    try:
        tree = ast.parse(source, filename=rel_path)
    except SyntaxError:
        return _emit_code_chunks(rel_path, source, 0, len(source), "code_fallback", "<unparseable>")

    line_offs = _line_offsets(source)
    src_len = len(source)

    top_defs = [
        n for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]
    spans = sorted(_node_span(d, line_offs, src_len) for d in top_defs)

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
        first_s = preamble_parts[0][0]
        last_e = preamble_parts[-1][1]
        body = "\n\n".join(p[2].rstrip() for p in preamble_parts if p[2].strip())
        chunks.extend(_emit_code_chunks(rel_path, body, first_s, last_e, "module", "<module>"))

    for d in top_defs:
        if isinstance(d, ast.ClassDef):
            chunks.extend(_chunk_class(rel_path, source, line_offs, d))
        else:
            s, e = _node_span(d, line_offs, src_len)
            chunks.extend(_emit_code_chunks(rel_path, source[s:e], s, e, "function", d.name))

    chunks.sort(key=lambda c: c.char_start)
    for i, c in enumerate(chunks):
        c.chunk_id = i
    return chunks


def chunk_prose_with_header(rel_path: str, source: str) -> list[Chunk]:
    """Prose chunker variant used by the panel corpus: prefixes a
    ``# File: ... # Section: ...`` header for symmetry with code chunks."""
    blocks = split_into_blocks(source)
    merged = merge_blocks(
        blocks, target=DOC_TARGET, overlap=DOC_OVERLAP,
        minimum=DOC_MIN, hard_max=DOC_HARD_MAX,
    )
    out: list[Chunk] = []
    for i, (s, e, t) in enumerate(merged):
        first_line = t.lstrip().split("\n", 1)[0].lstrip("# ").strip()
        symbol = first_line[:80] if first_line else f"chunk {i}"
        header = f"# File: {rel_path}\n# Section: {symbol}\n\n"
        out.append(Chunk(
            file=rel_path, chunk_id=i,
            char_start=s, char_end=e,
            text=header + t, kind="doc", symbol=symbol,
        ))
    return out


def chunk_prose_plain(path: Path, base_dir: Path) -> list[Chunk]:
    """Prose chunker for the docs corpus — no synthetic header, file
    field is the path relative to ``base_dir``."""
    text = path.read_text(encoding="utf-8")
    blocks = split_into_blocks(text)
    merged = merge_blocks(
        blocks, target=DOC_TARGET, overlap=DOC_OVERLAP,
        minimum=DOC_MIN, hard_max=DOC_HARD_MAX,
    )
    try:
        rel = str(path.relative_to(base_dir))
    except ValueError:
        rel = str(path)
    return [
        Chunk(file=rel, chunk_id=i, char_start=s, char_end=e, text=t)
        for i, (s, e, t) in enumerate(merged)
    ]


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------

PANEL_INCLUDE_DIRS: list[tuple[Path, set[str]]] = [
    (PANEL_ROOT / "panel", {".py"}),
    (PANEL_ROOT / "doc", {".md", ".rst"}),
    (PANEL_ROOT / "examples", {".py"}),
]
PANEL_INCLUDE_TOP_LEVEL = ["README.md", "CHANGELOG.md"]
PANEL_SKIP_DIRS = {
    "tests", "__pycache__", "_static", "_templates", "dist",
    "node_modules", ".pixi", ".ipynb_checkpoints",
}


def discover_panel_files() -> list[tuple[Path, str]]:
    out: list[tuple[Path, str]] = []
    for root, exts in PANEL_INCLUDE_DIRS:
        if not root.exists():
            continue
        for p in sorted(root.rglob("*")):
            if not p.is_file():
                continue
            if any(part in PANEL_SKIP_DIRS for part in p.relative_to(root).parts):
                continue
            ext = p.suffix.lower()
            if ext in exts:
                out.append((p, ext))
    for name in PANEL_INCLUDE_TOP_LEVEL:
        p = PANEL_ROOT / name
        if p.is_file():
            out.append((p, p.suffix.lower()))
    return out


def discover_core_docs() -> list[Path]:
    return sorted(p for p in DOCS_DIR.rglob("*.md") if p.is_file())


def _candidate_plugin_roots() -> list[Path]:
    """Plugin roots: source checkout's ``plugins/`` plus the packaged
    .app's bundled copy. Whichever exists is used."""
    out: list[Path] = []
    repo = REPO_ROOT / "plugins"
    if repo.is_dir():
        out.append(repo)
    try:
        import voitta as _voitta  # type: ignore[import-not-found]
        res = Path(_voitta.__file__).resolve().parent / "resources" / "plugins"
        if res.is_dir() and res not in out:
            out.append(res)
    except Exception:
        pass
    return out


def discover_plugin_docs() -> list[tuple[Path, Path]]:
    """Returns ``[(base_dir, md_path), ...]`` where ``base_dir`` is the
    plugins root so chunk paths come out as ``<plugin>/docs/<file>.md``."""
    out: list[tuple[Path, Path]] = []
    for plugins_root in _candidate_plugin_roots():
        for plugin_dir in sorted(plugins_root.iterdir()):
            if not plugin_dir.is_dir() or plugin_dir.name.startswith("."):
                continue
            docs_dir = plugin_dir / "docs"
            if not docs_dir.is_dir():
                continue
            for md in sorted(docs_dir.rglob("*.md")):
                if md.is_file():
                    out.append((plugins_root, md))
    return out


# ---------------------------------------------------------------------------
# Index builders — shared, parameterised by corpus config
# ---------------------------------------------------------------------------

@dataclass
class CorpusConfig:
    name: str                       # logical corpus name ("docs" / "panel")
    chroma_dir: Path
    bm25_dir: Path
    collection_name: str
    extra_metadata_fields: list[str] = field(default_factory=list)
    progress_every_batches: int = 0  # 0 = no per-batch progress logs


DOCS_CFG = CorpusConfig(
    name="docs",
    chroma_dir=RAG_DIR / ".chroma",
    bm25_dir=RAG_DIR / ".bm25",
    collection_name="docs",
)

PANEL_CFG = CorpusConfig(
    name="panel",
    chroma_dir=RAG_DIR / ".chroma_panel",
    bm25_dir=RAG_DIR / ".bm25_panel",
    collection_name="panel_source",
    extra_metadata_fields=["kind", "symbol"],
    progress_every_batches=4,
)


def reset_dirs(cfg: CorpusConfig) -> None:
    for d in (cfg.chroma_dir, cfg.bm25_dir):
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True, exist_ok=True)


def build_dense(chunks: list[Chunk], cfg: CorpusConfig) -> None:
    import chromadb
    from chromadb.config import Settings

    client = chromadb.PersistentClient(
        path=str(cfg.chroma_dir),
        settings=Settings(anonymized_telemetry=False),
    )
    try:
        client.delete_collection(cfg.collection_name)
    except Exception:
        pass
    coll = client.create_collection(
        name=cfg.collection_name, metadata={"hnsw:space": "cosine"}
    )

    ids = [f"{c.file}#{c.chunk_id}" for c in chunks]
    documents = [c.text for c in chunks]
    metadatas: list[dict] = []
    for c in chunks:
        m: dict = {
            "file": c.file,
            "chunk_id": c.chunk_id,
            "char_start": c.char_start,
            "char_end": c.char_end,
        }
        for extra in cfg.extra_metadata_fields:
            m[extra] = getattr(c, extra)
        metadatas.append(m)

    BATCH = 256
    for i in range(0, len(ids), BATCH):
        coll.add(
            ids=ids[i:i + BATCH],
            documents=documents[i:i + BATCH],
            metadatas=metadatas[i:i + BATCH],
        )
        if cfg.progress_every_batches and (i // BATCH) % cfg.progress_every_batches == 0:
            print(f"    embedded {min(i + BATCH, len(ids))}/{len(ids)}")
    print(f"  dense: indexed {len(ids)} chunks → {cfg.chroma_dir}")


def build_sparse(chunks: list[Chunk], cfg: CorpusConfig) -> None:
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
    retriever.save(str(cfg.bm25_dir / "bm25"))
    print(f"  sparse: indexed {len(chunks)} chunks → {cfg.bm25_dir}")


def write_manifest(chunks: list[Chunk], cfg: CorpusConfig) -> None:
    manifest = {
        "version": 1,
        "corpus": cfg.collection_name,
        "embedder": "all-MiniLM-L6-v2 (chroma default)",
        "chunk_count": len(chunks),
        "files": sorted({c.file for c in chunks}),
        "chunks": [asdict(c) for c in chunks],
    }
    path = cfg.bm25_dir / "manifest.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    print(f"  manifest: {path}  "
          f"({len(chunks)} chunks across {len(manifest['files'])} files)")


# ---------------------------------------------------------------------------
# Corpus assemblers
# ---------------------------------------------------------------------------

def assemble_docs_chunks() -> list[Chunk]:
    if not DOCS_DIR.exists():
        print(f"docs/ not found at {DOCS_DIR}", file=sys.stderr)
        return []

    files = discover_core_docs()
    if not files:
        print(f"no .md files under {DOCS_DIR}", file=sys.stderr)
        return []

    print(f"docs/: {len(files)} markdown file(s)")
    chunks: list[Chunk] = []
    for f in files:
        cs = chunk_prose_plain(f, DOCS_DIR)
        chunks.extend(cs)
        print(f"  {f.relative_to(DOCS_DIR)}: {len(cs)} chunks")

    plugin_pairs = discover_plugin_docs()
    if plugin_pairs:
        print(f"plugins: {len(plugin_pairs)} markdown file(s) across "
              f"{len({p[1].parent for p in plugin_pairs})} plugin(s)")
        for base, md in plugin_pairs:
            cs = chunk_prose_plain(md, base)
            chunks.extend(cs)
            print(f"  {md.relative_to(base)}: {len(cs)} chunks")

    return chunks


def assemble_panel_chunks(limit: int = 0) -> list[Chunk]:
    if not PANEL_ROOT.exists():
        print(f"panel source not found at {PANEL_ROOT}", file=sys.stderr)
        print("  hint: cd libs-info && git clone --depth 1 https://github.com/holoviz/panel",
              file=sys.stderr)
        return []

    files = discover_panel_files()
    if limit:
        files = files[:limit]
    if not files:
        print(f"no indexable files found under {PANEL_ROOT}", file=sys.stderr)
        return []

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
            cs = chunk_prose_with_header(rel, source)
        chunks.extend(cs)
    print(f"total chunks: {len(chunks)}  (skipped {n_skipped} file(s))")
    return chunks


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _run_corpus(name: str, chunks: list[Chunk], cfg: CorpusConfig) -> bool:
    if not chunks:
        print(f"\n[{name}] no chunks — skipped", file=sys.stderr)
        return False
    print(f"\n[{name}] total chunks: {len(chunks)}")
    print(f"[{name}] rebuilding indexes (full rewrite)…")
    reset_dirs(cfg)
    build_dense(chunks, cfg)
    build_sparse(chunks, cfg)
    write_manifest(chunks, cfg)
    return True


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Build dense + sparse RAG indexes (docs and/or panel corpora).",
    )
    p.add_argument(
        "--corpus", choices=("docs", "panel", "both"), default="both",
        help="Which corpus to (re)build. Default: both.",
    )
    p.add_argument(
        "--panel-limit", type=int, default=0,
        help="(panel only) If > 0, only index this many files (debug).",
    )
    args = p.parse_args(argv)

    built_any = False
    if args.corpus in ("docs", "both"):
        built_any |= _run_corpus("docs", assemble_docs_chunks(), DOCS_CFG)
    if args.corpus in ("panel", "both"):
        built_any |= _run_corpus("panel", assemble_panel_chunks(args.panel_limit), PANEL_CFG)

    if not built_any:
        return 2
    print("\ndone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
