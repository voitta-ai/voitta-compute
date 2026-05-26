"""LLM provider factory. Phase-1: Anthropic only."""

from __future__ import annotations

from typing import Literal

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

DEFAULT_MODELS: dict[ProviderId, str] = {
    "anthropic": "claude-sonnet-4-6",
    "openai": "gpt-4o",
    "gemini": "gemini-2.0-flash-exp",
}


def get_provider(provider_id: ProviderId, api_key: str | None) -> Provider:
    if not api_key:
        raise ProviderNotConfigured(
            provider_id,
            f"no API key for provider {provider_id!r}",
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
