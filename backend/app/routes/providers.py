"""Provider model listing — POST /api/providers/models.

Pulls the live model catalog from the chosen provider so the Settings UI
can populate its dropdown without a hardcoded list. Same key-handling
contract as /api/chat/stream: the API key arrives in the request body,
is consumed in-process, and never lands on disk or in logs.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field


router = APIRouter()
log = logging.getLogger(__name__)


class ListModelsRequest(BaseModel):
    provider: str = Field(..., description='one of: "anthropic" | "openai" | "gemini"')
    api_key: str


class ModelInfo(BaseModel):
    id: str
    display_name: str | None = None


class ListModelsResponse(BaseModel):
    provider: str
    models: list[ModelInfo]


@router.post("/api/providers/models")
async def list_provider_models(req: ListModelsRequest) -> ListModelsResponse:
    """Return chat-capable models for the chosen provider.

    Each provider's SDK is called from a worker thread (sync clients,
    simpler error surface than the async paginators). On any provider-
    side failure we surface a 502 — the frontend falls back to its
    hardcoded catalog so a bad key doesn't lock the dropdown.
    """

    if not req.api_key.strip():
        raise HTTPException(status_code=400, detail="api_key required")
    fetcher = _FETCHERS.get(req.provider)
    if fetcher is None:
        raise HTTPException(status_code=400, detail=f"unknown provider: {req.provider!r}")
    try:
        models = await asyncio.to_thread(fetcher, req.api_key)
    except Exception as exc:  # pragma: no cover — surface upstream failures
        log.warning("list_models failed for %s: %s: %s", req.provider, type(exc).__name__, exc)
        raise HTTPException(
            status_code=502,
            detail=f"{type(exc).__name__}: {exc}",
        )
    return ListModelsResponse(provider=req.provider, models=models)


def _fetch_anthropic(api_key: str) -> list[ModelInfo]:
    # Use the sync client; the auto-paginating iterator is the simplest
    # surface. ANTHROPIC_BASE_URL is hardcoded the same way the chat
    # provider hardcodes it (see services/llm/anthropic.py).
    from anthropic import Anthropic

    from app.services.llm.anthropic import ANTHROPIC_BASE_URL

    client = Anthropic(api_key=api_key, base_url=ANTHROPIC_BASE_URL)
    out: list[ModelInfo] = []
    for m in client.models.list(limit=1000):
        out.append(ModelInfo(id=m.id, display_name=getattr(m, "display_name", None)))
    return out


# OpenAI's /v1/models returns 100s of entries (embeddings, audio, image,
# fine-tuning, internal). We keep only chat-completion-capable models
# via a permissive prefix allow + a non-chat suffix block. New chat
# variants tend to keep the gpt-/o-style naming, so this rule ages OK;
# when it doesn't, it's a one-line tweak.
_OPENAI_CHAT_PREFIXES = ("gpt-", "o1", "o3", "o4", "o5")
_OPENAI_BLOCK_SUBSTRINGS = (
    "embedding",
    "whisper",
    "tts",
    "dall-e",
    "image",
    "moderation",
    "audio",
    "transcribe",
    "realtime",
    "search",
    "instruct",  # legacy completions API, not chat
)


def _fetch_openai(api_key: str) -> list[ModelInfo]:
    from openai import OpenAI

    client = OpenAI(api_key=api_key)
    out: list[ModelInfo] = []
    for m in client.models.list().data:
        mid = m.id
        if not any(mid.startswith(p) for p in _OPENAI_CHAT_PREFIXES):
            continue
        if any(s in mid for s in _OPENAI_BLOCK_SUBSTRINGS):
            continue
        out.append(ModelInfo(id=mid))
    # Newest first by `created` timestamp where available; fall back to
    # alphabetical so the order is deterministic in the dropdown.
    out.sort(key=lambda x: x.id, reverse=True)
    return out


def _fetch_gemini(api_key: str) -> list[ModelInfo]:
    # The google-genai SDK returns model names as "models/gemini-3-pro";
    # the chat path elsewhere expects the bare slug, so we strip the
    # prefix here. We also filter to models that actually support
    # generateContent (skip embeddings, AQA, and similar non-chat
    # endpoints).
    from google import genai

    client = genai.Client(api_key=api_key)
    out: list[ModelInfo] = []
    for m in client.models.list():
        name = getattr(m, "name", None) or ""
        if name.startswith("models/"):
            name = name[len("models/") :]
        if not name.startswith("gemini-"):
            continue
        actions = _supported_actions(m)
        if actions and "generateContent" not in actions:
            continue
        out.append(
            ModelInfo(
                id=name,
                display_name=getattr(m, "display_name", None),
            )
        )
    return out


def _supported_actions(m: Any) -> list[str]:
    """The genai SDK has shipped two field names for this; check both."""
    return (
        getattr(m, "supported_actions", None)
        or getattr(m, "supported_generation_methods", None)
        or []
    )


_FETCHERS = {
    "anthropic": _fetch_anthropic,
    "openai": _fetch_openai,
    "gemini": _fetch_gemini,
}
