# Providers

LLM providers are pluggable. The agent loop in
[`agent.py`](../backend/app/agent.py) drives a `Provider` interface
defined in [`services/llm/base.py`](../backend/app/services/llm/base.py);
each backend lives in its own subpackage.

Three providers wired today:

| `ProviderId` | Module | Default model |
|---|---|---|
| `anthropic` | [`anthropic.py`](../backend/app/services/llm/anthropic.py) | `claude-sonnet-4-5-20250929` |
| `openai`    | [`openai.py`](../backend/app/services/llm/openai.py)       | `gpt-4o` |
| `gemini`    | [`gemini.py`](../backend/app/services/llm/gemini.py)       | `gemini-2.0-flash-exp` |

(See [`services/llm/__init__.py`](../backend/app/services/llm/__init__.py)
for the authoritative `DEFAULT_MODELS` map.)

## The canonical block shape

The Anthropic SDK's content-block shape is the **interchange format**.
A `Message` has `role` (`"user"` or `"assistant"`) and `content` —
either a string or a list of blocks:

```python
{"type": "text", "text": "..."}
{"type": "tool_use", "id": "...", "name": "...", "input": {...}}
{"type": "tool_result", "tool_use_id": "...", "content": "...", "is_error": false}
{"type": "image", "source": {...}}
```

OpenAI and Gemini adapters convert in/out of this shape so the agent
loop only ever sees Anthropic-flavoured blocks.

## Sentinel-key convention

Keys starting with `_` (`_name`, `_image`) are cross-provider internal
sentinels — the orchestrator and adapters may read them, but they
must NOT reach the wire. The Anthropic adapter strips them in
`_strip_internal_keys` before send; OpenAI and Gemini access blocks
by named key so unknown keys are naturally invisible.

## Streaming events

Providers yield a uniform stream of events from `stream_message`:

- `BlockStart(index, block_type, …)`
- `BlockDelta(index, delta)` — chunked text or input-json
- `BlockStop(index)`
- `MessageStop(stop_reason)` — `"end_turn"`, `"tool_use"`, `"max_tokens"`, etc.
- `StreamError(message)` — fatal; orchestrator surfaces it to the user.

The agent loop treats these uniformly. The provider absorbs vendor
quirks.

## Choosing a provider at runtime

Settings JSON at `~/.config/voitta-compute/settings.json`:

```json
{
  "provider": "anthropic",
  "api_keys": {
    "anthropic": "sk-ant-...",
    "openai": "sk-...",
    "gemini": "..."
  },
  "models": {
    "anthropic": "claude-opus-4-5"
  }
}
```

The model field is optional — `default_model_for(provider_id)` is
used when absent. Settings are re-read on every turn so changes via
the in-widget Settings panel take effect without a session restart.
