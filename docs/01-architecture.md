# Architecture

## Request flow (happy path)

```
user types in widget
    │
    ▼
@chainlit/react-client emits "send-message" over Socket.IO
    │
    ▼
backend/app/chainlit_app.py:on_message
    │
    ▼
agent.py:run_turn        ◄── messages, system prompt, provider_id
    │
    ├── provider.stream_message(NormalisedRequest)
    │       │
    │       ├── yields BlockStart / BlockDelta / BlockStop / MessageStop
    │       │
    │       └── cl.Message.stream_token → widget renders incrementally
    │
    ├── if stop_reason == "tool_use":
    │       for each tool block:
    │         registry.dispatch(name, args)
    │           │
    │           ├── side="server"  → await spec.impl(args)
    │           └── side="browser" → cl.CopilotFunction(name, args).acall()
    │                                 ↳ FE routes to primitives.ts
    │
    └── append synthetic tool_result message; loop
```

The loop terminates when the model emits a non-`tool_use` stop reason
or hits `MAX_TOOL_ITERATIONS`.

## Key modules

| Module | Role |
|---|---|
| [`chainlit_app.py`](../backend/app/chainlit_app.py) | `@cl.on_chat_start`, `@cl.on_message`, `@cl.on_window_message`. Composes system prompt from applicable plugins. |
| [`agent.py`](../backend/app/agent.py) | The agent loop. Provider-agnostic — drives a `BaseProvider`. |
| [`services/llm/`](../backend/app/services/llm) | Provider abstraction. `NormalisedRequest`, `ToolSchema`, streaming events. One subpackage per provider. |
| [`tools/registry.py`](../backend/app/tools/registry.py) | `ToolSpec` + register / dispatch / `schemas_for_host`. |
| [`tools/load.py`](../backend/app/tools/load.py) | Side-effect imports — every tool module registers itself here. |
| [`plugins.py`](../backend/app/plugins.py) | Plugin discovery, manifest parsing, system-prompt loading, Python-module side-effect import. |
| [`reports/`](../backend/app/reports) | User-authored Python scripts → renderable panes. |

## System prompt composition

The base Voitta prompt lives in [`plugins/default/system.md`](../plugins/default/system.md).
At every turn, [`chainlit_app.py`](../backend/app/chainlit_app.py)
calls `plugins.for_host(host)` — which returns every plugin whose
`host_patterns` matches the page's host — and concatenates their
`system_prompt` contents. The default plugin's `host_patterns: ["*"]`
ensures the base prompt always applies; host-scoped plugins
(e.g. `ebay`) layer their own rules on top when active.

## Tool gating

`ToolSpec.host_pattern` gates a tool to specific hosts. `None` means
"always visible." `schemas_for_host(host)` filters the registry before
handing the tool list to the model — the model never sees tools that
don't apply on the current page.

Plugins back-fill `host_pattern` on their contributed ToolSpecs from
the manifest's `host_patterns`, so plugin authors specify host gating
ONCE in the manifest. Per-tool overrides win.

## Configuration

- **Process-wide constants** — [`config.py`](../backend/app/config.py):
  paths (`PROJECT_ROOT`, `DOCS_DIR`, `PLUGINS_DIR`, `RAG_DIR`), TLS
  cert paths, host/port, `MAX_TOKENS`, `MAX_TOOL_ITERATIONS`.

- **Per-user settings** — `~/.config/voitta-compute/settings.json`,
  read at chat start and on every turn (so settings changes take
  effect without restarting the session). Holds `provider`,
  `api_keys[provider]`, `models[provider]`.

## TLS

The widget runs on a foreign origin and embeds the BE via
`https://127.0.0.1:12358`. Modern browsers require HTTPS for the
Socket.IO upgrade — a self-signed cert lives at
[`certs/127.0.0.1+1.pem`](../certs). The user accepts the cert once
(visit the BE root in the browser), and the bookmarklet works
thereafter.
