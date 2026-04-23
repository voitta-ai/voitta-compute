"""Provider protocol and the normalised request/response types.

The Anthropic block shape is the canonical interchange:

  message.content = [
      {"type": "text", "text": "..."},
      {"type": "tool_use", "id": "...", "name": "...", "input": {...}},
      {"type": "tool_result", "tool_use_id": "...", "content": "...", "is_error": false},
  ]

OpenAI and Gemini adapters convert in/out of this shape so the chat
orchestrator only ever sees one schema.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol


# ---- error types --------------------------------------------------------


class ProviderNotConfigured(RuntimeError):
    """Raised when a provider is requested but its API key isn't set."""

    def __init__(self, provider: str, message: str = "") -> None:
        super().__init__(message or f"provider {provider!r} not configured")
        self.provider = provider


# ---- block + message types ----------------------------------------------

# We model these as plain dicts (not Pydantic) for two reasons: (1) the
# Anthropic SDK already has a model class hierarchy we don't want to
# duplicate, and (2) tool_use.input and tool_result.content are open-shape
# JSON, which Pydantic would force into ad-hoc schemas. Plain dicts let us
# move blocks between providers without serialisation friction.


TextBlock = dict[str, Any]      # {"type": "text", "text": str}
ToolUseBlock = dict[str, Any]   # {"type": "tool_use", "id": str, "name": str, "input": dict}
ToolResultBlock = dict[str, Any]  # {"type": "tool_result", "tool_use_id": str, "content": str, "is_error": bool}
ContentBlock = TextBlock | ToolUseBlock | ToolResultBlock


@dataclass
class Message:
    role: Literal["user", "assistant"]
    content: list[ContentBlock]


@dataclass
class ToolSchema:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


StopReason = Literal["end_turn", "tool_use", "max_tokens", "stop_sequence"]


@dataclass
class NormalisedRequest:
    model: str
    system: str
    max_tokens: int
    messages: list[Message] = field(default_factory=list)
    tools: list[ToolSchema] = field(default_factory=list)


@dataclass
class NormalisedResponse:
    content: list[ContentBlock]
    stop_reason: StopReason
    usage: Usage
    model: str = ""
    raw: Any = None  # provider-native object, kept for debugging only


# ---- provider protocol --------------------------------------------------


class Provider(Protocol):
    id: str

    async def create_message(self, req: NormalisedRequest) -> NormalisedResponse:
        ...
