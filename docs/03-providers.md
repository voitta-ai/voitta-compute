# LLM providers

Three providers, one shape. The chat orchestrator only knows the
normalised types from `app.services.llm.base`.

## Where keys live

API keys do **not** live in `.env`. They live in the browser's
`localStorage.voitta-bkmk-settings`, edited via the in-pane **Settings**
view (⚙ in the drawer header), and travel with each chat request:

```jsonc
POST /api/chat/stream
{
  "messages": [...],
  "session_id": "...",
  "provider": "anthropic",
  "api_key":  "sk-ant-...",
  "model":    "claude-sonnet-4-6",
  "max_tokens": 16384,
  "max_tool_iterations": 25
}
```

The backend forwards the key to the provider SDK for that request and
drops the request DTO at end-of-handler. No on-disk persistence; nothing
in any log line.

| Provider | Default model |
| -------- | ------------- |
| Anthropic | `claude-sonnet-4-6` |
| OpenAI | `gpt-5` (Responses API) |
| Gemini | `gemini-3.1-pro-preview` |

The frontend offers a richer per-provider model dropdown — see
`frontend/src/lib/settings.ts::MODELS_BY_PROVIDER`.

Missing-key behaviour: a chat request with an empty `api_key` (or no
`provider`) yields one SSE event:

```
event: error
data: {"message": "...", "type": "provider_not_configured", "provider": "anthropic"}
```

The chat pane catches this and switches to the Settings view so the user
can fix it. The other providers are unaffected — keys are independent.

## Normalised request

```python
class NormalisedRequest:
    model: str
    system: str
    max_tokens: int
    messages: list[Message]   # role + content blocks
    tools: list[ToolSchema]   # name, description, input_schema
```

## Normalised response

Anthropic-shaped, because it's the most expressive of the three:

```python
class NormalisedResponse:
    content: list[ContentBlock]      # text | tool_use blocks
    stop_reason: Literal["end_turn", "tool_use", "max_tokens", "stop_sequence"]
    usage: Usage                     # input/output tokens, cache hit/miss
```

All three providers translate in and out of this shape. The orchestrator's
loop is provider-agnostic.

## Adapter notes

### Anthropic

Pass-through. Native streaming is supported; we use the streaming API for
real-time delta emission.

### OpenAI

Use the **Responses API** (`client.responses.create(...)`), not the legacy
ChatCompletions API. Mapping rules:

- `system` → `instructions`.
- `messages` → `input`. Anthropic `tool_use` blocks become OpenAI
  `function_call` items; `tool_result` blocks become `function_call_output`
  items.
- `tools` → `[{"type": "function", "name", "description", "parameters": input_schema}]`.
- Response `output` items: `message` → text block(s); `function_call` →
  `tool_use` block (`id` taken from the `call_id`).
- `stop_reason`:
  - any `function_call` items present → `"tool_use"`,
  - `incomplete_details.reason == "max_output_tokens"` → `"max_tokens"`,
  - else → `"end_turn"`.

Streaming is non-streaming for now (one delta per assistant text block). A
future upgrade can switch to `client.responses.stream(...)`.

### Gemini

Use `google-genai` (not the deprecated `google-generativeai`). Mapping rules:

- `system` → `system_instruction`.
- `messages` → `contents`. Roles map: `user` → `user`, `assistant` →
  `model`. `tool_use` blocks become `function_call` parts; `tool_result`
  blocks become `function_response` parts.
- `tools` → `[{"function_declarations": [...]}]`. **The schema must be
  Gemini-flavoured**: drop unsupported keywords (`additionalProperties`,
  `$schema`, `default`, etc.); use `type` not `type[]`. The reference
  conversion lives in `the original plugin/lib/providers.js::sanitizeGeminiSchema`.
- Response `candidates[0].content.parts`: `text` → text block; `functionCall`
  → `tool_use` block (id minted client-side).
- `stop_reason`: any `function_call` → `"tool_use"`,
  `finishReason == "MAX_TOKENS"` → `"max_tokens"`, else `"end_turn"`.

## Caching

Anthropic only. Mark the system prompt + tool list as ephemeral cache, and
mark the second-to-last and last user turn boundaries to keep the running
KV-cache hot across iterations. See `the original plugin/lib/providers.js`
for the markup convention. OpenAI's prompt caching is automatic; Gemini's
is opt-in but limited.

## Adding a new provider

1. New file in `app/services/llm/<name>.py` exporting a class implementing
   `Provider`.
2. Register it in `app/services/llm/__init__.py::get_provider`.
3. Add a default-model entry in `app/config.py::DEFAULT_MODELS`.
4. Add the provider id + model list to
   `frontend/src/lib/settings.ts::MODELS_BY_PROVIDER`, the matching
   placeholder/destination text in
   `frontend/src/components/SettingsView.tsx`, and rebuild the widget.
5. Update this doc with the conversion rules.

The chat orchestrator does not change.
