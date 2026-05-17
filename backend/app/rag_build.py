"""Build the RAG indexes (BM25 + Chroma) into the user data dir at
first launch.

The dev-side ``rag/build_rag.py`` script pulls heavy deps (chromadb +
sentence-transformers) and walks ``docs/`` to produce both indexes. In
the packaged .app this can't happen at build time — the bundle is
signed and read-only — so the indexer runs on the user's machine after
the heavy-package installer finishes. Outputs land in
``<PROJECT_ROOT>/rag/`` (writable user data dir).

Streams a per-phase progress callback so the install window can show
the user what's happening (file chunking, the Chroma embedding phase,
the BM25 phase). The rebuild *itself* runs out-of-process inside the
caller's thread; this module just delegates to the dev script's
already-tested helpers after re-pointing its module-level path
globals at the user data dir.

Idempotent: if both indexes already exist with the expected manifest
fields, this is a no-op.
"""

from __future__ import annotations

import shutil
import sys
import traceback
from pathlib import Path
from typing import Callable

from app.config import PROJECT_ROOT


# Same callback shape as installer.install_all so the install window's
# set_progress(current, total, label, log) works without adapter code.
ProgressCb = Callable[[int, int, str, "str | None"], None]


def output_dirs() -> tuple[Path, Path]:
    rag_root = PROJECT_ROOT / "rag"
    return rag_root / ".chroma", rag_root / ".bm25"


def _error_log_path() -> Path:
    """Persistent record of the last RAG-build failure. Written by
    :func:`build`, read by :func:`status_summary`. Cleared on success."""
    return PROJECT_ROOT / "rag" / "last_build_error.txt"


def is_built() -> bool:
    """Cheap structural check — true when both index dirs and the BM25
    manifest are on disk. Doesn't import chromadb / bm25s, so the
    first-launch fast path stays fast."""
    chroma_dir, bm25_dir = output_dirs()
    if not chroma_dir.is_dir() or not bm25_dir.is_dir():
        return False
    if not (bm25_dir / "manifest.json").is_file():
        return False
    return True


def status_summary() -> dict:
    """Snapshot for the Settings menu.

    Returns ``{built, chunk_count, files_count, last_error}``. Chunk
    counts come from the BM25 manifest (already on disk; reading it
    is a cheap JSON parse, no chromadb / bm25s imports needed).
    """
    chroma_dir, bm25_dir = output_dirs()
    out: dict = {
        "built": is_built(),
        "chunk_count": None,
        "files_count": None,
        "chroma_dir": str(chroma_dir),
        "bm25_dir": str(bm25_dir),
        "last_error": None,
    }
    err_path = _error_log_path()
    if err_path.is_file():
        try:
            out["last_error"] = err_path.read_text(encoding="utf-8")
        except OSError:
            pass
    if out["built"]:
        try:
            import json
            manifest = json.loads((bm25_dir / "manifest.json").read_text())
            out["chunk_count"] = manifest.get("chunk_count")
            out["files_count"] = len(manifest.get("files") or [])
        except Exception:
            pass
    return out


def _bundle_resources_dir() -> Path | None:
    """Resolve the ``src/voitta/resources/`` directory in either layout.

    In a source checkout it's ``<repo>/src/voitta/resources/``. In a
    packaged .app the same files end up under
    ``<app>/Contents/Resources/app_packages/voitta/resources/`` —
    importing ``voitta`` and inspecting its ``__file__`` gets us there
    without hard-coding paths.
    """
    try:
        import voitta
    except ImportError:
        return None
    pkg_dir = Path(voitta.__file__).resolve().parent
    res = pkg_dir / "resources"
    return res if res.is_dir() else None


def _docs_source_dir() -> Path:
    """The read-only docs/ tree to feed the indexer.

    Source-checkout: ``<repo>/docs/``. Packaged: bundle resources copy
    staged by ``build_app.sh`` under
    ``src/voitta/resources/docs/``.
    """
    repo_candidate = PROJECT_ROOT / "docs"
    if repo_candidate.is_dir():
        return repo_candidate
    res = _bundle_resources_dir()
    if res is not None:
        bundled = res / "docs"
        if bundled.is_dir():
            return bundled
    # Last-resort fallback: walk up four levels from this file (the
    # source-checkout layout when this file is loaded as a script).
    return Path(__file__).resolve().parents[2] / "docs"


def _find_rag_script_dir() -> Path | None:
    """Locate the directory containing ``build_rag.py``.

    Source-checkout: ``<repo>/rag/``. Packaged: bundle resources copy
    at ``src/voitta/resources/rag_scripts/``.
    """
    repo_candidate = PROJECT_ROOT / "rag"
    if (repo_candidate / "build_rag.py").is_file():
        return repo_candidate
    res = _bundle_resources_dir()
    if res is not None:
        bundled = res / "rag_scripts"
        if (bundled / "build_rag.py").is_file():
            return bundled
    last_resort = Path(__file__).resolve().parents[2] / "rag"
    if (last_resort / "build_rag.py").is_file():
        return last_resort
    return None


def build(progress_cb: ProgressCb) -> bool:
    """Build the docs corpus into ``PROJECT_ROOT/rag/``.

    Drives ``scripts/build_rag.py``'s ``main()`` (the same script
    developers run locally) with overridden output paths so the index
    lands in the user-writable data dir instead of next to the script
    in the read-only bundle.

    progress_cb is invoked once at start, once at end. build_rag.py
    prints its own per-phase status to stdout — that lands in
    voitta.log via desktop_launcher's stdout/stderr redirect.

    Returns True on success. On failure, index dirs are wiped so a
    retry on next launch starts fresh.
    """
    docs_dir = _docs_source_dir()
    if not docs_dir.is_dir():
        progress_cb(0, 1, "RAG: docs/ not found, skipping", f"!!! {docs_dir}")
        return False

    chroma_dir, bm25_dir = output_dirs()

    rag_script_dir = _find_rag_script_dir()
    if rag_script_dir is None:
        progress_cb(0, 1, "RAG: build_rag.py not in bundle, skipping", "!!!")
        return False

    if str(rag_script_dir) not in sys.path:
        sys.path.insert(0, str(rag_script_dir))

    # Wipe partial output so the indexer starts clean.
    for d in (chroma_dir, bm25_dir):
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
    chroma_dir.parent.mkdir(parents=True, exist_ok=True)

    progress_cb(
        0, 1,
        "RAG: building (chromadb downloads ~80 MB on first run; may take ~30 s)…",
        ">>> rag build start",
    )

    try:
        import build_rag  # type: ignore[import-not-found]

        # Redirect the script's module-level path constants so the
        # index lands in the user data dir. DOCS_DIR for input, the
        # DOCS_CFG corpus config for output.
        build_rag.DOCS_DIR = docs_dir
        build_rag.RAG_DIR = chroma_dir.parent
        build_rag.DOCS_CFG.chroma_dir = chroma_dir
        build_rag.DOCS_CFG.bm25_dir = bm25_dir

        rc = build_rag.main(["--corpus", "docs"])
        if rc != 0:
            raise RuntimeError(f"build_rag.main returned exit code {rc}")
    except Exception as exc:
        tb = traceback.format_exc()
        progress_cb(
            0, 1,
            f"RAG: build failed — {type(exc).__name__} (rag_query will be unavailable)",
            tb[-2000:],
        )
        err_path = _error_log_path()
        try:
            err_path.parent.mkdir(parents=True, exist_ok=True)
            err_path.write_text(tb, encoding="utf-8")
        except OSError:
            pass
        for d in (chroma_dir, bm25_dir):
            if d.exists():
                shutil.rmtree(d, ignore_errors=True)
        return False

    progress_cb(1, 1, "RAG: build complete", "<<< rag build complete")
    # Clear any stale error from a previous failed run.
    _error_log_path().unlink(missing_ok=True)
    return True
