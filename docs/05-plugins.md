# Plugins

Plugins extend Voitta with host-specific system prompts, backend tools, frontend components, and MCP servers.

## Manifest format

`plugins/<name>/manifest.json`:

```json
{
  "name": "my-plugin",
  "version": "1.0.0",
  "description": "What this plugin does",
  "host_patterns": ["*.mysite.com", "mysite.com"],
  "system_prompt": "system.md",
  "python_module": "voitta_myplugin",
  "frontend_bundle": "frontend/widget.ts",
  "agent_name": "MyPlugin Agent",
  "settings_schema": {
    "fields": [
      {"key": "plugins.my-plugin.api_key", "label": "API Key", "type": "password"}
    ]
  },
  "mcp_servers": [
    {
      "id": "my-mcp",
      "url_setting": "plugins.my-plugin.mcp.url",
      "api_key_setting": "plugins.my-plugin.mcp.api_key"
    }
  ]
}
```

All fields except `name` are optional.

## Host gating

`host_patterns` is a list of glob strings matched against the page hostname (lowercased, port stripped). Rules:
- `"*"` matches every page (used by the default plugin).
- `"ebay.com"` matches `ebay.com` and `www.ebay.com` (suffix match).
- `"*.mysite.com"` matches any subdomain via `fnmatch`.

When the bookmarklet is on a matching page:
- The plugin's `system_prompt` text is appended to the system prompt for the session.
- The plugin's tools (from `python_module`) become visible to the model.
- The plugin's MCP server tools become available.

## System prompt composition

At chat start, `app/plugins.py:for_host(host)` returns all matching plugins. Their `system_prompt` strings are concatenated (in discovery order) and passed to the agent loop. The default plugin (`host_patterns: ["*"]`) always contributes the base Voitta prompt.

## Python module

If `python_module` is set, the loader:
1. Adds `plugins/<name>/backend/` to `sys.path`.
2. `importlib.import_module(python_module)` — the module registers `ToolSpec` instances as import side effects.
3. Any `ToolSpec` that didn't declare `host_pattern` inherits the manifest's `host_patterns`.

Import failures are logged and skipped — one bad plugin doesn't abort startup.

## Frontend bundle

`frontend_bundle` is a path relative to the plugin dir (e.g. `frontend/widget.ts`). The Vite build glob picks it up automatically. No runtime loading — it's compiled into the IIFE at build time. The backend warns at startup if the declared file is missing.

## MCP servers

`mcp_servers[]` entries declare remote MCP servers the plugin wants to connect:

```json
{
  "id": "my-mcp",
  "url_setting": "plugins.my-plugin.mcp.url",
  "api_key_setting": "plugins.my-plugin.mcp.api_key"
}
```

`url_setting` and `api_key_setting` are dotted paths into `settings.json`. The loader registers a connector; `app/services/mcp/registry.py:refresh_all()` opens connections at FastAPI startup and synthesises `ToolSpec` instances for every tool the MCP server exposes.

## Settings panel

`settings_panel: "schema"` (default) — the FE renders a form from `settings_schema.fields`.  
`settings_panel: "custom"` — the FE looks for a `settings-panel.tsx` in the plugin's `frontend/` dir.

## Discovery

The loader does `PLUGINS_DIR.glob("**/manifest.json")` recursively, so nested trees like `plugins/google/drive/manifest.json` work. Plugins are sorted by path before loading so load order is deterministic.
