# Voitta Plugin Integration Guide

This document describes how to add a new plugin to voitta-bookmarklet. Read it through once before writing any code — there are conventions about token handling, file layout, and registration that the auto-discovery loader depends on.

---

## What a plugin is

A plugin is a self-contained folder under `/plugins/<name>/` that adds host-specific capability to voitta-bookmarklet without modifying core. Plugins ride five auto-discovery surfaces:

| Surface | What gets discovered | Where in core |
|---|---|---|
| Backend tools | The plugin's Python package; every `registry.register(ToolSpec(...))` it makes at import time | [backend/app/tools/providers/__init__.py](backend/app/tools/providers/__init__.py) |
| Browser primitives | The plugin's `frontend/widget.ts`; every `registerPrimitive(...)` it makes | [frontend/src/widget.tsx](frontend/src/widget.tsx) |
| Documentation → RAG | Every `.md` under `plugins/<name>/docs/`; chunks land in the same RAG corpus as core docs | [rag/build_rag.py](rag/build_rag.py) |
| Seed scripts | `plugins/<name>/scripts/{compute,reports}/<slug>/`; staged into the .app and copied to the user's writable scripts dir on first launch | [build_app.sh](build_app.sh) + [backend/app/scripts_seed.py](backend/app/scripts_seed.py) |
| Branding / theme | `manifest.json` → `agent_name`; `plugins/<name>/theme.css` if present | [backend/app/main.py](backend/app/main.py) (`GET /api/plugin`, `GET /api/plugin/{name}/theme.css`) |

Core has zero hardcoded plugin names. The discovery loader walks `/plugins/*/manifest.json` and imports whatever each manifest points at. The canonical OSS reference plugin (`/plugins/google/`) is the only one tracked in the upstream repo; everything else is gitignored by `/plugins/*` plus a `!/plugins/google/` carve-out.

---

## Distribution model

Plugins live **outside** the upstream repo in normal use. The intended workflow:

1. Clone voitta-bookmarklet.
2. Drop your plugin into `/plugins/<name>/`.
3. Build / run as normal — `./run.sh` or `./build_app.sh`.
4. Pull from upstream voitta-bookmarklet whenever you want — your plugin folder is gitignored, so nothing collides.

If you want to track your plugin in source control, `git init` inside `plugins/<name>/` and treat it as its own repo. Don't add it as a submodule of voitta-bookmarklet — the upstream `.gitignore` excludes it, and submodules complicate the simple "drop a folder" deployment.

---

## Layout

```
plugins/<name>/
├── manifest.json                 ← required
├── README.md                     ← optional, recommended
├── theme.css                     ← optional; CSS custom-property overrides for branding
├── backend/
│   └── <python_module>/
│       ├── __init__.py           ← imports your tool modules
│       ├── _http.py              ← optional helper: shared HTTP plumbing
│       ├── tools.py              ← any number of *.py with ToolSpec registrations
│       └── ...
├── frontend/
│   └── widget.ts                 ← optional; only if you need browser primitives
├── docs/
│   └── *.md                      ← auto-indexed into RAG
└── scripts/                      ← optional curated compute / report scripts
    ├── compute/<slug>/{code.py, meta.json}
    └── reports/<slug>/{code.py, meta.json}
```

Pick a `<name>` that's filesystem-safe and a `<python_module>` that won't collide with PyPI. The convention is `<your_brand>_<thing>` for the python module — e.g. `voitta_acme` — to avoid stomping on third-party packages a future installer might pull in.

---

## Manifest schema

```json
{
  "name": "acme",
  "version": "0.1.0",
  "description": "Brief one-line summary surfaced in diagnostic UI.",
  "host_patterns": ["data.acme.example.com"],
  "python_module": "voitta_acme",
  "frontend_bundle": "frontend/widget.ts",
  "docs_dir": "docs",
  "python_dependencies": [
    {"import": "polars", "spec": "polars>=0.20"},
    {"import": "pyarrow", "spec": "pyarrow>=15.0"}
  ]
}
```

| Field | Required | Effect |
|---|---|---|
| `name` | yes | Folder name; must match the directory under `/plugins/`. |
| `version` | yes | Free-form. Surfaced in `/healthz`-style diagnostics. |
| `description` | yes | One-line summary shown in the menu bar Settings dialog. |
| `host_patterns` | recommended | Strict-suffix hostname matches. The first entry is auto-applied as `host_pattern` to every ToolSpec your plugin registers that doesn't declare its own. Multi-host plugins should declare per-tool `host_pattern` explicitly. |
| `agent_name` | optional | Display name for the assistant shown in the widget header (e.g. `"eBay Assistant"`, `"Voitta Enterprise"`). Defaults to title-case of `name`. Still used as the accessible name (aria-label) when `hide_brand` is true. |
| `hide_brand` | optional | `true` to suppress the brand mark + agent-name text in the header entirely. Useful when the host page already carries the Voitta wordmark and duplicating it would be noise. Provider chip and header buttons stay visible. |
| `default_layout` | optional | `"chat-left"` or `"chat-right"`. Sets the initial pane position for new users. The user's saved preference always wins if they've changed it. |
| `python_module` | yes if you have a backend | Importable name of your plugin's Python package, located at `plugins/<name>/backend/<python_module>/`. |
| `frontend_bundle` | optional | Informational; the discovery loader globs `frontend/widget.ts` directly. |
| `docs_dir` | optional | Informational; RAG always uses `docs/`. |
| `python_dependencies` | optional | List of `{import, spec}`. The first-launch installer extends its heavy-package list with these so users without your deps installed get them automatically. Dedupe across plugins is automatic. |

---

## Branding and theming

When the widget mounts on a host that matches your plugin's `host_patterns`, it calls `GET /api/plugin?host=<hostname>` and receives your plugin's `agent_name` and, if present, a URL to your `theme.css`. No extra wiring required — the bootstrap happens automatically at mount time.

### Agent name

Set `"agent_name"` in `manifest.json`. The widget header will show that name instead of "Voitta". If omitted, it defaults to title-case of your plugin's `name` field.

### Theme override

Create `plugins/<name>/theme.css`. Declare only the CSS custom properties you want to change — everything else inherits from the core default theme. The widget injects your file into the shadow DOM *before* the base theme, so your `:host` block wins in the cascade.

Minimal example:

```css
/* plugins/acme/theme.css */
:host {
  --voitta-header-bg:   #0057a8;
  --voitta-header-fg:   #ffffff;
  --voitta-accent:      #0057a8;
  --voitta-accent-hover:#004a90;
  --voitta-accent-fg:   #ffffff;
  --voitta-accent-tint: rgba(0, 87, 168, 0.08);
}
```

Available tokens are defined in [`frontend/src/theme.css`](frontend/src/theme.css) — that file is the authoritative list. If no `theme.css` is present in your plugin folder the default Voitta theme is used unmodified.

The `/api/plugin/{name}/theme.css` endpoint is unauthenticated so the widget can fetch it before the user has logged in.

---

## Tool registration

Every tool is a `ToolSpec`. The plugin's `__init__.py` imports each tool module; each module calls `registry.register(...)` at import time.

```python
# plugins/acme/backend/voitta_acme/__init__.py
from voitta_acme import (  # noqa: F401  — registration side-effects
    files,
    workitems,
    queries,
)
```

```python
# plugins/acme/backend/voitta_acme/files.py
from typing import Any
from app.tools.registry import ToolCtx, ToolSpec, registry


async def _list_files(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    folder_id = args["folder_id"]
    # ... do the work, return a JSON-serialisable dict ...
    return {"ok": True, "files": [...]}


registry.register(
    ToolSpec(
        name="acme_list_files",
        description=(
            "What this tool does, in 1–3 sentences. The LLM reads this. "
            "Be specific about input/output shapes and edge cases. "
            "Mention companion tools the LLM should chain to."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "folder_id": {"type": "string"},
            },
            "required": ["folder_id"],
            "additionalProperties": False,
        },
        handler=_list_files,
        side="server",
        # host_pattern omitted — manifest's host_patterns[0] auto-applies.
    )
)
```

### `side` field

| value | meaning |
|---|---|
| `"server"` | Pure backend handler. Best when your tool talks to your own backend, doesn't need cookies/tokens from the user's browser, or is purely local computation. |
| `"hybrid"` | Backend handler that calls one or more browser primitives via `app.tools.browser.call_browser(...)`. Best when you need the user's logged-in session at the upstream provider. |

### Gating tools

Tools become visible to the LLM only on hosts matching `host_pattern`. The auto-apply from `manifest.host_patterns[0]` handles the common single-host case. To override, set `host_pattern` explicitly on the ToolSpec.

For tools that depend on a runtime condition (a feature flag, an OAuth token being present), add a `visibility_check`:

```python
ToolSpec(
    ...,
    visibility_check=lambda: my_oauth.is_connected(),
)
```

The check fires every chat turn. Keep it cheap — no I/O, no API calls.

---

## Browser primitives (frontend integration)

A primitive is a JS function that runs in the user's browser tab. Plugins register them in `plugins/<name>/frontend/widget.ts`; voitta core globs every plugin's `widget.ts` via Vite and bundles them into `widget.js`.

```typescript
// plugins/acme/frontend/widget.ts
import { PrimitiveError, registerPrimitive } from "../../../frontend/src/lib/bridge";

interface FetchArgs {
  path?: string;
  method?: string;
  body?: unknown;
  max_bytes?: number;
}

const FETCH_CAP = 25 * 1024 * 1024;

function readToken(): string | null {
  // Read whatever auth artifact your platform stores in the browser.
  // localStorage, sessionStorage, a cookie via document.cookie, etc.
  return localStorage.getItem("acme_token");
}

registerPrimitive("acme_fetch", async (rawArgs, ctx) => {
  const args = rawArgs as FetchArgs;
  const path = String(args.path ?? "");
  if (!path.startsWith("/") || path.startsWith("//") || path.includes("://")) {
    throw new PrimitiveError("invalid_path", "path must be relative");
  }
  const token = readToken();
  if (!token) {
    throw new PrimitiveError("not_signed_in", "no token in localStorage");
  }
  const headers = new Headers(args.headers || {});
  headers.set("Authorization", `Bearer ${token}`);

  const res = await fetch(path, { method: args.method ?? "GET", headers, signal: ctx.signal });
  // ...decode body, enforce max_bytes, return {status, body, content_type, ...}
  return { status: res.status, body: await res.json() };
});
```

### Hybrid-tool pattern (backend calls primitive)

```python
# plugins/acme/backend/voitta_acme/queries.py
from app.tools.browser import call_browser
from app.tools.registry import ToolCtx, ToolSpec, registry


async def _list_things(args, ctx: ToolCtx):
    response = await call_browser(
        "acme_fetch",
        {"path": "/api/v1/things"},
        ctx,
    )
    if response.get("status") != 200:
        return {"ok": False, "error": "upstream_error", "detail": response}
    return {"ok": True, "things": response["body"].get("results", [])}


registry.register(ToolSpec(
    name="acme_list_things",
    description="List things from the platform.",
    input_schema={"type": "object", "properties": {}, "additionalProperties": False},
    handler=_list_things,
    side="hybrid",
))
```

The user's token never leaves the browser; the FastAPI process sees only response payloads.

---

## Privacy boundary

Two paths a plugin can use to talk to its upstream:

### Path A — Browser bridge (default)

The user's auth artifact (token, cookie, etc.) stays in the browser. The browser-side primitive makes the upstream request with credentials attached and returns the response payload to the FastAPI process. Use this for everything except large or binary payloads.

### Path B — Server-direct (rare)

For payloads that don't survive the bridge (Apache Arrow IPC bytes, multi-MB binaries) you can briefly forward the user's token to the FastAPI process for a single outgoing HTTPS request. Conventions:

* The token is held only in a local Python variable inside the handler, then falls out of scope at function return.
* It is **never** persisted to disk.
* It is **never** included in the tool result envelope returned to the LLM.
* The frontend primitive that exposes the token is named, e.g., `get_token`, with a clearly labelled comment block. It's the only place across the entire plugin where a token traverses the bridge backwards.

Concrete shape of the helper:

```python
# plugins/acme/backend/voitta_acme/_http.py
from app.tools.browser import call_browser
from app.tools.registry import ToolCtx
import httpx, time

async def _get_token_via_browser(ctx: ToolCtx) -> tuple[str, str | None]:
    info = await call_browser("get_token", {}, ctx, timeout_ms=10_000)
    if not isinstance(info, dict) or not info.get("token"):
        raise RuntimeError("no token in browser — sign in first")
    return info["token"], info.get("username")


async def acme_post_direct(path: str, body: dict, ctx: ToolCtx, *, accept="application/json"):
    token, username = await _get_token_via_browser(ctx)
    url = "https://api.acme.example.com" + path
    started = time.time()
    try:
        async with httpx.AsyncClient(timeout=600) as client:
            resp = await client.post(url, json=body, headers={
                "Authorization": f"Bearer {token}",
                "Accept": accept,
            })
    finally:
        token = ""  # noqa: F841 — explicit "out of scope NOW"
    return {
        "ok": resp.is_success,
        "status": resp.status_code,
        "body": resp.json() if accept == "application/json" else None,
        "body_bytes": resp.content if accept != "application/json" else None,
        "username": username,
        "elapsed_s": round(time.time() - started, 2),
    }
```

Use this only when a strictly browser-bridged request would fail (size limit, binary content type the bridge re-encodes lossily, etc.).

---

## Storing data: `python_storage`

Voitta has a stable on-disk snapshot store under `~/Library/Application Support/Voitta Bookmarklet/python_storage/`. Plugins can put data into it via the same API core uses:

```python
from app.services import python_storage

rec = python_storage.put(
    kind="curves",          # one of: drive_file, curves, query_result, ...
    response_body=parsed_json,
    meta={
        "origin": python_storage.make_origin(
            source="acme.platform",
            account=username,
            path="/api/v1/files/123",
            file_id="123",
            host="data.acme.example.com",
            url="https://data.acme.example.com/api/v1/files/123",
        ),
    },
)
# rec["handle"] is a short string like "py_a1b2c3d4"
# Pass it back to the LLM. Subsequent run_compute calls can reach
# the snapshot via ctx.snapshot(handle).
```

For binary or large data, write files directly into `python_storage.STORAGE_ROOT / f"snapshot_{handle}/"` after creating the dir. Always also write a `meta.json` so `list_python_storage` can describe the snapshot.

The user-facing rule is: **tool results never carry bulk data through the LLM context**. Big results return a `handle`; the LLM follows up with `run_compute` against the handle, which reads from disk.

---

## Documentation and RAG

Every `.md` under `plugins/<name>/docs/` is auto-indexed into the RAG corpus core ships with. The chunks' relative path is stamped as `<name>/docs/<file>.md`, so when the LLM does a `rag_query` and gets a hit, it can tell at a glance whether the source was core docs or which plugin contributed it.

A plugin's docs are how the LLM discovers what your tools do, what flows are idiomatic, and what edge cases to watch for. Treat them as part of the contract, not an afterthought.

Recommended structure:

* `docs/01-tools.md` — catalog with one section per tool group (or one per major workflow).
* `docs/02-data-model.md` — the upstream's domain model in voitta's vocabulary.
* `docs/03-flows.md` — 3–5 worked examples of common LLM-driven flows.

---

## Seed scripts (curated compute + reports)

If your plugin has canonical analyses ("parse this kind of file", "render that kind of plot"), ship them as **seed scripts**. They live at:

```
plugins/<name>/scripts/
├── compute/<slug>/
│   ├── code.py        # def run(ctx, args=None): ...
│   └── meta.json      # {"name": "<slug>", "kind": "compute", ...}
└── reports/<slug>/
    ├── code.py        # def build(ctx): ... (returns a Panel layout)
    └── meta.json      # {"name": "<slug>", "kind": "report", ...}
```

`build_app.sh` stages the entire `plugins/<name>/scripts/{compute,reports}/<slug>/` tree into the .app's seed_scripts/ resources dir. On first launch, `app.scripts_seed` copies them to the user's writable `<PROJECT_ROOT>/scripts/` directory. After that, the LLM can call them with `run_compute(name="<slug>", args={...})` or open them via `show_holoviz_report(name="<slug>")`.

---

## Stable interfaces a plugin can rely on

The following surfaces are part of voitta core's stable plugin contract:

| Surface | What |
|---|---|
| `app.tools.registry` | `ToolSpec`, `ToolCtx` (incl. `ToolCtx.extras: dict`), `registry.register()`, `registry.dispatch()` |
| `app.tools.browser` | `call_browser(name, args, ctx)`, `BrowserToolError` |
| `app.services.python_storage` | `put`, `put_file`, `get`, `list_all`, `delete`, `make_origin`, `STORAGE_ROOT` |
| `app.services.ensure_local` | `register(scheme, fn)`, `ensure_local(ref)`, `available_schemes()`, `EnsureLocalError` |
| `app.services.refs` | `parse(ref) → Ref`, `canonicalise(ref)`, `RefError` |
| `app.config.PROJECT_ROOT` | The user-writable data directory; safe to write into. |
| `frontend/src/lib/bridge.ts` | `registerPrimitive(name, fn)`, `PrimitiveError`, `getBackendOrigin()` |

Anything else is internal. If you find yourself reaching into `app.services.scripts._persist` or the like, write a wrapper inside your plugin and contribute the wrapper upstream as a stable API.

### Sentinel-key convention on content blocks

Cross-provider sentinels on `tool_result` / `tool_use` blocks use a leading underscore (`_name`, `_image`, `_raw`). The orchestrator and adapters may read them; they MUST NOT reach the wire. Today the Anthropic adapter strips them in `_strip_internal_keys`; OpenAI and Gemini access blocks by named key and are naturally immune. If you ship a plugin that hangs structured metadata off a `tool_result`, prefix the key with `_` so future adapters don't accidentally serialise it.

### Ref schemes and resolvers

A *ref* is a URI-shaped string (`scheme://k=v&k=v`) that names a durable upstream artefact. Reports and compute scripts store refs; `ctx.ensure_local(ref)` materialises a local snapshot on demand. The grammar lives in `app.services.refs`; schemes are open and plugin-owned.

To define a scheme in your plugin:

```python
# plugins/acme/backend/voitta_acme/resolver.py
from pathlib import Path
from app.services import ensure_local as _ensure_local, refs

async def resolve(ref: refs.Ref) -> Path:
    # fetch the upstream artefact, write a snapshot, stamp
    # meta.json::origin.ref = ref.canonical, return the path
    ...

_ensure_local.register("acme", resolve)
```

Then import it from your plugin's package `__init__.py` so the registration runs:

```python
from voitta_acme import (
    tools,
    resolver,  # registers the acme:// scheme via ensure_local.register
)
```

Canonical examples: [`plugins/google/backend/voitta_google/resolver.py`](plugins/google/backend/voitta_google/resolver.py) (`drive://`) and [`plugins/voitta-enterprise/backend/voitta_enterprise/resolver.py`](plugins/voitta-enterprise/backend/voitta_enterprise/resolver.py) (`vre://`).

### Shared keyspaces and conflict semantics

A plugin shares exactly four writable namespaces with core and other plugins. Knowing the collision behaviour saves debugging the day a second plugin shows up:

| Keyspace | Owner of `register` | On duplicate key |
|---|---|---|
| Tool names (`registry._tools[name]`) | `app.tools.registry.register` | WARN, keep prior, skip new. Plugin import continues. |
| Ref schemes (`ensure_local._RESOLVERS[scheme]`) | `app.services.ensure_local.register` | WARN, last-write-wins. Plugin load order is alphabetic by directory. |
| MCP connector ids | `app.services.mcp.register_connector` | See `app/services/mcp/registry.py` |
| Settings tree (`plugins.<name>.<field>`) | Frontend `SettingsView` | Namespaced by plugin directory name — no collision possible unless two plugin dirs share a name (filesystem prevents it). |

Plugin Python modules (`voitta_acme.*`) are namespaced by Python's import system; module-level state from one plugin is not visible to another.

---

## Local development

The fastest dev loop:

```bash
cd voitta-bookmarklet
./run.sh                 # runs the backend with --reload; auto-discovery picks up your plugin
# … in another terminal …
cd frontend && npm run dev   # rebuilds widget.js on file change
```

To verify your plugin loaded:

```bash
$ ./.venv/bin/python -c "
import sys; sys.path.insert(0, 'backend')
from app.tools.providers import LOADED_PLUGINS
for p in LOADED_PLUGINS: print(p['name'], p['manifest']['version'])
"
```

To list your plugin's registered tools:

```bash
$ ./.venv/bin/python -c "
import sys; sys.path.insert(0, 'backend')
from app.tools.providers import LOADED_PLUGINS  # noqa
from app.tools.registry import registry
for t in registry.all():
    if (getattr(t.handler, '__module__', '')).startswith('voitta_acme'):
        print(t.name, '→', t.host_pattern)
"
```

---

## Packaging into the .app

Plugins ship inside the briefcase-built .app automatically — `build_app.sh` copies `/plugins/*/` into the bundle's `Resources/` tree, and the auto-discovery loader resolves the bundled plugin trees the same way it resolves the source-checkout ones. No build-script edits needed.

When users install your plugin's .app for the first time, the launcher:

1. Runs the heavy-packages installer with both core packages AND every plugin's `python_dependencies` merged in.
2. Builds the RAG index across core docs + every plugin's docs.
3. Copies seed scripts from the bundle into the user's writable scripts/ dir.
4. Starts uvicorn.

---

## Authoring checklist

- [ ] `plugins/<name>/manifest.json` with `name`, `version`, `python_module`, `host_patterns`.
- [ ] `plugins/<name>/backend/<python_module>/__init__.py` that imports each tool module.
- [ ] At least one `ToolSpec` registered with the global registry.
- [ ] `plugins/<name>/frontend/widget.ts` if any tools need the user's browser session.
- [ ] `plugins/<name>/docs/01-tools.md` describing what's available — this is what the LLM sees via RAG.
- [ ] `python_dependencies` in the manifest if your backend imports anything not in core.
- [ ] Optional: `"agent_name"` in `manifest.json` to brand the widget header.
- [ ] Optional: `plugins/<name>/theme.css` to override CSS tokens (accent colour, header background, etc.).
- [ ] Optional: `plugins/<name>/scripts/compute/<slug>/` for canonical analyses your tools should chain into.
- [ ] Optional: `plugins/<name>/backend/<python_module>/resolver.py` + an import in `__init__.py` if your plugin owns a ref scheme (`acme://…`).
- [ ] Optional: `plugins/<name>/README.md` for human maintainers.
- [ ] Verify: `git ls-files | xargs grep -l <your-plugin-name>` returns empty (the OSS upstream has no record of your plugin).
- [ ] Verify: launching the .app shows your tools in `registry.all()` and your docs in RAG hits.
- [ ] Verify: `curl "http://localhost:12358/api/plugin?host=<your-host>"` returns your `agent_name`.

---

## Things to avoid

* **Editing core to add your plugin's name.** Discovery is fully data-driven; if you have to touch core to make your plugin work, file an upstream issue — the missing affordance is a bug.
* **Catching upstream auth tokens in tool result envelopes.** Even debug logging is a privacy leak. Token never travels to the LLM.
* **Returning bulk arrays through the chat context.** Use `python_storage` and return a handle.
* **Per-tool `host_pattern` repetition.** Set `host_patterns` once in the manifest; the loader applies it to every ToolSpec that doesn't override.
* **Importing from `app.tools.providers.<other_plugin>`.** Plugins are independent; if you need something another plugin offers, the owner of that plugin should expose it as a stable Python API or the capability belongs in core.
* **Claiming a ref scheme already owned by another plugin.** `ensure_local.register` is last-write-wins (logged at WARNING). If a scheme you want is taken, namespace yours (`acme-projects://`) instead of overriding.

---

## Where to look in the canonical example

`/plugins/google/` is the OSS reference plugin and demonstrates everything above. `/plugins/ebay/` demonstrates branding — it ships a `theme.css` and `"agent_name"` showing how to white-label the widget without touching core:

**google plugin:**

| File | Demonstrates |
|---|---|
| `manifest.json` | minimal manifest |
| `backend/voitta_google/__init__.py` | importing tool modules to trigger registration |
| `backend/voitta_google/tools.py` | OAuth-gated server tools, hybrid tools, `visibility_check` |
| `backend/voitta_google/page_scrape.py` | DOM-scrape primitive call as a fallback |
| `backend/voitta_google/context.py` | reading the active tab URL via the bridge |
| `frontend/widget.ts` | registering a download-modal primitive |
| `docs/01-drive-tools.md` | end-user-facing tool catalog |

**ebay plugin:**

| File | Demonstrates |
|---|---|
| `manifest.json` | `agent_name` field for widget header branding |
| `theme.css` | minimal CSS token overrides (eBay red accent + header) |
