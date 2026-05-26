"""Tests for the RAG chunker.

The embedder is ``sentence-transformers/all-MiniLM-L6-v2`` via Chroma's
ONNX default. Its hard input limit is 256 tokens. The chunker MUST emit
chunks small enough to fit — otherwise the embedder silently truncates
and the dense vector reflects only the first ~1000 chars of each chunk.

These tests defend the size invariant from multiple angles:

* synthetic Python / JS / Java that would have overflowed the old
  cap of 3500
* the prose chunker's previously-broken handling of single large
  paragraphs
* notebook code-cell extraction (markdown / outputs ignored)
* a real round-trip through the MiniLM tokenizer to confirm chunks
  do not exceed 256 tokens
"""

from __future__ import annotations

import json
from types import ModuleType

import pytest


# ============================================================
# Code chunking — size invariants
# ============================================================


def test_python_chunks_under_cap_for_huge_function(build_rag: ModuleType) -> None:
    """A 50 KB function body must be split into multiple chunks, each
    within PY_HARD_MAX. Catches regressions of the old
    ``max(PY_HARD_MAX - len(header), 500)`` overflow."""
    body = "    x = 1\n" * 5000  # ~50 KB
    source = f"def huge():\n{body}\n"
    chunks = build_rag.chunk_python("synthetic/huge.py", source)
    assert chunks, "must emit at least one chunk"
    for c in chunks:
        assert len(c.text) <= build_rag.PY_HARD_MAX, (
            f"chunk {c.symbol} part overflow: {len(c.text)} > {build_rag.PY_HARD_MAX}"
        )


def test_python_chunks_under_cap_for_huge_class(build_rag: ModuleType) -> None:
    """A class containing one massive method must window the method
    body, not the class as a whole."""
    method_body = "        y = 1\n" * 3000
    source = (
        "class Big:\n"
        "    def m(self):\n"
        f"{method_body}\n"
    )
    chunks = build_rag.chunk_python("synthetic/big.py", source)
    assert chunks
    for c in chunks:
        assert len(c.text) <= build_rag.PY_HARD_MAX


def test_js_fallback_under_cap(build_rag: ModuleType) -> None:
    """Unparseable JS (no top-level decls) goes through the
    code_fallback windowing path. That path historically emitted
    214 oversized chunks because the part-header overhead wasn't
    accounted for. Synthesize a body that triggers the corner case."""
    source = "// no top-level fn or class here\n" + ("data data data " * 5000)
    chunks = build_rag.chunk_js_ts("synthetic/blob.js", source, "js")
    assert chunks
    for c in chunks:
        assert len(c.text) <= build_rag.PY_HARD_MAX


def test_java_fallback_under_cap(build_rag: ModuleType) -> None:
    """Java files without a recognised top-level decl also use the
    fallback path. Regression for the elk monorepo path."""
    source = "// random java preamble\n" + ("int x = 1; " * 5000)
    chunks = build_rag.chunk_js_ts("synthetic/Blob.java", source, "java")
    assert chunks
    for c in chunks:
        assert len(c.text) <= build_rag.PY_HARD_MAX


def test_chunk_python_class_method_kinds(build_rag: ModuleType) -> None:
    """Known-shape input: chunker must produce expected kinds + symbols."""
    source = (
        "import os\n"
        "\n"
        "def top_fn():\n"
        "    return 1\n"
        "\n"
        "class Foo:\n"
        "    def method_a(self):\n"
        "        pass\n"
        "    def method_b(self, x):\n"
        "        return x + 1\n"
    )
    chunks = build_rag.chunk_python("ex.py", source)
    kinds = [(c.kind, c.symbol) for c in chunks]
    # Module preamble + top function + class header + 2 methods
    assert ("module", "<module>") in kinds
    assert ("function", "top_fn") in kinds
    assert ("class_header", "Foo") in kinds
    assert ("method", "Foo.method_a") in kinds
    assert ("method", "Foo.method_b") in kinds


def test_chunk_python_unparseable_falls_back(build_rag: ModuleType) -> None:
    """Syntax errors produce a code_fallback chunk, not a crash."""
    source = "def broken(\n   no closing paren\n"
    chunks = build_rag.chunk_python("broken.py", source)
    assert chunks
    assert any(c.kind == "code_fallback" for c in chunks)


def test_emit_code_chunks_overlap(build_rag: ModuleType) -> None:
    """When a body is windowed, adjacent chunks must share the
    configured number of overlap chars so context isn't lost at
    the boundary."""
    body = "x" * (build_rag.PY_HARD_MAX * 3)
    chunks = build_rag._emit_code_chunks(
        "ovl.py", body, 0, len(body), "function", "biggie", "py",
    )
    assert len(chunks) >= 2
    for i in range(1, len(chunks)):
        # char_start of chunk i should be (end of chunk i-1) - PY_OVERLAP.
        prev_end = chunks[i - 1].char_end
        cur_start = chunks[i].char_start
        # Allow ±1 char rounding from header math.
        assert prev_end - cur_start == build_rag.PY_OVERLAP, (
            f"overlap broken between part {i-1} and {i}: "
            f"prev_end={prev_end}, cur_start={cur_start}"
        )


def test_emit_code_chunks_oversized_header_raises(build_rag: ModuleType) -> None:
    """If someone bumps PY_HARD_MAX down to a value smaller than the
    minimum header overhead, fail loud rather than emit truncated
    chunks."""
    # Synthesize an absurdly long symbol so header alone exceeds cap.
    long_path = "a/" * 500 + "f.py"
    with pytest.raises(ValueError, match="larger than PY_HARD_MAX"):
        build_rag._emit_code_chunks(
            long_path, "x" * 2000, 0, 2000, "function", "name", "py",
        )


# ============================================================
# Prose chunking — size invariants
# ============================================================


def test_prose_chunks_under_cap(build_rag: ModuleType, tmp_path) -> None:
    """A single oversized paragraph must be split — previously
    ``merge_blocks`` accepted single blocks larger than hard_max."""
    huge_para = "word " * 5000  # ~25 KB single paragraph
    p = tmp_path / "huge.md"
    p.write_text(huge_para)
    chunks = build_rag.chunk_prose(p, tmp_path)
    assert chunks
    for c in chunks:
        assert len(c.text) <= build_rag.DOC_HARD_MAX, (
            f"prose chunk overflow: {len(c.text)} > {build_rag.DOC_HARD_MAX}"
        )


def test_prose_chunks_handle_mixed_sizes(build_rag: ModuleType, tmp_path) -> None:
    """A doc with one tiny + one huge paragraph splits without
    losing the tiny one or oversizing the chunk that holds it."""
    text = "Short intro.\n\n" + ("big " * 5000)
    p = tmp_path / "mixed.md"
    p.write_text(text)
    chunks = build_rag.chunk_prose(p, tmp_path)
    assert len(chunks) >= 2
    for c in chunks:
        assert len(c.text) <= build_rag.DOC_HARD_MAX


# ============================================================
# Notebook chunking — code cells only
# ============================================================


def _nb(cells: list[dict]) -> str:
    return json.dumps({
        "cells": cells,
        "metadata": {"kernelspec": {"name": "python3"}},
        "nbformat": 4, "nbformat_minor": 5,
    })


def test_ipynb_code_cells_only(build_rag: ModuleType) -> None:
    """Markdown cells must NOT appear in the chunked output. Code
    cells must — concatenated and chunked as Python."""
    nb = _nb([
        {"cell_type": "markdown", "metadata": {}, "source": ["# Intro\n", "Hello world\n"]},
        {"cell_type": "code", "metadata": {},
         "source": ["import numpy as np\n", "x = np.zeros(10)\n"],
         "outputs": [], "execution_count": 1},
        {"cell_type": "markdown", "metadata": {}, "source": "## Aside"},
        {"cell_type": "code", "metadata": {},
         "source": "def foo():\n    return 42\n",
         "outputs": [], "execution_count": 2},
    ])
    chunks = build_rag.chunk_ipynb("nb.ipynb", nb)
    assert chunks
    joined = "\n".join(c.text for c in chunks)
    assert "numpy" in joined
    assert "def foo" in joined
    # Markdown content must be absent.
    assert "Intro" not in joined
    assert "Hello world" not in joined
    assert "Aside" not in joined


def test_ipynb_outputs_ignored(build_rag: ModuleType) -> None:
    """Cell outputs (stdout / images / errors) must not be included
    — only the source field."""
    nb = _nb([
        {"cell_type": "code", "metadata": {},
         "source": "print('alpha')\n",
         "outputs": [
             {"output_type": "stream", "name": "stdout", "text": ["zulu zulu zulu\n"]},
         ],
         "execution_count": 1},
    ])
    chunks = build_rag.chunk_ipynb("nb.ipynb", nb)
    joined = "\n".join(c.text for c in chunks)
    assert "alpha" in joined
    assert "zulu" not in joined, "cell output leaked into chunk text"


def test_ipynb_empty_returns_empty(build_rag: ModuleType) -> None:
    """A notebook with only markdown cells yields zero chunks."""
    nb = _nb([
        {"cell_type": "markdown", "metadata": {}, "source": ["# Just docs"]},
    ])
    assert build_rag.chunk_ipynb("nb.ipynb", nb) == []


def test_ipynb_malformed_json_raises(build_rag: ModuleType) -> None:
    """Junk JSON must raise — no silent skip."""
    with pytest.raises(ValueError, match="invalid JSON"):
        build_rag.chunk_ipynb("nb.ipynb", "{this isn't JSON")


def test_ipynb_missing_cells_raises(build_rag: ModuleType) -> None:
    """A JSON document missing the ``cells`` array isn't a notebook
    — must raise rather than silently produce nothing."""
    with pytest.raises(ValueError, match="cells"):
        build_rag.chunk_ipynb("nb.ipynb", json.dumps({"metadata": {}}))


def test_ipynb_cells_not_a_list_raises(build_rag: ModuleType) -> None:
    with pytest.raises(ValueError, match="expected list"):
        build_rag.chunk_ipynb(
            "nb.ipynb", json.dumps({"cells": "not a list"}),
        )


def test_ipynb_source_string_or_list(build_rag: ModuleType) -> None:
    """Notebook `source` can be either a list-of-lines (nbformat 4)
    or a single string (older / nbformat 3 / hand-authored)."""
    nb = _nb([
        {"cell_type": "code", "metadata": {},
         "source": "list_form = False\n", "outputs": []},
        {"cell_type": "code", "metadata": {},
         "source": ["list_form = True\n", "x = 1\n"], "outputs": []},
    ])
    chunks = build_rag.chunk_ipynb("nb.ipynb", nb)
    joined = "\n".join(c.text for c in chunks)
    assert "list_form = False" in joined
    assert "list_form = True" in joined


def test_ipynb_chunks_under_cap(build_rag: ModuleType) -> None:
    """A notebook with one huge code cell must window correctly."""
    huge = "x = 1\n" * 5000
    nb = _nb([
        {"cell_type": "code", "metadata": {},
         "source": huge, "outputs": []},
    ])
    chunks = build_rag.chunk_ipynb("nb.ipynb", nb)
    assert chunks
    for c in chunks:
        assert len(c.text) <= build_rag.PY_HARD_MAX


# ============================================================
# Embedder fit — the ultimate test
# ============================================================


def test_chunks_fit_minilm_tokenizer(build_rag: ModuleType) -> None:
    """Round-trip: load the actual MiniLM tokenizer Chroma uses and
    confirm chunks at our hard-max sizes tokenize to ≤ 256 tokens
    for content we actually index.

    This catches the failure mode that motivated the whole rework:
    chunks that LOOK fine on a char count but tokenize past the
    embedder's window get silently truncated. If this test fails,
    PY_HARD_MAX or DOC_HARD_MAX is too aggressive — lower them.

    The MiniLM model used by Chroma is
    ``sentence-transformers/all-MiniLM-L6-v2`` (256-token cap).

    We sample REAL code/prose from the actual corpus rather than
    constructing pathological worst cases — the cap is set by what
    we'll actually index, not by what could theoretically be passed.
    """
    try:
        from transformers import AutoTokenizer
    except ImportError:
        pytest.skip("transformers not installed")
    tok = AutoTokenizer.from_pretrained(
        "sentence-transformers/all-MiniLM-L6-v2",
    )

    import pathlib
    repo_root = pathlib.Path(__file__).resolve().parents[3]
    lib_sources = repo_root / "lib-sources"
    docs_dir = repo_root / "docs"

    def _sample(globs: list[tuple[pathlib.Path, str]], length: int, n: int) -> list[str]:
        out: list[str] = []
        for base, pattern in globs:
            if not base.is_dir():
                continue
            for p in base.rglob(pattern):
                if not p.is_file():
                    continue
                try:
                    t = p.read_text(errors="replace")
                except OSError:
                    continue
                if len(t) >= length:
                    out.append(t[:length])
                if len(out) >= n:
                    return out
        return out

    # Code: sample at PY_HARD_MAX from .py and .java (the densest
    # languages we index). Each sample is body-only; we add a
    # representative header to mirror what _emit_code_chunks emits.
    code_samples = (
        _sample([(lib_sources, "*.py"), (lib_sources, "*.java")],
                build_rag.PY_HARD_MAX, n=50)
    )
    if not code_samples:
        pytest.skip("no lib-sources content available to sample")

    header = "# File: lib-sources/some/long/path/to/a/module.py\n# Symbol: SomeClass.some_method  (method)\n\n"
    worst_code = 0
    for s in code_samples:
        text = header + s
        n = len(tok(text, add_special_tokens=True, truncation=False)["input_ids"])
        worst_code = max(worst_code, n)
    assert worst_code <= 256, (
        f"PY_HARD_MAX={build_rag.PY_HARD_MAX} worst-real-code: "
        f"{worst_code} tokens > 256. Lower PY_HARD_MAX."
    )

    # Prose: sample at DOC_HARD_MAX from docs/ + .md under lib-sources.
    prose_samples = _sample(
        [(docs_dir, "*.md"), (lib_sources, "*.md")],
        build_rag.DOC_HARD_MAX, n=50,
    )
    if not prose_samples:
        pytest.skip("no prose content available to sample")
    worst_prose = 0
    for s in prose_samples:
        n = len(tok(s, add_special_tokens=True, truncation=False)["input_ids"])
        worst_prose = max(worst_prose, n)
    assert worst_prose <= 256, (
        f"DOC_HARD_MAX={build_rag.DOC_HARD_MAX} worst-real-prose: "
        f"{worst_prose} tokens > 256. Lower DOC_HARD_MAX."
    )
