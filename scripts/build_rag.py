#!/usr/bin/env python3
"""Build dense + sparse RAG indexes for two corpora.

* ``docs`` — project prose docs at ``docs/`` plus every plugin's
  ``plugins/<name>/docs/`` tree. Markdown-aware chunking.
* ``code`` — vendored libraries under ``lib-sources/<repo>/``.
  Python files are AST-chunked (module / class / method / function);
  JS/TS files are regex-chunked at top-level function and class
  boundaries. Each chunk carries ``repo``, ``path``, ``folder``,
  ``file``, ``lang``, ``kind``, ``symbol`` metadata for filtered
  queries.

Outputs:
  rag/.chroma/        ·  rag/.bm25/         (docs)
  rag/.chroma_code/   ·  rag/.bm25_code/    (code)

Each run for a given corpus is a full rewrite — output directories
are deleted and recreated. No incremental updates.

Usage:
    python scripts/build_rag.py                  # both corpora
    python scripts/build_rag.py --corpus docs
    python scripts/build_rag.py --corpus code
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

import os as _os
REPO_ROOT = Path(_os.environ.get("VOITTA_REPO_ROOT") or Path(__file__).resolve().parents[1])
DOCS_DIR = Path(_os.environ.get("VOITTA_DOCS_DIR") or REPO_ROOT / "docs")
PLUGINS_DIR = Path(_os.environ.get("VOITTA_PLUGINS_DIR") or REPO_ROOT / "plugins")
LIBS_DIR = Path(_os.environ.get("VOITTA_LIBS_DIR") or REPO_ROOT / "lib-sources")
RAG_DIR = Path(_os.environ.get("VOITTA_RAG_DIR") or REPO_ROOT / "rag")


# ---------------------------------------------------------------------------
# Chunk model
# ---------------------------------------------------------------------------

@dataclass
class Chunk:
    file: str          # ``<repo>/<path>`` for code, ``<plugin>/docs/<file>`` for docs
    chunk_id: int
    char_start: int
    char_end: int
    text: str
    # Code-corpus metadata. Docs chunks leave these empty.
    repo: str = ""
    path: str = ""           # path relative to the repo root
    folder: str = ""         # dirname of path (so the LLM can filter by folder)
    lang: str = ""           # "py" | "ts" | "tsx" | "js" | "jsx"
    kind: str = ""           # "module" | "class" | "class_header" | "method"
                             # | "function" | "code_fallback" | "doc"
    symbol: str = ""         # function/class/method name (best effort)


# ---------------------------------------------------------------------------
# Prose chunking (docs corpus)
# ---------------------------------------------------------------------------

# Prose chunk targets — sized to fit MiniLM's 256-token window.
#
# all-MiniLM-L6-v2 max_seq_length = 256 tokens (WordPiece).
# Worst-case chars/token for English prose ≈ 2.44, for code ≈ 3.5-4.
# Safe ceiling: 256 × 3.5 = ~896 chars; we cap at 900 with 150 overlap.
# Previous caps (550/450) used only 43% of capacity, doubling chunk count
# unnecessarily and splitting logical units mid-context.
DOC_TARGET = 700
DOC_OVERLAP = 150
DOC_MIN = 120
DOC_HARD_MAX = 900


def split_into_blocks(text: str) -> list[tuple[int, int, str]]:
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
    *, target: int, overlap: int, minimum: int, hard_max: int,
) -> list[tuple[int, int, str]]:
    if not blocks:
        return []
    blocks = [b for b in blocks if b[2].strip()]

    # If a single block exceeds hard_max, split it into windowed
    # sub-blocks BEFORE the merge loop. Without this pre-split, a
    # single oversized paragraph would land as one too-big chunk and
    # the embedder would silently truncate it.
    expanded: list[tuple[int, int, str]] = []
    for s, e, t in blocks:
        if len(t) <= hard_max:
            expanded.append((s, e, t))
            continue
        pos = 0
        n = len(t)
        step = max(hard_max - overlap, 1)
        while pos < n:
            end = min(pos + hard_max, n)
            expanded.append((s + pos, s + end, t[pos:end]))
            if end == n:
                break
            pos += step
    blocks = expanded

    chunks: list[tuple[int, int, str]] = []
    cur_start = blocks[0][0]
    cur_text = ""
    for s, _e, t in blocks:
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
    elif cur_text and chunks and len(chunks[-1][2]) + len(cur_text) <= hard_max:
        # Merge undersized leftover into previous, but only if the
        # result still fits the embedder window. Otherwise emit it
        # as its own (short) chunk — short is fine; oversized is not.
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
        # Adding the tail must not push the chunk over hard_max.
        # If it would, trim the tail from the front to fit. We lose
        # some overlap context but stay under the embedder window.
        headroom = hard_max - len(t)
        if headroom <= 0:
            tail = ""
        elif len(tail) > headroom:
            tail = tail[-headroom:]
        new_text = tail + t
        new_start = max(0, s - len(tail))
        out.append((new_start, e, new_text))
    return out


def chunk_prose(path: Path, base_dir: Path) -> list[Chunk]:
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
    out = [
        Chunk(file=rel, chunk_id=i, char_start=s, char_end=e, text=t)
        for i, (s, e, t) in enumerate(merged)
    ]
    # Invariant — same reason as in _emit_code_chunks: oversize means
    # silent truncation at embed time. Crash the build instead.
    for c in out:
        if len(c.text) > DOC_HARD_MAX:
            raise AssertionError(
                f"prose chunker emitted oversized chunk: {rel} "
                f"chunk_id={c.chunk_id} len={len(c.text)} > "
                f"cap={DOC_HARD_MAX}. Would silently truncate at embed."
            )
    return out


# ---------------------------------------------------------------------------
# Code chunking — Python AST
# ---------------------------------------------------------------------------

# Code chunk size — sized to fit MiniLM's 256-token window.
#
# Empirical measurement on our actual corpus (sampled 400 Python +
# Java chunks of 800-900 chars): worst-case chars/token for code is
# ~2.00. So 256 tokens × 2.00 = 512 chars in the dense direction.
# Subtract header overhead (~80 chars for path + symbol metadata) =
# ~430 chars of body. Cap at 450 to leave a thin margin; chunks
# bigger than this risk the embedder truncating part of the body,
# which makes the dense vector reflect only the chunk head and
# breaks semantic retrieval for the tail.
#
# Smaller chunks → more chunks → finer-grained retrieval. PY_OVERLAP
# stitches context across boundaries so a function definition split
# across two chunks still embeds-and-retrieves coherently.
#
# See ``backend/tests/rag/test_chunker.py::test_chunks_fit_minilm``
# for the round-trip invariant against the real tokenizer.
PY_HARD_MAX = 900
PY_OVERLAP = 150


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
    rel_path: str, body: str, char_start: int, char_end: int,
    kind: str, symbol: str, lang: str,
) -> list[Chunk]:
    """Emit one or more chunks for a code unit, char-windowing if the
    unit's body + synthetic header exceeds the hard cap.

    Invariant: EVERY returned chunk's ``text`` length is ≤ ``PY_HARD_MAX``.
    This is asserted at the end — a violation here means the embedder
    will silently truncate, so we'd rather crash the build than
    quietly ship half-embedded chunks.

    The header embeds the path + symbol so embedding-signal includes
    locator metadata. When windowing, the part header is LONGER than
    the single-shot header (adds " part N"), so budget must be sized
    against the WORST-case header, not the simple one.
    """
    header = f"# File: {rel_path}\n# Symbol: {symbol}  ({kind})\n\n"
    if len(header) + len(body) <= PY_HARD_MAX:
        out = [Chunk(
            file=rel_path, chunk_id=-1,
            char_start=char_start, char_end=char_end,
            text=header + body, kind=kind, symbol=symbol, lang=lang,
        )]
        _assert_chunks_under_cap(out, rel_path)
        return out

    # Reserve headroom for the worst-case part header. " part 999" is
    # 9 extra chars over the single-shot header — pad to 16 for safety.
    part_header_overhead = 16
    body_budget = PY_HARD_MAX - len(header) - part_header_overhead
    if body_budget <= 0:
        raise ValueError(
            f"chunk header for {rel_path}#{symbol} is {len(header)} chars "
            f"— larger than PY_HARD_MAX={PY_HARD_MAX} minus part overhead "
            f"({part_header_overhead}). Shorten the path or symbol name, "
            f"or raise PY_HARD_MAX."
        )
    out: list[Chunk] = []
    pos = 0
    part = 1
    n = len(body)
    while pos < n:
        end = min(pos + body_budget, n)
        part_header = f"# File: {rel_path}\n# Symbol: {symbol}  ({kind}, part {part})\n\n"
        out.append(Chunk(
            file=rel_path, chunk_id=-1,
            char_start=char_start + pos, char_end=char_start + end,
            text=part_header + body[pos:end],
            kind=kind, symbol=symbol, lang=lang,
        ))
        if end == n:
            break
        pos = end - PY_OVERLAP
        part += 1
    _assert_chunks_under_cap(out, rel_path)
    return out


def _assert_chunks_under_cap(chunks: list[Chunk], rel_path: str) -> None:
    """Hard invariant. Violations mean the embedder will truncate
    silently — a bug, not a warning. Crash the build."""
    for c in chunks:
        if len(c.text) > PY_HARD_MAX:
            raise AssertionError(
                f"chunker emitted oversized chunk: {rel_path} "
                f"symbol={c.symbol!r} kind={c.kind!r} "
                f"len={len(c.text)} > cap={PY_HARD_MAX}. "
                f"This would silently truncate at embed time."
            )


def _chunk_python_class(
    rel_path: str, source: str, line_offs: list[int], node: ast.ClassDef, lang: str,
) -> list[Chunk]:
    cls_s, cls_e = _node_span(node, line_offs, len(source))
    methods = [b for b in node.body if isinstance(b, (ast.FunctionDef, ast.AsyncFunctionDef))]
    if not methods:
        return _emit_code_chunks(rel_path, source[cls_s:cls_e], cls_s, cls_e, "class", node.name, lang)
    chunks: list[Chunk] = []
    method_spans = [(m, *_node_span(m, line_offs, len(source))) for m in methods]
    method_spans.sort(key=lambda t: t[1])
    first_m_s = method_spans[0][1]
    chunks.extend(_emit_code_chunks(
        rel_path, source[cls_s:first_m_s], cls_s, first_m_s, "class_header", node.name, lang,
    ))
    for m, s, e in method_spans:
        chunks.extend(_emit_code_chunks(
            rel_path, source[s:e], s, e, "method", f"{node.name}.{m.name}", lang,
        ))
    return chunks


def chunk_python(rel_path: str, source: str) -> list[Chunk]:
    lang = "py"
    try:
        tree = ast.parse(source, filename=rel_path)
    except SyntaxError:
        fallback = _emit_code_chunks(rel_path, source, 0, len(source), "code_fallback", "<unparseable>", lang)
        for i, c in enumerate(fallback):
            c.chunk_id = i
        return fallback
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
        chunks.extend(_emit_code_chunks(rel_path, body, first_s, last_e, "module", "<module>", lang))
    for d in top_defs:
        if isinstance(d, ast.ClassDef):
            chunks.extend(_chunk_python_class(rel_path, source, line_offs, d, lang))
        else:
            s, e = _node_span(d, line_offs, src_len)
            kind = "function"
            chunks.extend(_emit_code_chunks(rel_path, source[s:e], s, e, kind, d.name, lang))
    chunks.sort(key=lambda c: c.char_start)
    for i, c in enumerate(chunks):
        c.chunk_id = i
    return chunks


# ---------------------------------------------------------------------------
# Jupyter notebook chunking — code cells only
# ---------------------------------------------------------------------------

def chunk_ipynb(rel_path: str, source: str) -> list[Chunk]:
    """Extract code cells from a Jupyter notebook and chunk them as
    Python.

    Markdown cells and cell outputs are IGNORED (per design — we want
    runnable patterns, not commentary or rendered output). Cells are
    concatenated with a separator comment so the chunker sees them
    as one synthetic Python file and can apply AST-aware chunking.

    Raises ``ValueError`` on a malformed notebook (not valid JSON,
    or no ``cells`` array). No silent skip — a corrupt notebook is
    a real problem the user should know about.
    """
    import json as _json
    try:
        nb = _json.loads(source)
    except _json.JSONDecodeError as exc:
        raise ValueError(
            f"notebook {rel_path}: invalid JSON ({exc})"
        ) from exc
    if not isinstance(nb, dict) or "cells" not in nb:
        raise ValueError(
            f"notebook {rel_path}: missing 'cells' array — not a "
            f"Jupyter notebook?"
        )
    cells = nb["cells"]
    if not isinstance(cells, list):
        raise ValueError(
            f"notebook {rel_path}: 'cells' is {type(cells).__name__}, "
            f"expected list"
        )
    code_parts: list[str] = []
    for i, cell in enumerate(cells):
        if not isinstance(cell, dict):
            continue
        if cell.get("cell_type") != "code":
            continue
        src = cell.get("source", "")
        # Notebook sources are typically lists of lines but can be
        # strings (older format / nbformat<4).
        if isinstance(src, list):
            text = "".join(src)
        elif isinstance(src, str):
            text = src
        else:
            continue
        if not text.strip():
            continue
        code_parts.append(f"# --- cell {i} ---\n{text.rstrip()}\n")
    if not code_parts:
        return []
    synthesised = "\n".join(code_parts)
    return chunk_python(rel_path, synthesised)


# ---------------------------------------------------------------------------
# Code chunking — JS / TS via regex-bounded top-level declarations
#
# Not as precise as AST (tree-sitter would be) but cheap and robust:
# we walk balanced braces from each top-level declaration to find the
# end. Covers ``function foo(){...}``, ``class Foo {...}``, ``export
# function ...``, ``export class ...``, ``const foo = (...) => {...}``
# (arrow functions at top level), and ``export const foo = ...``.
# Anything between top-level definitions becomes the ``module`` chunk.
# ---------------------------------------------------------------------------

JS_HARD_MAX = 3500
JS_OVERLAP = 200

# Top-level boundary candidates. Each match captures the symbol name.
# Anchored at line start to skip method definitions inside classes
# (which are 2-space indented in canonical formatting).
_JS_TOP = re.compile(
    r"""(?xm)
    ^                                       # line start
    (?:export\s+(?:default\s+)?(?:async\s+)?)?
    (?:
      (?P<fn>function\s+(?P<fname>[A-Za-z_$][\w$]*))     # function foo
      |
      (?P<cls>class\s+(?P<cname>[A-Za-z_$][\w$]*))       # class Foo
      |
      (?P<arrow>(?:const|let|var)\s+(?P<aname>[A-Za-z_$][\w$]*)\s*=\s*
                (?:async\s+)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>)
    )
    """,
)

# Java / Xtend top-level + method declarations. Bumps chunk granularity
# from "whole file as code_fallback" to per-class / per-method when we
# index the Eclipse ELK monorepo. Same kind/symbol metadata shape as
# the JS regex so downstream code doesn't care which language it was.
_JAVA_TOP = re.compile(
    r"""(?xm)
    ^                                       # line start
    \s*
    (?:@\w+(?:\([^)]*\))?\s+)*              # annotations (skip)
    (?:public|protected|private|abstract|static|final|synchronized|default|native|strictfp|\s)*
    (?:
      # class / interface / enum / record / annotation type
      (?P<cls>(?:class|interface|enum|record|@interface)\s+(?P<cname>[A-Za-z_$][\w$]*))
      |
      # method:  <ReturnType> name(  with body opening on same or next line
      (?P<fn>(?:[A-Za-z_$][\w$<>,\s\[\]?&]*?\s+)
            (?P<fname>[A-Za-z_$][\w$]*)\s*\([^)]*\)\s*
            (?:throws\s+[\w\s,]+\s*)?\{)
    )
    """,
)


def _find_block_end(source: str, brace_pos: int) -> int:
    """Walk forward from a ``{`` at ``brace_pos`` to its matching ``}``,
    respecting string literals (incl. template strings) and line/block
    comments. Returns the index AFTER the closing brace, or len(source)
    if unbalanced."""
    n = len(source)
    depth = 0
    i = brace_pos
    while i < n:
        ch = source[i]
        if ch == "{":
            depth += 1
            i += 1
            continue
        if ch == "}":
            depth -= 1
            i += 1
            if depth == 0:
                return i
            continue
        if ch == "/" and i + 1 < n:
            nxt = source[i + 1]
            if nxt == "/":
                j = source.find("\n", i + 2)
                i = n if j == -1 else j + 1
                continue
            if nxt == "*":
                j = source.find("*/", i + 2)
                i = n if j == -1 else j + 2
                continue
        if ch in ("'", '"', "`"):
            quote = ch
            i += 1
            while i < n:
                c2 = source[i]
                if c2 == "\\":
                    i += 2
                    continue
                if c2 == quote:
                    i += 1
                    break
                if quote == "`" and c2 == "$" and i + 1 < n and source[i + 1] == "{":
                    # template-literal interpolation — recurse on the inner brace
                    inner_end = _find_block_end(source, i + 1)
                    i = inner_end
                    continue
                i += 1
            continue
        i += 1
    return n


def chunk_js_ts(rel_path: str, source: str, lang: str) -> list[Chunk]:
    """Top-level function / class / arrow-function granularity. Falls
    back to a single ``code_fallback`` chunk if no top-level decls are
    found (or for files smaller than the hard cap).

    Used for JS/TS AND for Java/Xtend — Java uses a different regex
    (``_JAVA_TOP``) but the same chunking pipeline: find top-level
    declarations, span each from its head to its matching ``}``, emit
    one chunk per. Anything in between (license headers, imports) is
    discarded — the LLM doesn't need it.
    """
    n = len(source)
    pattern = _JAVA_TOP if lang in {"java", "xtend"} else _JS_TOP
    matches = list(pattern.finditer(source))
    if not matches:
        if n == 0:
            return []
        fallback = _emit_code_chunks(rel_path, source, 0, n, "code_fallback", "<file>", lang)
        for i, c in enumerate(fallback):
            c.chunk_id = i
        return fallback

    # Determine each match's span (decl through matching `}` or
    # statement terminator).
    spans: list[tuple[int, int, str, str]] = []  # (start, end, kind, symbol)
    for m in matches:
        start = m.start()
        gd = m.groupdict()
        if gd.get("fn"):
            kind = "function"
            symbol = gd["fname"]
        elif gd.get("cls"):
            kind = "class"
            symbol = gd["cname"]
        else:
            kind = "function"
            symbol = gd["aname"]
        # Find the `{` that opens the body, then walk to its match.
        brace = source.find("{", m.end())
        if brace == -1 or brace - m.end() > 200:
            # Arrow funcs can be expression-bodied: `const f = () => x;`.
            # Take through the next `;` or newline.
            term = source.find(";", m.end())
            end = (term + 1) if term != -1 else source.find("\n", m.end())
            if end == -1:
                end = n
        else:
            end = _find_block_end(source, brace)
        spans.append((start, end, kind, symbol))

    # Resolve overlaps (nested decls — keep outer only).
    spans.sort(key=lambda t: (t[0], -t[1]))
    pruned: list[tuple[int, int, str, str]] = []
    last_end = -1
    for s, e, k, sym in spans:
        if s < last_end:
            continue
        pruned.append((s, e, k, sym))
        last_end = e

    chunks: list[Chunk] = []
    # Module preamble = everything before the first top-level decl.
    if pruned and pruned[0][0] > 0:
        head = source[:pruned[0][0]]
        if head.strip():
            chunks.extend(_emit_code_chunks(rel_path, head, 0, pruned[0][0], "module", "<module>", lang))
    for s, e, kind, sym in pruned:
        chunks.extend(_emit_code_chunks(rel_path, source[s:e], s, e, kind, sym, lang))
    # Trailing material after the last decl.
    if pruned and pruned[-1][1] < n:
        tail = source[pruned[-1][1]:n]
        if tail.strip():
            chunks.extend(_emit_code_chunks(rel_path, tail, pruned[-1][1], n, "module", "<module:tail>", lang))

    chunks.sort(key=lambda c: c.char_start)
    for i, c in enumerate(chunks):
        c.chunk_id = i
    return chunks


# ---------------------------------------------------------------------------
# Per-repo file discovery for the code corpus
# ---------------------------------------------------------------------------

# Per-repo include rules. Each entry maps a repo dir name (under
# lib-sources/) to ``(roots_relative_to_repo, allowed_extensions)``.
# Roots are walked recursively; allowed extensions are matched
# case-insensitively. Anything outside the roots is skipped.
CODE_RULES: dict[str, list[tuple[str, set[str]]]] = {
    "jinja": [
        # Jinja2 Python source — the template engine implementation.
        ("src/jinja2", {".py"}),
        # RST reference docs — API contracts, template language spec,
        # extensions, sandbox. The LLM reads these to know what's
        # available without grepping the implementation.
        ("docs",       {".rst", ".md"}),
        # Bundled example templates.
        ("examples",   {".py", ".html", ".txt"}),
    ],
    "elkjs": [
        # Thin JS/TS wrapper around the GWT-compiled bundle. The
        # actual algorithm source lives in `lib-sources/elk/` (the
        # upstream Eclipse ELK Java monorepo) — indexed separately
        # below.
        ("src", {".ts", ".js"}),
        ("typings", {".ts", ".d.ts"}),
    ],
    "elk": [
        # Full Eclipse ELK Java monorepo. We index every form of
        # canonical source — the goal is the most complete local
        # representation of ELK:
        #
        #   .java          — algorithm implementations (Layered/Stress/
        #                    MrTree/Force/Radial/Disco/...)
        #   .xtend         — Xtend support code (Eclipse-flavoured
        #                    sugar over Java)
        #   .melk          — option-definition DSL (declares all
        #                    elk.layered.* / elk.spacing.* keys + enums)
        #   .ecore         — EMF metamodel for the ELK graph data
        #                    structure (canonical "what fields exist
        #                    on a node/edge/port")
        #   .xtext         — Xtext grammars (defines ELK Text DSL +
        #                    JSON-text + GRandom syntax)
        #   .elkt          — sample ELK Text graphs with options
        #   .md            — plugin READMEs + design notes
        ("plugins", {".java", ".xtend", ".melk", ".ecore", ".xtext",
                     ".elkt", ".md"}),
        ("docs",    {".md"}),
        # The test/ tree carries ~188 .java files demonstrating real
        # algorithm usage, option combinations, and regression cases.
        # `_SKIP_DIR_NAMES` would normally drop these — repos in
        # `_INCLUDE_TEST_DIRS` opt back in.
        ("test",    {".java", ".xtend", ".elkt"}),
    ],
    "three.js": [
        # src/ — public API classes. nodes/ (WebGPU shader graph, 216 files)
        # and renderers/ (internal GL/WebGPU machinery, 289 files) are stripped
        # by _SKIP_THREE_SRC_DIRS. Everything else is public API used in scripts.
        ("src",          {".js"}),
        # Canonical API reference from docs.threejs.org (789 markdown files).
        ("docs",         {".md"}),
        ("manual",       {".md"}),
        # jsm addons — OrbitControls, GLTFLoader, EffectComposer, loaders, etc.
        ("examples/jsm", {".js"}),
    ],
}

_SKIP_DIR_NAMES = {
    "__pycache__", ".git", "node_modules", "dist", "build",
    ".pixi", ".ipynb_checkpoints", "tests", "test", "__tests__",
    ".turbo", ".next", "coverage", ".storybook", "storybook",
}

# three.js src/ subtrees to skip — WebGPU shader node graph and internal
# renderer machinery. 505 files, zero value for typical script authoring.
_SKIP_THREE_SRC_DIRS = {"nodes", "renderers"}

# ELK plugin packages to skip entirely — generated parsers, IDE UI widgets,
# Eclipse GMF connector, Xtext/EMF tooling. None of this is useful for
# understanding ELK layout options or algorithm behaviour.
_SKIP_ELK_PACKAGES = {
    "src-gen",          # ANTLR-generated parser Java (260K lines of noise)
    "core.debug",       # debugging infrastructure
    "core.meta",        # Xtext metamodel for .melk DSL — IDE tooling only
    "conn.gmf",         # Eclipse GMF diagram connector
    "graph.text",       # Xtext DSL parser plugin
    "graph.json.text",  # JSON-text DSL parser plugin
}

# Repos that EXPLICITLY want their test trees indexed (overrides the
# `tests` / `test` entries in _SKIP_DIR_NAMES for path filtering).
# Used for libraries whose tests document API usage well enough that
# the LLM benefits from reading them — e.g. Eclipse ELK, whose
# algorithm tests are the canonical "how do I configure X" recipes.
_INCLUDE_TEST_DIRS: set[str] = {"elk"}

# Files larger than this are likely generated / minified / data —
# skip rather than spend embeddings on them.
_MAX_FILE_BYTES = 250_000


def discover_code_files(
    repo_filter: set[str] | None = None,
) -> list[tuple[str, Path, str]]:
    """Returns ``[(repo, abs_path, lang), ...]`` ready for chunking.

    ``repo_filter``, if given, restricts results to repos whose
    directory name is in the set. Default = all repos.
    """
    out: list[tuple[str, Path, str]] = []
    if not LIBS_DIR.is_dir():
        return out
    for repo_dir in sorted(LIBS_DIR.iterdir()):
        if not repo_dir.is_dir() or repo_dir.name.startswith("."):
            continue
        if repo_filter is not None and repo_dir.name not in repo_filter:
            continue
        rules = CODE_RULES.get(repo_dir.name)
        if rules is None:
            # Unknown repo with no rule entry. Default to indexing
            # nothing — add to CODE_RULES if you want this repo in.
            # For tests / one-offs the caller can monkeypatch CODE_RULES.
            continue
        for root_rel, exts in rules:
            root = repo_dir / root_rel
            if not root.is_dir():
                continue
            skip_dirs = _SKIP_DIR_NAMES
            if repo_dir.name in _INCLUDE_TEST_DIRS:
                skip_dirs = _SKIP_DIR_NAMES - {"test", "tests", "__tests__"}
            for p in root.rglob("*"):
                if not p.is_file():
                    continue
                rel_parts = p.relative_to(repo_dir).parts
                if any(part in skip_dirs for part in rel_parts):
                    continue
                # three.js: skip nodes/ and renderers/ anywhere under src/
                if repo_dir.name == "three.js" and any(
                    part in _SKIP_THREE_SRC_DIRS for part in rel_parts
                ):
                    continue
                ext = p.suffix.lower()
                if ext not in exts:
                    continue
                # ELK: skip generated/IDE-tooling packages; for Java also
                # skip internal algorithm phase implementations — only
                # options/ directories and test/ files are useful for scripting.
                if repo_dir.name == "elk":
                    if any(any(pkg in part for pkg in _SKIP_ELK_PACKAGES)
                           for part in rel_parts):
                        continue
                    if ext == ".java":
                        in_options = any(part == "options" for part in rel_parts)
                        in_test = any(part in {"test", "tests"} for part in rel_parts)
                        if not in_options and not in_test:
                            continue
                if ext not in exts:
                    continue
                # Notebooks are JSON-bloated (cell outputs, metadata)
                # — apply a more generous size cap. After cell
                # extraction the actual code is much smaller.
                size_cap = _MAX_FILE_BYTES * 4 if ext == ".ipynb" else _MAX_FILE_BYTES
                try:
                    if p.stat().st_size > size_cap:
                        continue
                except OSError:
                    continue
                lang = ext.lstrip(".")
                out.append((repo_dir.name, p, lang))
    return out


# ---------------------------------------------------------------------------
# Indexer
# ---------------------------------------------------------------------------

@dataclass
class CorpusConfig:
    name: str
    chroma_dir: Path
    bm25_dir: Path
    collection_name: str
    extra_metadata_fields: list[str] = field(default_factory=list)


DOCS_CFG = CorpusConfig(
    name="docs",
    chroma_dir=RAG_DIR / ".chroma",
    bm25_dir=RAG_DIR / ".bm25",
    collection_name="docs",
)

CODE_CFG = CorpusConfig(
    name="code",
    chroma_dir=RAG_DIR / ".chroma_code",
    bm25_dir=RAG_DIR / ".bm25_code",
    collection_name="code",
    extra_metadata_fields=["repo", "path", "folder", "lang", "kind", "symbol"],
)


def reset_dirs(cfg: CorpusConfig) -> None:
    for d in (cfg.chroma_dir, cfg.bm25_dir):
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True, exist_ok=True)


def _make_embedding_fn():
    """Return a chromadb embedding function that uses CoreML (Apple Neural Engine)
    when available, falling back to CPU onnxruntime.

    CoreML can be 4–8× faster than CPU on Apple Silicon for MiniLM inference.
    We try to activate it by passing preferred_providers to onnxruntime; if
    onnxruntime-extensions or the CoreML EP is absent we fall back silently.
    """
    try:
        import onnxruntime as ort
        available = [ep for ep in ort.get_available_providers()
                     if ep in ("CoreMLExecutionProvider", "CPUExecutionProvider")]
        if "CoreMLExecutionProvider" in available:
            providers = ["CoreMLExecutionProvider", "CPUExecutionProvider"]
            print("  embedder: onnxruntime CoreML (Neural Engine / GPU)")
        else:
            providers = ["CPUExecutionProvider"]
            print("  embedder: onnxruntime CPU")
    except Exception:
        providers = None
        print("  embedder: chromadb default")

    if providers is None:
        return None  # let chromadb pick

    try:
        from chromadb.utils.embedding_functions import ONNXMiniLM_L6_V2
        ef = ONNXMiniLM_L6_V2(preferred_providers=providers)
        # Warm-up: confirm it actually works before we commit to it
        ef(["warm-up"])
        return ef
    except Exception as exc:
        print(f"  embedder: CoreML init failed ({exc}), falling back to default")
        return None


# Module-level singleton so we initialise once per process (model load is slow).
_embedding_fn = None


def _get_embedding_fn():
    global _embedding_fn
    if _embedding_fn is None:
        _embedding_fn = _make_embedding_fn()
    return _embedding_fn


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

    ef = _get_embedding_fn()
    coll_kwargs: dict = {"name": cfg.collection_name, "metadata": {"hnsw:space": "cosine"}}
    if ef is not None:
        coll_kwargs["embedding_function"] = ef
    coll = client.create_collection(**coll_kwargs)

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

    # Large batch = fewer round-trips to the embedder; 512 fits comfortably
    # in memory and saturates the CoreML EP without OOM risk on 16 GB.
    BATCH = 512
    total = len(ids)
    for i in range(0, total, BATCH):
        coll.add(
            ids=ids[i:i + BATCH],
            documents=documents[i:i + BATCH],
            metadatas=metadatas[i:i + BATCH],
        )
        done = min(i + BATCH, total)
        if i == 0 or done == total or (i // BATCH) % 4 == 0:
            print(f"    embedded {done}/{total} chunks")
    print(f"  dense: indexed {total} chunks → {cfg.chroma_dir}")


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
    print(f"  manifest: {path}  ({len(chunks)} chunks across {len(manifest['files'])} files)")


# ---------------------------------------------------------------------------
# Corpus assemblers
# ---------------------------------------------------------------------------

def assemble_docs_chunks() -> list[Chunk]:
    chunks: list[Chunk] = []
    if DOCS_DIR.exists():
        files = sorted(p for p in DOCS_DIR.rglob("*.md") if p.is_file())
        print(f"docs/: {len(files)} markdown file(s)")
        for f in files:
            cs = chunk_prose(f, DOCS_DIR)
            chunks.extend(cs)
            print(f"  {f.relative_to(DOCS_DIR)}: {len(cs)} chunks")
    else:
        print(f"docs/ not found at {DOCS_DIR}", file=sys.stderr)

    plugin_pairs: list[tuple[Path, Path]] = []
    if PLUGINS_DIR.is_dir():
        for docs_dir in sorted(PLUGINS_DIR.rglob("docs")):
            if not docs_dir.is_dir():
                continue
            for md in sorted(docs_dir.rglob("*.md")):
                if md.is_file():
                    plugin_pairs.append((PLUGINS_DIR, md))
    if plugin_pairs:
        n_plugins = len({p[1].parent.parent for p in plugin_pairs})
        print(f"plugins: {len(plugin_pairs)} markdown file(s) across {n_plugins} plugin(s)")
        for base, md in plugin_pairs:
            cs = chunk_prose(md, base)
            chunks.extend(cs)
            print(f"  {md.relative_to(base)}: {len(cs)} chunks")
    else:
        print("plugins: no plugin docs found (optional — skipping)")
    return chunks


def assemble_code_chunks(
    limit: int = 0, repo_filter: set[str] | None = None,
) -> list[Chunk]:
    if not LIBS_DIR.is_dir():
        print(f"lib-sources/ not found at {LIBS_DIR}", file=sys.stderr)
        return []
    files = discover_code_files(repo_filter=repo_filter)
    if limit:
        files = files[:limit]
    if not files:
        scope = "any repo" if repo_filter is None else f"repos {sorted(repo_filter)!r}"
        print(f"no indexable code files found under lib-sources/ for {scope}", file=sys.stderr)
        return []

    by_repo: dict[str, int] = {}
    chunks: list[Chunk] = []
    n_skipped = 0
    for repo, path, lang in files:
        repo_dir = LIBS_DIR / repo
        try:
            rel_path = str(path.relative_to(repo_dir))
        except ValueError:
            rel_path = str(path)
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            print(f"  skip {repo}/{rel_path}: {exc}", file=sys.stderr)
            n_skipped += 1
            continue
        if lang == "py":
            cs = chunk_python(rel_path, source)
        elif lang == "ipynb":
            # Jupyter notebook: extract code cells (markdown cells and
            # outputs are ignored — see ``chunk_ipynb``), then chunk
            # the synthesised Python.
            cs = chunk_ipynb(rel_path, source)
        elif lang in {"md", "markdown", "rst", "txt"}:
            # Prose chunking for documentation embedded under code
            # repos (e.g. lib-sources/elk/docs/*.md, plugin READMEs).
            cs = chunk_prose(path, repo_dir)
            # `chunk_prose` returns chunks with `file` set to the path
            # relative to repo_dir, so the repo-stamping below works
            # without modification.
        else:
            cs = chunk_js_ts(rel_path, source, lang)
        # Stamp repo/path/folder metadata and re-namespace the ``file``
        # field so the docs and code corpora can coexist without
        # ambiguity. ``file`` becomes ``<repo>/<path>``.
        folder = str(Path(rel_path).parent)
        if folder == ".":
            folder = ""
        for c in cs:
            c.repo = repo
            c.path = rel_path
            c.folder = folder
            c.file = f"{repo}/{rel_path}"
        chunks.extend(cs)
        by_repo[repo] = by_repo.get(repo, 0) + len(cs)

    print(f"code: {len(chunks)} chunks (skipped {n_skipped} file(s))")
    for repo, count in sorted(by_repo.items()):
        print(f"  {repo:14s} {count} chunks")
    # Globally re-number chunk_id so ids are unique across the corpus.
    # (Per-file chunk_ids were set by the per-file chunkers; we keep
    # those values via the ``file`` key when reading back, but for the
    # corpus-level id we add a counter.)
    # Actually the runtime indexer keys on ``(file, chunk_id)`` so the
    # per-file ids are fine — leave them alone.
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
        description="Build dense + sparse RAG indexes (docs and/or code corpora).",
    )
    p.add_argument(
        "--corpus", choices=("docs", "code", "both"), default="both",
        help="Which corpus to (re)build. Default: both.",
    )
    p.add_argument(
        "--repo",
        help=(
            "(code only) Comma-separated list of repo names under "
            "lib-sources/ to index (e.g. 'panel' or 'panel,three.js'). "
            "Default = all repos. NOTE: each run is a full rewrite, so "
            "this REPLACES the code corpus with chunks from only the "
            "named repos — useful for fast iteration / testing, not for "
            "incremental updates."
        ),
    )
    p.add_argument(
        "--code-limit", type=int, default=0,
        help="(code only) If > 0, only index this many files (debug).",
    )
    args = p.parse_args(argv)

    repo_filter: set[str] | None = None
    if args.repo:
        repo_filter = {r.strip() for r in args.repo.split(",") if r.strip()}

    built_any = False
    if args.corpus in ("docs", "both"):
        built_any |= _run_corpus("docs", assemble_docs_chunks(), DOCS_CFG)
    if args.corpus in ("code", "both"):
        built_any |= _run_corpus(
            "code",
            assemble_code_chunks(args.code_limit, repo_filter=repo_filter),
            CODE_CFG,
        )

    if not built_any:
        return 2
    print("\ndone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
