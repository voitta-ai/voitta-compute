# Plugins

Plugins are the extension model. A plugin can contribute:

- A **system-prompt addendum** scoped to specific hosts.
- **Server tools** (Python ToolSpecs registered as an import side effect).
- **Browser primitives** (FE-side, registered via `registerPrimitive`).
- **Docs** (auto-indexed by the RAG builder).

The plugin tree lives at the repo root, sibling to `backend/` and
`frontend/`:

```
plugins/<name>/
├── manifest.json            ← REQUIRED
├── prompt.md                ← optional — host-scoped system prompt
├── backend/
│   └── <python_module>/     ← optional Python package; registers ToolSpecs
│       └── __init__.py
├── frontend/
│   └── widget.ts            ← optional FE bundle; calls registerPrimitive
└── docs/                    ← optional markdown, auto-indexed by RAG
    └── *.md
```

Every piece is optional except `manifest.json`. A docs-only plugin
is valid; a tools-only plugin is valid; a prompt-only plugin is valid.

## Manifest

```json
{
  "name": "ebay",
  "host_patterns": ["ebay.com"],
  "python_module": "voitta_ebay",
  "frontend_bundle": "frontend/widget.ts",
  "system_prompt": "prompt.md"
}
```

| Field | Meaning |
|---|---|
| `name` | Display name. Should match the directory. |
| `host_patterns` | List of hostname suffixes. `"*"` matches every page. |
| `python_module` | Importable package under `<plugin>/backend/`. Optional. |
| `frontend_bundle` | Path (relative) to the FE entry; existence-validated, glob-loaded by Vite. Optional. |
| `system_prompt` | Path to a markdown file whose content is appended to the system prompt when the plugin applies. Optional. |
| `mcp_servers` | Reserved for a future MCP layer. Skipped today with a log line. |

## Discovery

[`backend/app/plugins.py`](../backend/app/plugins.py) runs at BE
startup:

1. Walks `plugins/*/manifest.json` (sorted).
2. For each plugin: parses manifest, reads `system_prompt` file,
   warns if `frontend_bundle` is declared but missing.
3. If `python_module` is set: inserts `<plugin>/backend/` on
   `sys.path` and `importlib.import_module(<python_module>)`. The
   module's `register(ToolSpec(...))` calls run as import side effects.
4. After import, back-fills `host_pattern` on plugin-contributed
   ToolSpecs that didn't declare their own — manifest is the source
   of truth for host gating, per-tool overrides win.
5. Failure of one plugin is logged + skipped; the rest load.

## System-prompt composition

[`chainlit_app.py`](../backend/app/chainlit_app.py) on every turn:

```python
host = cl.user_session.get("host")
parts = []
for plugin in for_host(host):
    if plugin.system_prompt:
        parts.append(plugin.system_prompt.rstrip())
system = "\n\n".join(parts)
```

The `default` plugin's `host_patterns: ["*"]` ensures the base
Voitta prompt always applies; host-scoped plugins layer their
addenda on top.

## Frontend loading

[`frontend/src/widget.tsx`](../frontend/src/widget.tsx) calls:

```ts
import.meta.glob("../../../plugins/*/frontend/widget.ts", { eager: true });
```

Vite walks the sibling `plugins/` tree at build time. Each plugin's
`widget.ts` registers primitives:

```ts
import { registerPrimitive } from "../../../frontend/src/lib/primitives";

registerPrimitive("ebay_inspect_page", () => {
  return { page_type: "search", item_count: 42 };
});
```

The relative path back to core: from `plugins/<name>/frontend/widget.ts`,
`../../../frontend/src/lib/primitives` resolves correctly. If the
plugin imports React/Preact, you'll need to add the appropriate aliases
to `frontend/vite.config.ts` (the plugin's own dir has no
`node_modules` above it).

## Docs

`plugins/<name>/docs/*.md` is auto-indexed by `scripts/build_rag.py`
into the `docs` corpus. Chunk file paths come out as
`<plugin>/docs/<file>.md` so search results show provenance.

## The default plugin

[`plugins/default/`](../plugins/default) ships the base Voitta system
prompt (`system.md`). It has `host_patterns: ["*"]` — always active.
This is also the one place the core touches: when packaging, ship
this plugin or the assistant has no instructions.

## Forward-compat fields

- `mcp_servers[]` — declared connectors to remote MCP servers.
  Reserved; skipped with a log line today. When the MCP layer arrives,
  each entry will register as an `MCPConnector` whose tools become
  available alongside local ones.
