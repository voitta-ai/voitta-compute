"""OpenAI adapter — uses the Responses API.

Mapping rules (in detail in docs/03-providers.md):

  request:
    system            -> instructions
    messages          -> input  (Anthropic blocks → Responses items)
    tools             -> [{type: "function", name, description, parameters}]

  response:
    output_text       -> {type: "text", text}
    function_call     -> {type: "tool_use", id, name, input}

We treat the OpenAI ``call_id`` as the Anthropic ``tool_use.id``, which is
the only stable cross-provider id we have.
"""

from __future__ import annotations

import json
from typing import Any

from openai import AsyncOpenAI

from app.services.llm.base import (
    Message,
    NormalisedRequest,
    NormalisedResponse,
    Provider,
    Usage,
)


class OpenAIProvider(Provider):
    id = "openai"

    def __init__(self, api_key: str) -> None:
        self._client = AsyncOpenAI(api_key=api_key)

    async def create_message(self, req: NormalisedRequest) -> NormalisedResponse:
        kwargs: dict[str, Any] = dict(
            model=req.model,
            instructions=req.system,
            max_output_tokens=req.max_tokens,
            input=_messages_to_responses_input(req.messages),
        )
        if req.tools:
            kwargs["tools"] = [
                {
                    "type": "function",
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.input_schema,
                }
                for t in req.tools
            ]
        result = await self._client.responses.create(**kwargs)
        return _from_openai(result)


def _messages_to_responses_input(messages: list[Message]) -> list[dict[str, Any]]:
    """Translate Anthropic-shaped messages to Responses API input items.

    Each user/assistant message produces one or more items: text → message
    items, tool_use → function_call items (assistant only), tool_result →
    function_call_output items (user only).
    """

    out: list[dict[str, Any]] = []
    for m in messages:
        for block in m.content:
            btype = block.get("type")
            if btype == "text":
                out.append(
                    {
                        "role": m.role,
                        "type": "message",
                        "content": [{"type": "input_text", "text": block.get("text", "")}]
                        if m.role == "user"
                        else [{"type": "output_text", "text": block.get("text", "")}],
                    }
                )
            elif btype == "tool_use" and m.role == "assistant":
                out.append(
                    {
                        "type": "function_call",
                        "call_id": block["id"],
                        "name": block["name"],
                        "arguments": json.dumps(block.get("input") or {}, default=str),
                    }
                )
            elif btype == "tool_result" and m.role == "user":
                content = block.get("content", "")
                if not isinstance(content, str):
                    content = json.dumps(content, default=str)
                out.append(
                    {
                        "type": "function_call_output",
                        "call_id": block["tool_use_id"],
                        "output": content,
                    }
                )
    return out


def _from_openai(result: Any) -> NormalisedResponse:
    content: list[dict[str, Any]] = []
    has_function_call = False

    for item in getattr(result, "output", []) or []:
        itype = getattr(item, "type", None)
        if itype == "message":
            for part in getattr(item, "content", []) or []:
                ptype = getattr(part, "type", None)
                if ptype in ("output_text", "text"):
                    text = getattr(part, "text", "") or ""
                    if text:
                        content.append({"type": "text", "text": text})
        elif itype == "function_call":
            has_function_call = True
            try:
                parsed_args = json.loads(getattr(item, "arguments", "") or "{}")
            except json.JSONDecodeError:
                parsed_args = {"_raw": getattr(item, "arguments", "")}
            content.append(
                {
                    "type": "tool_use",
                    "id": getattr(item, "call_id", "") or getattr(item, "id", ""),
                    "name": getattr(item, "name", "") or "",
                    "input": parsed_args,
                }
            )

    if has_function_call:
        stop_reason = "tool_use"
    else:
        incomplete = getattr(result, "incomplete_details", None)
        reason = getattr(incomplete, "reason", None) if incomplete else None
        stop_reason = "max_tokens" if reason == "max_output_tokens" else "end_turn"

    raw_usage = getattr(result, "usage", None)
    usage = Usage(
        input_tokens=getattr(raw_usage, "input_tokens", 0) or 0,
        output_tokens=getattr(raw_usage, "output_tokens", 0) or 0,
        cache_read_input_tokens=getattr(
            getattr(raw_usage, "input_tokens_details", None),
            "cached_tokens",
            0,
        ) or 0,
        cache_creation_input_tokens=0,
    )

    return NormalisedResponse(
        content=content,
        stop_reason=stop_reason,  # type: ignore[arg-type]
        usage=usage,
        model=getattr(result, "model", "") or "",
        raw=result,
    )
