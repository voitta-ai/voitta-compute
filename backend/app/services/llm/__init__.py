"""LLM provider factory.

Three providers, one shape (``Provider`` protocol from ``base.py``). The
chat orchestrator imports ``get_provider(id, api_key)`` and otherwise
stays provider-agnostic.

API keys are **per-request** — supplied by the browser-side settings
view in the chat request body. The backend never persists them.
``get_provider`` raises ``ProviderNotConfigured`` when the key is empty,
and the chat route surfaces that to the user as a 400-ish error event.
"""

from __future__ import annotations

from typing import Literal

from app.config import DEFAULT_MODELS
from app.services.llm.base import (
    ContentBlock,
    Message,
    NormalisedRequest,
    NormalisedResponse,
    Provider,
    ProviderNotConfigured,
    TextBlock,
    ToolSchema,
    ToolUseBlock,
    Usage,
)


ProviderId = Literal["anthropic", "openai", "gemini"]


def get_provider(provider_id: ProviderId, api_key: str | None) -> Provider:
    if not api_key:
        raise ProviderNotConfigured(
            provider_id,
            f"no API key for provider {provider_id!r} — open the Settings panel and add one",
        )
    if provider_id == "anthropic":
        from app.services.llm.anthropic import AnthropicProvider
        return AnthropicProvider(api_key=api_key)
    if provider_id == "openai":
        from app.services.llm.openai import OpenAIProvider
        return OpenAIProvider(api_key=api_key)
    if provider_id == "gemini":
        from app.services.llm.gemini import GeminiProvider
        return GeminiProvider(api_key=api_key)
    raise ProviderNotConfigured(provider_id, f"unknown provider {provider_id!r}")


def default_model_for(provider_id: ProviderId) -> str:
    if provider_id in DEFAULT_MODELS:
        return DEFAULT_MODELS[provider_id]
    raise ProviderNotConfigured(provider_id, f"unknown provider {provider_id!r}")


__all__ = [
    "ContentBlock",
    "Message",
    "NormalisedRequest",
    "NormalisedResponse",
    "Provider",
    "ProviderId",
    "ProviderNotConfigured",
    "TextBlock",
    "ToolSchema",
    "ToolUseBlock",
    "Usage",
    "default_model_for",
    "get_provider",
]
