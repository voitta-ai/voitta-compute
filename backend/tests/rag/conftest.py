"""Pytest fixtures for RAG chunker tests.

The chunker lives at ``scripts/build_rag.py`` — not a proper package,
just a script. We load it via importlib so the tests can exercise
its internals (chunkers, size invariants) without changing the
script's filesystem layout.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

# scripts/build_rag.py lives two levels up from this file:
#   backend/tests/rag/conftest.py
#   ^^^^^^^^^^^^^^^^^^^^^^^^^^^^
# repo_root = parents[3]
_REPO_ROOT = Path(__file__).resolve().parents[3]
_BUILD_RAG = _REPO_ROOT / "scripts" / "build_rag.py"


@pytest.fixture(scope="session")
def build_rag() -> ModuleType:
    """Loaded module handle for ``scripts/build_rag.py``."""
    assert _BUILD_RAG.is_file(), f"build_rag.py not found at {_BUILD_RAG}"
    spec = importlib.util.spec_from_file_location("build_rag_under_test", _BUILD_RAG)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    # Stash so importlib re-imports return the same instance.
    sys.modules["build_rag_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod
