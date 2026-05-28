# Tool Catalog

All tools are `ToolSpec` instances registered with `app.tools.registry`. They are loaded at startup by `app/tools/load.py`.

## Report / script management

| Tool | Description |
|---|---|
| `run_script` | Execute a saved script; dispatches HTML to the report pane or inline to chat |
| `define_script` | Create a new script (smoke-tests before saving) |
| `edit_script` | Replace the source of an existing script (smoke-tests before saving) |
| `get_script` | Return the source code of a saved script |
| `list_scripts` | List all saved scripts, optionally filtered by folder |
| `delete_script` | Delete a saved script |
| `get_script_errors` | Return the last runtime errors for a script |
| `verify_script` | Smoke-test a script source without saving or running |
| `get_active_report` | Return the slug of the report currently mounted in the pane |
| `screenshot_report` | Capture the currently-mounted report as a PNG via html-to-image |

## Workspace / data

| Tool | Description |
|---|---|
| `list_data` | List python_storage snapshots available in the workspace |
| `preview_data` | Return a preview of a snapshot (head rows, schema, sample) |

## Folder management

| Tool | Description |
|---|---|
| `create_folder` | Create a script folder |
| `delete_folder` | Delete a folder (and optionally its scripts) |
| `move_to_folder` | Move a script into a folder |

## Browser tools

| Tool | Description |
|---|---|
| `browser_eval` | Execute arbitrary JavaScript in the user's active tab; returns result + console logs |
| `get_page_title` | Return the `document.title` of the current tab |

## Utility

| Tool | Description |
|---|---|
| `get_current_time` | Return the current server time (ISO 8601) |

## RAG (when configured)

| Tool | Description |
|---|---|
| `rag_search` | Vector + BM25 search over the indexed docs corpus |
| `rag_index` | Trigger a re-index of the docs corpus |

## Host visibility

`registry.visible_for_host(host)` filters the tool list based on each `ToolSpec.host_pattern`. Tools with `global_tool=True` or `host_pattern=None` are always visible. Plugin-contributed tools inherit the plugin's `host_patterns` from the manifest unless they declare their own.
