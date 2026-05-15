"""Live-API test config: keys come from env vars only.

Tests are skipped (not failed) when their key is absent so a developer
can run only the providers they have keys for.

No keys are read from files, written to disk, or logged. The test runner
never persists them.
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture
def anthropic_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        pytest.skip("ANTHROPIC_API_KEY not set")
    return key


@pytest.fixture
def openai_key() -> str:
    key = os.environ.get("OPENAI_API_KEY", "")
    if not key:
        pytest.skip("OPENAI_API_KEY not set")
    return key


@pytest.fixture
def gemini_key() -> str:
    key = os.environ.get("GEMINI_API_KEY", "")
    if not key:
        pytest.skip("GEMINI_API_KEY not set")
    return key
