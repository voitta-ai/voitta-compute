"""RAG integration tests against a tiny tmp corpus.

Skipped if ``chromadb`` / ``bm25s`` aren't installed so the suite stays
green for devs without the heavy deps.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("chromadb")
pytest.importorskip("bm25s")

# Make scripts/build_rag.py importable.
REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "scripts"))


@pytest.fixture
def tmp_corpus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Build a real (tiny) RAG index under ``tmp_path/rag``."""
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "00-overview.md").write_text(
        "# Overview\n\nVoitta is an assistant in a bookmarklet.\n"
        "## Architecture\n\nFastAPI plus Chainlit drives the agent loop.\n"
    )
    (docs / "01-plugins.md").write_text(
        "# Plugins\n\nPlugins live at plugins/<name>/. Manifests declare "
        "host_patterns and an optional python_module.\n"
    )

    plugins = tmp_path / "plugins"
    (plugins / "ebay" / "docs").mkdir(parents=True)
    (plugins / "ebay" / "docs" / "01-tools.md").write_text(
        "# eBay tools\n\nScrape the active eBay tab. No API key required.\n"
    )

    rag_dir = tmp_path / "rag"

    import build_rag  # type: ignore[import-not-found]

    monkeypatch.setattr(build_rag, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(build_rag, "DOCS_DIR", docs)
    monkeypatch.setattr(build_rag, "PLUGINS_DIR", plugins)
    # Empty lib-sources so a stray --corpus both run never walks real
    # checked-out repos. (Defensive — we still pass --corpus docs.)
    libs_empty = tmp_path / "lib-sources"
    libs_empty.mkdir()
    monkeypatch.setattr(build_rag, "LIBS_DIR", libs_empty)
    monkeypatch.setattr(build_rag, "RAG_DIR", rag_dir)
    monkeypatch.setattr(
        build_rag,
        "DOCS_CFG",
        build_rag.CorpusConfig(
            name="docs",
            chroma_dir=rag_dir / ".chroma",
            bm25_dir=rag_dir / ".bm25",
            collection_name="docs",
        ),
    )

    # Docs-only build — exercises just the docs path, ~150 ms.
    rc = build_rag.main(["--corpus", "docs"])
    assert rc == 0

    # Point the runtime loader at the tmp index.
    from app import config as config_mod
    from app.tools.rag import index as index_mod

    monkeypatch.setattr(config_mod, "RAG_DIR", rag_dir)
    monkeypatch.setattr(index_mod, "CORPORA", {
        "docs": index_mod.CorpusConfig(
            name="docs",
            chroma_dir=rag_dir / ".chroma",
            bm25_dir=rag_dir / ".bm25",
            collection_name="docs",
            description="tmp",
        ),
    })
    index_mod._reset_for_tests()
    yield tmp_path
    index_mod._reset_for_tests()


def test_query_finds_plugin_doc(tmp_corpus: Path) -> None:
    from app.tools.rag import query

    results = query("how do plugins declare host patterns", top_k=3, dense_weight=0.7)
    assert results, "expected at least one result"
    files = [r["file"] for r in results]
    # The plugin-docs file ("01-plugins.md") or the ebay plugin doc
    # should rank in the top 3 for this query.
    assert any("plugin" in f.lower() for f in files), files


def test_query_returns_ranked_metadata(tmp_corpus: Path) -> None:
    from app.tools.rag import query

    results = query("eBay scrape", top_k=2, dense_weight=0.5)
    assert results
    r = results[0]
    for key in ("file", "chunk_id", "text", "score", "dense_score", "sparse_score", "corpus"):
        assert key in r, f"missing {key} in {r}"
    assert r["corpus"] == "docs"


def test_get_range_stitches_chunks(tmp_corpus: Path) -> None:
    from app.tools.rag import get_range, query

    results = query("Overview Voitta", top_k=1, dense_weight=0.9)
    assert results
    f = results[0]["file"]
    total = results[0]["total_chunks_in_file"]
    stitched = get_range(file=f, first=0, last=total - 1)
    assert stitched["ok"] is True
    assert stitched["file"] == f
    assert stitched["text"]


@pytest.fixture
def tmp_code_corpus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Build a tiny code corpus from a synthetic lib-sources tree.

    One fake-py repo (one .py file with a class + function) plus one
    fake-js repo (one .ts file with a class + arrow function) exercise
    both chunker paths. Total ~6 chunks — builds in well under a
    second so it fits in CI budgets.
    """
    libs = tmp_path / "lib-sources"
    py_repo = libs / "fakepy" / "fakepy"
    py_repo.mkdir(parents=True)
    (py_repo / "core.py").write_text(
        "def greet(name):\n"
        "    \"\"\"Return a greeting.\"\"\"\n"
        "    return f'hello {name}'\n"
        "\n"
        "class Plotter:\n"
        "    \"\"\"Tiny synthetic plotting helper.\"\"\"\n"
        "    def __init__(self, title):\n"
        "        self.title = title\n"
        "    def render(self, data):\n"
        "        return {'title': self.title, 'data': list(data)}\n"
    )
    ts_repo = libs / "fakets" / "src"
    ts_repo.mkdir(parents=True)
    (ts_repo / "widget.ts").write_text(
        "export function makeWidget(label: string) {\n"
        "  return { label, kind: 'widget' };\n"
        "}\n"
        "\n"
        "export class WidgetStore {\n"
        "  items: string[] = [];\n"
        "  add(label: string) { this.items.push(label); }\n"
        "}\n"
    )

    rag_dir = tmp_path / "rag"

    import build_rag  # type: ignore[import-not-found]

    monkeypatch.setattr(build_rag, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(build_rag, "DOCS_DIR", tmp_path / "docs")
    monkeypatch.setattr(build_rag, "PLUGINS_DIR", tmp_path / "plugins")
    monkeypatch.setattr(build_rag, "LIBS_DIR", libs)
    monkeypatch.setattr(build_rag, "RAG_DIR", rag_dir)
    monkeypatch.setattr(
        build_rag,
        "CODE_CFG",
        build_rag.CorpusConfig(
            name="code",
            chroma_dir=rag_dir / ".chroma_code",
            bm25_dir=rag_dir / ".bm25_code",
            collection_name="code",
            extra_metadata_fields=["repo", "path", "folder", "lang", "kind", "symbol"],
        ),
    )
    # Override CODE_RULES so the synthetic repos are recognised.
    monkeypatch.setattr(build_rag, "CODE_RULES", {
        "fakepy": [("fakepy", {".py"})],
        "fakets": [("src", {".ts"})],
    })

    rc = build_rag.main(["--corpus", "code"])
    assert rc == 0, "synthetic code corpus build failed"

    from app import config as config_mod
    from app.tools.rag import index as index_mod

    monkeypatch.setattr(config_mod, "RAG_DIR", rag_dir)
    monkeypatch.setattr(index_mod, "CORPORA", {
        "code": index_mod.CorpusConfig(
            name="code",
            chroma_dir=rag_dir / ".chroma_code",
            bm25_dir=rag_dir / ".bm25_code",
            collection_name="code",
            description="tmp synthetic code",
        ),
    })
    index_mod._reset_for_tests()
    yield tmp_path
    index_mod._reset_for_tests()


def test_code_query_returns_repo_metadata(tmp_code_corpus: Path) -> None:
    """Code-corpus chunks carry repo/path/folder/lang/kind/symbol."""
    from app.tools.rag import query

    results = query("widget store", top_k=5, dense_weight=0.7, corpus="code")
    assert results, "expected at least one result"
    r = results[0]
    for key in ("repo", "path", "folder", "lang", "kind", "symbol"):
        assert key in r, f"missing {key!r} in {r}"
    assert r["repo"] in {"fakepy", "fakets"}


def test_code_query_finds_python_symbol(tmp_code_corpus: Path) -> None:
    from app.tools.rag import query

    results = query("greet name", top_k=3, dense_weight=0.3, corpus="code")
    assert results
    # Sparse-leaning query should surface the Python ``greet`` function.
    assert any(r.get("symbol") == "greet" for r in results), \
        [(r.get("repo"), r.get("symbol")) for r in results]


def test_code_query_finds_typescript_symbol(tmp_code_corpus: Path) -> None:
    from app.tools.rag import query

    results = query("WidgetStore class", top_k=3, dense_weight=0.5, corpus="code")
    assert results
    assert any(r.get("symbol") == "WidgetStore" for r in results), \
        [(r.get("repo"), r.get("symbol")) for r in results]


def test_code_repo_filter_restricts_corpus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--repo`` filter excludes other repos from the build."""
    libs = tmp_path / "lib-sources"
    (libs / "fakepy" / "fakepy").mkdir(parents=True)
    (libs / "fakepy" / "fakepy" / "a.py").write_text("def a(): return 1\n")
    (libs / "fakets" / "src").mkdir(parents=True)
    (libs / "fakets" / "src" / "b.ts").write_text("export function b() { return 2; }\n")

    rag_dir = tmp_path / "rag"
    import build_rag  # type: ignore[import-not-found]

    monkeypatch.setattr(build_rag, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(build_rag, "DOCS_DIR", tmp_path / "docs")
    monkeypatch.setattr(build_rag, "PLUGINS_DIR", tmp_path / "plugins")
    monkeypatch.setattr(build_rag, "LIBS_DIR", libs)
    monkeypatch.setattr(build_rag, "RAG_DIR", rag_dir)
    monkeypatch.setattr(build_rag, "CODE_CFG", build_rag.CorpusConfig(
        name="code",
        chroma_dir=rag_dir / ".chroma_code",
        bm25_dir=rag_dir / ".bm25_code",
        collection_name="code",
        extra_metadata_fields=["repo", "path", "folder", "lang", "kind", "symbol"],
    ))
    monkeypatch.setattr(build_rag, "CODE_RULES", {
        "fakepy": [("fakepy", {".py"})],
        "fakets": [("src", {".ts"})],
    })

    rc = build_rag.main(["--corpus", "code", "--repo", "fakepy"])
    assert rc == 0

    import json
    manifest = json.loads((rag_dir / ".bm25_code" / "manifest.json").read_text())
    repos = {c["repo"] for c in manifest["chunks"]}
    assert repos == {"fakepy"}, f"--repo filter leaked other repos: {repos}"


def test_rag_not_built_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """If indexes are absent, ``query`` raises RagNotBuilt."""
    from app import config as config_mod
    from app.tools.rag import index as index_mod

    empty = tmp_path / "empty_rag"
    monkeypatch.setattr(config_mod, "RAG_DIR", empty)
    monkeypatch.setattr(index_mod, "CORPORA", {
        "docs": index_mod.CorpusConfig(
            name="docs",
            chroma_dir=empty / ".chroma",
            bm25_dir=empty / ".bm25",
            collection_name="docs",
            description="",
        ),
    })
    index_mod._reset_for_tests()
    with pytest.raises(index_mod.RagNotBuilt):
        index_mod.load("docs")
