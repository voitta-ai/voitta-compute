"""Gemini adapter — uses the `google-genai` SDK.

Mapping rules (in detail in docs/03-providers.md):

  request:
    system            -> system_instruction
    messages          -> contents   (Anthropic blocks → Parts)
    tools             -> [{function_declarations: [...]}]

  response:
    candidates[0].content.parts[*].text         -> {type: "text", text}
    candidates[0].content.parts[*].functionCall -> {type: "tool_use", id, name, input}

Tool ids are minted client-side because Gemini doesn't return one;
``id = "gemini_call_<seq>_<ts>"``. Subsequent ``tool_result`` blocks
must echo this id back.

Schema sanitisation: Gemini rejects several JSON-Schema keywords that
Anthropic accepts. We strip them — see ``_sanitize_schema``. The reference
is ``the original plugin/lib/providers.js::sanitizeGeminiSchema``.
"""

from __future__ import annotations

import json
import time
from typing import Any

from google import genai
from google.genai import types as genai_types

from app.services.llm.base import (
    Message,
    NormalisedRequest,
    NormalisedResponse,
    Provider,
    Usage,
)


class GeminiProvider(Provider):
    id = "gemini"

    def __init__(self, api_key: str) -> None:
        self._client = genai.Client(api_key=api_key)

    async def create_message(self, req: NormalisedRequest) -> NormalisedResponse:
        contents = _messages_to_contents(req.messages)
        config = genai_types.GenerateContentConfig(
            system_instruction=req.system,
            max_output_tokens=req.max_tokens,
        )
        if req.tools:
            config.tools = [
                genai_types.Tool(
                    function_declarations=[
                        genai_types.FunctionDeclaration(
                            name=t.name,
                            description=t.description,
                            parameters=_sanitize_schema(t.input_schema),
                        )
                        for t in req.tools
                    ]
                )
            ]
            config.tool_config = genai_types.ToolConfig(
                function_calling_config=genai_types.FunctionCallingConfig(mode="AUTO")
            )

        result = await self._client.aio.models.generate_content(
            model=req.model,
            contents=contents,
            config=config,
        )
        return _from_gemini(result, model=req.model)


# ---- request translation -----------------------------------------------


def _messages_to_contents(messages: list[Message]) -> list[dict[str, Any]]:
    """Anthropic blocks → Gemini ``contents`` list."""

    out: list[dict[str, Any]] = []
    for m in messages:
        if m.role == "user":
            parts: list[dict[str, Any]] = []
            for b in m.content:
                if b.get("type") == "text" and b.get("text"):
                    parts.append({"text": b["text"]})
                elif b.get("type") == "tool_result":
                    raw = b.get("content", "")
                    response: Any
                    if isinstance(raw, str):
                        try:
                            parsed = json.loads(raw)
                            response = (
                                parsed
                                if isinstance(parsed, dict) and not isinstance(parsed, list)
                                else {"result": parsed}
                            )
                        except json.JSONDecodeError:
                            response = {"result": raw}
                    else:
                        response = raw if isinstance(raw, dict) else {"result": raw}
                    if b.get("is_error"):
                        response = {**response, "error": True}
                    parts.append(
                        {
                            "function_response": {
                                "name": b.get("_name", "unknown_tool"),
                                "response": response,
                            }
                        }
                    )
            if parts:
                out.append({"role": "user", "parts": parts})
        else:  # assistant
            parts = []
            for b in m.content:
                if b.get("type") == "text" and b.get("text"):
                    parts.append({"text": b["text"]})
                elif b.get("type") == "tool_use":
                    parts.append(
                        {
                            "function_call": {
                                "name": b["name"],
                                "args": b.get("input") or {},
                            }
                        }
                    )
            if parts:
                out.append({"role": "model", "parts": parts})
    return out


def _sanitize_schema(input_schema: Any) -> Any:
    """Drop JSON-Schema keywords Gemini doesn't accept; mirror the
    plugin-light sanitiser so behaviour is identical."""

    if isinstance(input_schema, list):
        return input_schema
    if not isinstance(input_schema, dict):
        return input_schema

    out: dict[str, Any] = {}
    for k, v in input_schema.items():
        if k in ("type", "description", "nullable", "format"):
            out[k] = v
        elif k in ("enum", "required") and isinstance(v, list):
            out[k] = list(v)
        elif k == "items":
            out[k] = _sanitize_schema(v)
        elif k == "properties" and isinstance(v, dict):
            out[k] = {pn: _sanitize_schema(ps) for pn, ps in v.items()}

    if out.get("type") == "object" and not out.get("properties"):
        out.pop("properties", None)
    if out.get("type") == "array" and "items" not in out:
        out["items"] = {"type": "object"}
    if isinstance(out.get("required"), list):
        prop_keys = set((out.get("properties") or {}).keys())
        filtered = [k for k in out["required"] if isinstance(k, str) and k in prop_keys]
        if filtered:
            out["required"] = filtered
        else:
            out.pop("required", None)
    return out


# ---- response translation ----------------------------------------------


def _from_gemini(result: Any, *, model: str) -> NormalisedResponse:
    content: list[dict[str, Any]] = []
    has_tool = False
    seq = 0
    ts = int(time.time() * 1000)

    candidates = getattr(result, "candidates", None) or []
    cand = candidates[0] if candidates else None
    parts = []
    if cand is not None:
        cand_content = getattr(cand, "content", None)
        parts = getattr(cand_content, "parts", None) or []

    for p in parts:
        text = getattr(p, "text", None)
        function_call = getattr(p, "function_call", None)
        if text:
            content.append({"type": "text", "text": text})
        elif function_call is not None:
            has_tool = True
            content.append(
                {
                    "type": "tool_use",
                    "id": f"gemini_call_{ts}_{seq}",
                    "name": getattr(function_call, "name", "") or "",
                    "input": dict(getattr(function_call, "args", {}) or {}),
                }
            )
            seq += 1

    if has_tool:
        stop_reason = "tool_use"
    else:
        finish = getattr(cand, "finish_reason", None) if cand is not None else None
        finish_str = str(finish) if finish else ""
        stop_reason = "max_tokens" if "MAX_TOKENS" in finish_str else "end_turn"

    raw_usage = getattr(result, "usage_metadata", None)
    usage = Usage(
        input_tokens=getattr(raw_usage, "prompt_token_count", 0) or 0,
        output_tokens=getattr(raw_usage, "candidates_token_count", 0) or 0,
        cache_read_input_tokens=getattr(raw_usage, "cached_content_token_count", 0) or 0,
        cache_creation_input_tokens=0,
    )

    return NormalisedResponse(
        content=content,
        stop_reason=stop_reason,  # type: ignore[arg-type]
        usage=usage,
        model=model,
        raw=result,
    )
