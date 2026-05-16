# Plugin authoring

Voitta core has zero hardcoded providers. Everything host-specific lives under `/plugins/<name>/` — the canonical Google example included. A new plugin is **a folder you drop in**; no upstream edits, no merge conflicts on `git pull`.

## Layout

```
plugins/<name>/
├── manifest.json              # name, version, host_patterns, python_module
├── backend/
│   └── <python_module>/       # the package the manifest points at
│       ├── __init__.py        # registry.register(...) calls
│       └── *.py               # tool/context modules
├── frontend/
│   └── widget.ts              # registerPrimitive(...) side-effect imports
├── docs/
│   └── *.md                   # auto-indexed into the RAG corpus
└── seed_scripts/              # optional curated compute / report scripts (copied into python_storage/ at first launch)
    ├── compute/<slug>/{code.py, meta.json}
    └── reports/<slug>/{code.py, meta.json}
```

Voitta core auto-discovers all four pieces:

| Surface | Mechanism | Where the loop lives |
| --- | --- | --- |
| Backend tools | `pkgutil`-style scan: every `plugins/<name>/manifest.json` reads `python_module`, the loader adds `backend/` to `sys.path` and imports the module | [backend/app/tools/providers/__init__.py](../backend/app/tools/providers/__init__.py) |
| Browser primitives | `import.meta.glob("../../plugins/*/frontend/widget.ts", { eager: true })` in widget.tsx | [frontend/src/widget.tsx](../frontend/src/widget.tsx) |
| Docs → RAG | `_discover_plugin_docs()` walks every `plugins/<name>/docs/*.md` into the same corpus core docs land in | [rag/build_rag.py](../rag/build_rag.py) |
| Seed scripts | `build_app.sh` walks `plugins/<name>/seed_scripts/{compute,reports}/*` and stages alongside core seed scripts; at first launch they're copied into the user's `python_storage/{compute,reports}/` | [build_app.sh](../build_app.sh) |

## The manifest

Minimal manifest:

```json
{
  "name": "google",
  "version": "0.1.0",
  "description": "Google Drive — read-only OAuth, plus no-OAuth pickup fallback.",
  "host_patterns": ["drive.google.com"],
  "python_module": "voitta_google",
  "frontend_bundle": "frontend/widget.ts",
  "docs_dir": "docs",
  "system_prompt": "prompt.md"
}
```

* **`name`** — folder name. Must match the directory.
* **`version`** — semver, free-form for now.
* **`description`** — one line, used by `/healthz/plugins` diagnostics.
* **`host_patterns`** — list of strict-suffix hostname matches. Tools registered by this plugin should pass `host_pattern=` matching one of these so they're hidden when the user is on a different site.
* **`python_module`** — importable name of the plugin's backend Python package (sits at `plugins/<name>/backend/<python_module>/`). Pick something distinctive (`voitta_<name>`, `<name>_provider`) to avoid colliding with PyPI namespaces (e.g. don't call your package `google` — there's an existing `google` namespace package on PyPI used by Cloud SDKs).
* **`frontend_bundle`** — path within the plugin folder to the entry-point `.ts` file (currently informational; Vite globs `frontend/widget.ts` directly).
* **`docs_dir`** — informational; RAG indexer always uses `docs/`.
* **`system_prompt`** — optional plugin-dir-relative path to a Markdown file appended to the chat system prompt when the user is on a host matched by `host_patterns`. See "System prompt addendum" below.

## System prompt addendum

Some plugins need to tell the LLM "before doing X, do Y first" — `vre_search` the platform docs before guessing a slug, call the page-context tool before acting, treat a specific corpus as authoritative. That kind of rule belongs nowhere in the core prompt (it'd fire on every host) and nowhere in `docs/` (the LLM only sees `docs/` content if something pulls it into context).

Drop a Markdown file in the plugin (convention: `prompt.md`) and point the manifest at it:

```json
{
  "name": "my-portal",
  "host_patterns": ["portal.example.com"],
  "system_prompt": "prompt.md"
}
```

At backend startup the loader reads the file once and stashes the text on the plugin record. On every chat request, the route looks up the page's host, calls `plugins_for_host(host)` from `app.tools.providers`, and appends each matching plugin's `system_prompt` to the outgoing `system` string. Order: core prompt → plugin addenda (in load order) → `[Available files]` ambient block.

When to use it:

* Per-host LLM contracts: "always call `mything_get_page_context` first."
* Mandatory-lookup rules: "before authoring with this plugin's tools, `rag_query` the docs corpus."
* Tool-vocabulary pointers the LLM needs even when the relevant docs haven't been retrieved yet.

When NOT to use it:

* Behaviour the LLM should have on every host → that goes in `VOITTA_SYSTEM_PROMPT`.
* Long-form reference material (parameter tables, end-to-end examples) → that goes in `docs/`, indexed for RAG and retrieved on demand. Keep `prompt.md` short and rule-shaped.

Edits to `prompt.md` are picked up at backend restart — there's no hot-reload. A declared-but-missing file logs a warning and the plugin still loads (with no addendum), so a typo doesn't take down discovery.

The canonical example is [plugins/voitta-enterprise/prompt.md](../plugins/voitta-enterprise/prompt.md) — it carries the `vre_search` platform-docs pointer and a FreeCAD mandatory-lookup rule that fires only on `enterprise.voitta.ai`.

## Gating tools

The convention is to declare host gating per-`ToolSpec`:

```python
from app.tools.registry import ToolSpec, registry

registry.register(ToolSpec(
    name="my_tool",
    description="...",
    input_schema={...},
    handler=_handler,
    side="hybrid",
    host_pattern="data.example.com",   # tool only visible on this host
    visibility_check=lambda: settings.my_plugin_enabled,  # optional runtime gate
))
```

Tools without `host_pattern` are visible everywhere. The manifest's `host_patterns` field is a hint for diagnostic UI — it doesn't gate anything by itself; the gate lives on each ToolSpec.

## Hybrid tools (server calls browser primitive)

If your upstream API requires the user's browser session (cookies, OAuth tokens in localStorage, etc.), declare a frontend primitive that fetches the upstream URL with credentials, then call it from your Python handler:

```ts
// plugins/<name>/frontend/widget.ts
import { registerPrimitive } from "../../../frontend/src/lib/bridge";
registerPrimitive("my_fetch", async (args) => {
  const token = localStorage.getItem("session_token");
  const res = await fetch(args.path, {
    headers: { Authorization: `Bearer ${token}` },
  });
  return { status: res.status, body: await res.json() };
});
```

```python
# plugins/<name>/backend/<module>/tools.py
from app.tools.browser import call_browser
from app.tools.registry import ToolSpec, ToolCtx, registry

async def _list_things(args, ctx: ToolCtx):
    response = await call_browser(
        "my_fetch",
        {"path": "/api/v1/things"},
        ctx,
    )
    return {"things": response.get("body", {}).get("results", [])}

registry.register(ToolSpec(
    name="list_things",
    description="...",
    input_schema={"type": "object", "properties": {}, "additionalProperties": False},
    handler=_list_things,
    side="hybrid",
    host_pattern="data.example.com",
))
```

The token never traverses the Voitta backend; the browser injects it inside the user's tab.

## Distribution model

* **OSS-tracked plugin (Google).** Sits at `/plugins/google/` and is the only carve-out in `.gitignore`. Anyone cloning voitta-bookmarklet gets it.
* **Private plugins (everything else).** Drop into `/plugins/<your-name>/`. Gitignored by default — `git pull` from upstream voitta-bookmarklet never touches your plugin code, no merge conflicts. Track your plugin in its own private repo if you want.

To verify your plugin is loaded: launch the .app, open the menu-bar Settings dialog. If your manifest is well-formed and the Python module imports, you'll see it in the loaded-plugins list.

## Authoring checklist

- [ ] `plugins/<name>/manifest.json` with `name`, `python_module`, `host_patterns`
- [ ] `plugins/<name>/backend/<python_module>/__init__.py` that imports your `*.py` modules so their `registry.register(...)` calls run
- [ ] `plugins/<name>/frontend/widget.ts` (only if you need browser primitives)
- [ ] At least one `plugins/<name>/docs/*.md` describing your tools — the LLM relies on RAG hits to know what's available
- [ ] Optional: `plugins/<name>/prompt.md` + `"system_prompt": "prompt.md"` in the manifest, for host-scoped LLM rules ("before X, call Y first")
- [ ] Optional: `plugins/<name>/seed_scripts/compute/<slug>/{code.py, meta.json}` for curated analysis flows the LLM should call automatically when relevant data lands in `python_storage`

After the first run, your plugin's tools appear in the LLM's tool catalogue automatically (gated to your hosts), your docs flow into the RAG corpus, and your seed scripts get copied into the user's writable scripts directory.
