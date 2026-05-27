# Tool catalogue

Every tool the LLM can invoke is a `ToolSpec` registered with
`app.tools.registry.register(...)`. Tools split into **server**
(run in the BE process) and **browser** (round-trip to the FE via
`cl.CopilotFunction`).

This catalogue lists the tools shipped by the core. Plugin tools are
discovered separately — see [`05-plugins.md`](05-plugins.md).

## Server tools

| Tool | Source | What it does |
|---|---|---|
| `now` | [`tools/server/now.py`](../backend/app/tools/server/now.py) | Returns the server's current time as an ISO-8601 UTC string. Smoke-test tool. |
| `rag_query` | [`tools/server/rag.py`](../backend/app/tools/server/rag.py) | Hybrid dense+sparse search over the docs corpus. Returns ranked chunks. |
| `rag_get_chunk_range` | [`tools/server/rag.py`](../backend/app/tools/server/rag.py) | Stitch contiguous chunks from one file (overlap de-duplicated). Follow-up to `rag_query`. |
| `define_script` | [`tools/server/scripts/define_script.py`](../backend/app/tools/server/scripts/define_script.py) | Write a new user-authored Python script at `scripts/<name>/code.py`. Smoke-tests `build(ctx)` before persisting. |
| `edit_script` | [`tools/server/scripts/edit_script.py`](../backend/app/tools/server/scripts/edit_script.py) | Apply search-replace patches to an existing script. |
| `get_script` | [`tools/server/scripts/get_script.py`](../backend/app/tools/server/scripts/get_script.py) | Read a script back. |
| `list_scripts` | [`tools/server/scripts/list_scripts.py`](../backend/app/tools/server/scripts/list_scripts.py) | Enumerate every script in `scripts/`. |
| `delete_script` | [`tools/server/scripts/delete_script.py`](../backend/app/tools/server/scripts/delete_script.py) | Remove a script directory. |
| `run_script` | [`tools/server/scripts/run_script.py`](../backend/app/tools/server/scripts/run_script.py) | Execute a script's `build(ctx)`; mount the result as a pane (figure / plotly / react tree) or inline output. |
| `verify_script` | [`tools/server/scripts/verify_script.py`](../backend/app/tools/server/scripts/verify_script.py) | Last render's inventory — lightweight check that the FE actually mounted what the script returned. |
| `get_script_errors` | [`tools/server/scripts/get_script_errors.py`](../backend/app/tools/server/scripts/get_script_errors.py) | Render-event log; catches errors that happened after "looks ready." Consecutive duplicates deduped. |
| `screenshot_report` | [`tools/server/scripts/screenshot_report.py`](../backend/app/tools/server/scripts/screenshot_report.py) | Trigger the browser-side `screenshot_report` primitive; returns the PNG inline. |
| `list_data` | [`tools/server/scripts/list_data.py`](../backend/app/tools/server/scripts/list_data.py) | List all python_storage data snapshots. Each entry includes handle, name, kind, files, size, created_at, folder_name. |
| `preview_data` | [`tools/server/scripts/preview_data.py`](../backend/app/tools/server/scripts/preview_data.py) | Preview a file from a data snapshot. Images are returned inline as base64; text files as content. |
| `get_active_report` | [`tools/server/scripts/get_active_report.py`](../backend/app/tools/server/scripts/get_active_report.py) | Returns which script/tab is currently mounted in the FE report pane. Call this before editing a report you haven't just created. |
| `create_folder` | [`tools/server/scripts/create_folder.py`](../backend/app/tools/server/scripts/create_folder.py) | Create a workspace folder in the data domain, scripts domain, or both (default). No-op if already exists. |
| `move_to_folder` | [`tools/server/scripts/move_to_folder.py`](../backend/app/tools/server/scripts/move_to_folder.py) | Move a data snapshot (handle) or script (slug) into a folder. Pass `folder_name=null` to move back to root. |
| `delete_folder` | [`tools/server/scripts/delete_folder.py`](../backend/app/tools/server/scripts/delete_folder.py) | Delete a folder. **Contents are moved to root, not deleted.** |

See [`06-reports.md`](06-reports.md) for the scripts-and-reports
subsystem in depth. See [`07-workspace.md`](07-workspace.md) for the
workspace folder system, filesystem layout, and best practices.

## Browser tools

| Tool | Source | What it does |
|---|---|---|
| `get_page_title` | [`tools/browser/get_page_title.py`](../backend/app/tools/browser/get_page_title.py) (BE spec) + `primitives.ts:get_page_title` (FE impl) | Returns the host page's `<title>`. |
| `browser_eval` | [`tools/browser/browser_eval.py`](../backend/app/tools/browser/browser_eval.py) (BE spec) + `eval_js` FE primitive | Execute arbitrary JavaScript in the user's tab. Returns `{ok, result, logs, ms}`. Full DOM/fetch/localStorage access; top-level `await` supported. Prefer narrow plugin primitives where available. |

The browser-tool **spec** lives in the BE (so the LLM sees it in
`schemas_for_host`); the **implementation** lives in the FE
[`primitives.ts`](../frontend/src/lib/primitives.ts) (so it has DOM
access). Dispatch: BE calls `cl.CopilotFunction(name, args).acall()`,
which round-trips through the Chainlit socket; the FE's
[`CallFnRouter.tsx`](../frontend/src/lib/CallFnRouter.tsx) looks up the
primitive by name and ACKs.

## Browser-side primitives without a BE-side spec

Some primitives exist only to be triggered by other tools (e.g.
`screenshot_report` is called by the server-side `screenshot_report`
tool). They live in `primitives.ts` but have no `ToolSpec` — the LLM
doesn't see them as tools, they're internal mechanics.

## Adding a new tool

Server tool:

```python
# backend/app/tools/server/foo.py
from app.tools.registry import ToolSpec, register

async def _impl(args):
    return {"ok": True}

register(ToolSpec(
    name="foo",
    description="...",
    input_schema={"type": "object", "properties": {}, "additionalProperties": False},
    side="server",
    impl=_impl,
))
```

Then add the side-effect import to
[`tools/load.py`](../backend/app/tools/load.py):

```python
from app.tools.server import foo as _foo  # noqa: F401
```

Browser tool: same `ToolSpec` shape but `side="browser"` and no
`impl`. Add the FE counterpart to `primitives.ts` (or
`CallFnRouter.tsx` if it needs Recoil state).

For host-scoped tools, prefer registering them inside a **plugin** —
see [`05-plugins.md`](05-plugins.md). The plugin manifest's
`host_patterns` back-fills the tool's `host_pattern` automatically.
