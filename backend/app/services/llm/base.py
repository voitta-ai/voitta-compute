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

import abc
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Literal, Protocol, TYPE_CHECKING

if TYPE_CHECKING:
    from app.services.llm.stream import StreamEvent


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

    def stream(self, req: NormalisedRequest) -> Any:
        """Returns an async context manager yielding StreamEvent.

        Usage:
            async with provider.stream(req) as events:
                async for ev in events:
                    ...
        """
        ...


class BaseProvider(abc.ABC):
    """Concrete base class adapters inherit from.

    Subclasses implement `stream()` (an async context manager yielding
    `StreamEvent`s). The default `create_message()` consumes the stream and
    assembles a `NormalisedResponse` — single source of truth.
    """

    id: str = ""

    @abc.abstractmethod
    def stream(self, req: NormalisedRequest) -> Any:
        ...

    async def create_message(self, req: NormalisedRequest) -> NormalisedResponse:
        from app.services.llm.stream import (
            BlockStart,
            BlockDelta,
            BlockStop,
            MessageStop,
            StreamError,
        )

        content: list[ContentBlock] = []
        blocks_by_index: dict[int, dict[str, Any]] = {}
        text_buf: dict[int, list[str]] = {}
        args_buf: dict[int, list[str]] = {}
        stop_reason: StopReason = "end_turn"
        usage = Usage()

        async with self.stream(req) as events:
            async for ev in events:
                if isinstance(ev, BlockStart):
                    if ev.kind == "text":
                        blocks_by_index[ev.block_index] = {"type": "text", "text": ""}
                        text_buf[ev.block_index] = []
                    else:
                        blocks_by_index[ev.block_index] = {
                            "type": "tool_use",
                            "id": ev.tool_id or "",
                            "name": ev.tool_name or "",
                            "input": {},
                        }
                        args_buf[ev.block_index] = []
                elif isinstance(ev, BlockDelta):
                    if ev.kind == "text":
                        text_buf.setdefault(ev.block_index, []).append(ev.text)
                    else:
                        args_buf.setdefault(ev.block_index, []).append(ev.text)
                elif isinstance(ev, BlockStop):
                    block = blocks_by_index.get(ev.block_index)
                    if block is None:
                        continue
                    if block["type"] == "text":
                        block["text"] = "".join(text_buf.get(ev.block_index, []))
                    else:
                        joined = "".join(args_buf.get(ev.block_index, []))
                        if joined:
                            try:
                                import json as _json
                                block["input"] = _json.loads(joined)
                            except Exception:
                                block["input"] = {"_raw": joined}
                elif isinstance(ev, MessageStop):
                    stop_reason = ev.stop_reason
                    usage = ev.usage
                elif isinstance(ev, StreamError):
                    raise RuntimeError(f"{ev.type}: {ev.message}")

        for idx in sorted(blocks_by_index.keys()):
            block = blocks_by_index[idx]
            if block["type"] == "text" and not block.get("text"):
                continue
            content.append(block)

        return NormalisedResponse(
            content=content,
            stop_reason=stop_reason,
            usage=usage,
        )
