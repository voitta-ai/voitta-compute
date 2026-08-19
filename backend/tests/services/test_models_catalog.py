"""Tests for the dynamic model catalog + per-provider filters."""

from __future__ import annotations

import time

import pytest

from app.services import models_catalog as mc
from app.services.llm.openai import _is_openai_chat_model
from app.services.llm.gemini import _gemini_version_key


# ---- provider filters ---------------------------------------------------


def test_openai_chat_filter_keeps_chat_drops_noise():
    assert _is_openai_chat_model("gpt-4o")
    assert _is_openai_chat_model("gpt-4.1-mini")
    assert _is_openai_chat_model("o3")
    assert _is_openai_chat_model("chatgpt-4o-latest")
    # non-chat SKUs sharing the gpt-/o prefixes
    assert not _is_openai_chat_model("text-embedding-3-large")
    assert not _is_openai_chat_model("tts-1")
    assert not _is_openai_chat_model("whisper-1")
    assert not _is_openai_chat_model("dall-e-3")
    assert not _is_openai_chat_model("omni-moderation-latest")
    assert not _is_openai_chat_model("gpt-4o-realtime-preview")
    assert not _is_openai_chat_model("gpt-image-1")


def test_gemini_version_sort_newest_first():
    ids = ["gemini-1.5-pro", "gemini-2.5-flash", "gemini-2.0-flash", "weird-model"]
    ordered = sorted(ids, key=_gemini_version_key, reverse=True)
    assert ordered[0] == "gemini-2.5-flash"
    assert ordered[1] == "gemini-2.0-flash"
    assert ordered[2] == "gemini-1.5-pro"
    assert ordered[-1] == "weird-model"  # unparseable sinks to the bottom


# ---- catalog policy -----------------------------------------------------


@pytest.fixture
def fresh_catalog(tmp_path, monkeypatch):
    """Isolate the catalog's cache file + in-memory state per test."""
    monkeypatch.setattr(mc, "_CACHE_PATH", tmp_path / "models_cache.json")
    monkeypatch.setattr(mc, "_cache", None)
    monkeypatch.setattr(mc, "_snapshot", None)
    return mc


@pytest.mark.asyncio
async def test_no_credential_serves_snapshot(fresh_catalog, monkeypatch):
    monkeypatch.setattr(mc, "_credential_for", lambda p: None)
    res = await mc.list_models("anthropic")
    assert res["source"] == "snapshot"
    assert "claude-opus-4-8" in res["models"]  # snapshot includes the newest
    assert res["default"] in res["models"]


@pytest.mark.asyncio
async def test_live_fetch_then_cache_hit(fresh_catalog, monkeypatch):
    monkeypatch.setattr(mc, "_credential_for", lambda p: "sk-key")
    calls = {"n": 0}

    async def fake_fetch(provider, credential):
        calls["n"] += 1
        return ["claude-opus-4-8", "claude-sonnet-4-6"]

    monkeypatch.setattr(mc, "_fetch_live", fake_fetch)

    first = await mc.list_models("anthropic")
    assert first["source"] == "live"
    assert first["models"][0] == "claude-opus-4-8"
    assert calls["n"] == 1

    # Second call within TTL, same credential → cache hit, no refetch.
    second = await mc.list_models("anthropic")
    assert second["source"] == "cache"
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_stale_cache_refetches(fresh_catalog, monkeypatch):
    monkeypatch.setattr(mc, "_credential_for", lambda p: "sk-key")
    calls = {"n": 0}

    async def fake_fetch(provider, credential):
        calls["n"] += 1
        return ["gpt-4o"]

    monkeypatch.setattr(mc, "_fetch_live", fake_fetch)
    await mc.list_models("openai")
    assert calls["n"] == 1

    # Age the cache entry past the TTL.
    mc._cache["openai"]["fetched_at"] = time.time() - mc.CATALOG_TTL_S - 1
    res = await mc.list_models("openai")
    assert res["source"] == "live"
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_changed_credential_refetches(fresh_catalog, monkeypatch):
    cred = {"v": "key-a"}
    monkeypatch.setattr(mc, "_credential_for", lambda p: cred["v"])
    calls = {"n": 0}

    async def fake_fetch(provider, credential):
        calls["n"] += 1
        return ["gpt-4o"]

    monkeypatch.setattr(mc, "_fetch_live", fake_fetch)
    await mc.list_models("openai")
    cred["v"] = "key-b"  # rotate the key
    res = await mc.list_models("openai")
    assert res["source"] == "live"
    assert calls["n"] == 2  # fingerprint mismatch forced a refetch


@pytest.mark.asyncio
async def test_fetch_failure_falls_back_to_last_known(fresh_catalog, monkeypatch):
    monkeypatch.setattr(mc, "_credential_for", lambda p: "sk-key")
    state = {"fail": False}

    async def fake_fetch(provider, credential):
        if state["fail"]:
            raise RuntimeError("provider down")
        return ["claude-opus-4-8"]

    monkeypatch.setattr(mc, "_fetch_live", fake_fetch)
    await mc.list_models("anthropic")  # populate cache

    state["fail"] = True
    mc._cache["anthropic"]["fetched_at"] = time.time() - mc.CATALOG_TTL_S - 1  # force refetch
    res = await mc.list_models("anthropic")
    assert res["source"] == "cache"  # degraded to last-known, not an error
    assert res["models"] == ["claude-opus-4-8"]


@pytest.mark.asyncio
async def test_invalidate_then_no_credential_reverts_to_snapshot(fresh_catalog, monkeypatch):
    cred = {"v": "sk-key"}
    monkeypatch.setattr(mc, "_credential_for", lambda p: cred["v"])

    async def fake_fetch(provider, credential):
        return ["gpt-4o", "gpt-4o-mini"]

    monkeypatch.setattr(mc, "_fetch_live", fake_fetch)
    live = await mc.list_models("openai")
    assert live["source"] == "live"

    # Simulate key removal: invalidate + no credential.
    mc.invalidate("openai")
    cred["v"] = None
    res = await mc.list_models("openai")
    assert res["source"] == "snapshot"
