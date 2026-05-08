# Tool catalogue

The LLM-facing surface, grouped by package. Every tool is a
`ToolSpec(name, description, input_schema, handler, side, host_pattern?,
visibility_check?)` registered as an import side-effect of its
module.

## Domain — provider-agnostic (`backend/app/tools/domain/`)

| Module | Tools | Notes |
| ------ | ----- | ----- |
| `rag.py` | `rag_query`, `rag_get_chunk_range` | Hybrid search over `docs/` and `libs-info/panel/` |
| `web.py` | `web_fetch` | Open-web GET with HTML/JSON/PDF/text extraction |
| `context.py` | `get_page_context` | Generic URL/title parser, no host gating |
| `screenshot.py` | `screenshot_report` | Rasterise the active HoloViz Panel iframe |
| `report_edits.py` | `get_report_edits` | Read live drag/resize state out of the editable iframe |
| `holoviz.py` | `show_holoviz_report` | Open a `define_report`-built layout in the iframe pane |
| `scripts.py` | `run_compute`, `define_report`, `list_*`, `get_*`, `delete_*`, `clear_script_output` | Persistent Python scripts (compute → inline output, report → iframe pane) |
| `python_storage.py` | `list_python_storage`, `get_python_storage_info`, `delete_python_storage`, `clear_python_storage` | Server-side snapshot store management |
| `buffers.py` | `list_buffers`, `get_buffer_summary`, `delete_buffer`, `delete_buffer_keys`, `clear_buffers`, `query_buffer_curves`, `plot_buffer_curves`, `plot_bars_from_buffer`, `plot`, `buffer_eval` | Browser-side data buffers + Chart.js plotting + sandboxed Worker eval |
| `buffers_arrow.py` | (Arrow buffer ops, optional) | Arrow IPC reads when a provider tool opts in |

## Providers (`backend/app/tools/providers/`)

A "provider" is a third-party host the bookmarklet adds smarts for —
Google Drive today, with more on the roadmap.

| Provider | Module | Tools | Gating |
| -------- | ------ | ----- | ------ |
| Google Drive | `providers/drive/context.py` | `drive_get_page_context` | `host_pattern="drive.google.com"` |
| Google Drive | `providers/drive/tools.py` | `drive_list_files`, `drive_search`, `drive_get_file`, `drive_download_to_python_storage`, `drive_export_to_python_storage` | `visibility_check=google_oauth.is_connected` |

### Adding a provider

1. Create `backend/app/tools/providers/<name>/`.
2. Add `context.py` registering a `<name>_get_page_context` tool with
   `host_pattern="<your-host.example>"`. Page-context tools are
   host-gated because their output describes the user's *current*
   page state (which is meaningless on other hosts).
3. Add `tools.py` for the action tools (list / search / download /
   etc.). Action tools should NOT be host-gated — the LLM may want
   to act on `<provider>` content from any host page. They SHOULD be
   visibility-gated by the provider's auth check (e.g.
   `visibility_check=<provider>_oauth.is_connected`) so they
   disappear from the LLM's tool list until the user has connected.
4. Wire the package into `app/tools/providers/__init__.py` so it
   imports on backend startup.
5. If the provider needs OAuth, mirror the
   `app/services/google_oauth.py` pattern and surface a connect
   button in the Settings panel.

## Tool naming conventions

- `list_X` — paginated list (filters become tool args).
- `get_X(id)` — single entity by id.
- `<provider>_<verb>` — provider-namespaced (e.g. `drive_list_files`).
- `<provider>_get_page_context` — host-gated context tool.
- `<provider>_download_to_python_storage(file_id, …)` — preferred
  download path; the LLM never sees raw bytes, only a snapshot
  handle.

## Result-size discipline

Three conventions, in order of preference:

1. **Domain-specific trimmer** — return only fields the model needs.
2. **Snapshot handle** — content writes go to `python_storage/`; the
   LLM gets a handle and inspects via `run_compute` /
   `ctx.snapshot(handle)`.
3. **Hard cap** — every tool result is JSON-encoded and clamped at
   the orchestrator level. Tools that may exceed the cap must return
   a handle instead.

## Buffers vs. python_storage

| | Browser buffer (`buffers.py`) | Python storage (`python_storage.py`) |
| --- | --- | --- |
| Where | In-memory in the page | On disk under `python_storage/` |
| Lifetime | Until tab reload / explicit free | Until explicit delete (survives restarts) |
| Best for | Tight iteration loops, Chart.js plots, Worker eval | Pandas / matplotlib / HoloViz, large files |
| Writer | `buffer_eval` return value (or any provider tool that opts in) | Provider download tools (`drive_download_to_python_storage`, …) |
