# voitta-rag-enterprise MCP integration

The `voitta-enterprise` plugin bridges the bookmarklet chat backend to a
[voitta-rag-enterprise](../../../../voitta-rag-enterprise/) MCP server.
No Python code in this plugin — the bridge is declared entirely in
`manifest.json` and materialised by Voitta core's MCP infrastructure
(`backend/app/services/mcp/`).

## What it exposes

Every tool the remote MCP server lists becomes an LLM-visible tool with
the prefix `vre_`. As of voitta-rag-enterprise 0.x the list is:

| Remote tool             | Local name (with prefix)        |
|-------------------------|---------------------------------|
| `search`                | `vre_search`                    |
| `search_images`         | `vre_search_images`             |
| `get_file`              | `vre_get_file`                  |
| `get_chunk_range`       | `vre_get_chunk_range`           |
| `get_chunk_images`      | `vre_get_chunk_images`          |
| `get_image`             | `vre_get_image`                 |
| `list_indexed_folders`  | `vre_list_indexed_folders`      |
| `list_page_images`      | `vre_list_page_images`          |
| `get_page_image`        | `vre_get_page_image`            |
| `get_page_layout`       | `vre_get_page_layout`           |
| `get_workbook`          | `vre_get_workbook`              |
| `resolve_url`           | `vre_resolve_url`               |
| `list_assets`           | `vre_list_assets`               |
| `request_asset`         | `vre_request_asset`             |

Tools are gated to `enterprise.voitta.ai` via the plugin's
`host_patterns` — they're hidden from the LLM on every other host
(matching how every other plugin scopes its tools).

## Working with the file bytes — three-step flow

`vre_request_asset` returns a signed URL, not bytes. To get the
file into `python_storage` where `run_compute` can read it, chain
the call with `fetch_to_python_storage`:

```python
# 1. Find the file
hits = vre_search(query="McKinsey Q1")
file_id = hits[0]["file_id"]
file_path = hits[0]["file_path"]   # e.g. "mckinsey_quarterly_2025_q1.pdf"

# 2. Mint a signed URL for the source bytes
asset = vre_request_asset(file_id=file_id, asset_type="original")
url = asset["urls"]["file"]
# asset also has expires_at — fetch within the TTL (default 1 hour)

# 3. Pull the URL into python_storage
snap = fetch_to_python_storage(url=url, name=file_path)
handle = snap["handle"]            # "py_…"

# 4. Process the file in a compute script
run_compute(code=f"""
    rec = ctx.snapshot({handle!r})
    import pypdf
    reader = pypdf.PdfReader(rec['path'] + '/' + rec['meta']['stored_name'])
    ctx.text(reader.pages[0].extract_text()[:500])
""")
```

`fetch_to_python_storage` is the generic "URL → handle" tool — it
works for any HTTPS URL (signed asset URLs, public web downloads,
etc.), not just rag-enterprise responses. It adds no auth headers;
the URL is the credential. See [fetch_to_python_storage tool docs](
../../../docs/02-tool-catalogue.md#fetch_to_python_storage) for the
full signature.

## Asset types — what `request_asset` can produce

`vre_request_asset(file_id, asset_type, slug=None, params=None)`
always returns a dict with `asset_type` and exactly one of `inline`
(structured data the LLM consumes directly) or `urls` (variant
name → signed URL + `expires_at`). Call `vre_list_assets(file_id)`
first to discover what a specific file exposes.

| `asset_type`      | Applies to                    | Response shape                          | Notes                                                                                                      |
|-------------------|-------------------------------|-----------------------------------------|------------------------------------------------------------------------------------------------------------|
| `original`        | every indexed file            | `urls["file"]`                          | Source bytes of the indexed file. No `slug`, no `params`.                                                  |
| `cad_projection`  | `.step` / `.stp` / `.FCStd`   | `urls` keyed by `front`/`top`/`side`/`iso` | Four PNG views. Requires `slug` naming the component (use `vre_list_assets` for slugs). Optional `params={"size": 320}`. |
| `cad_mesh`        | `.step` / `.stp` / `.iges` / `.igs` / `.FCStd` | `urls["mesh"]`                          | Binary glTF (`.glb`, `model/gltf-binary`). Without `slug`: whole assembly. With a `cad_projection` slug: just that component. Optional `params={"linear_deflection": 0.5}` controls tessellation tolerance in mm (range 0.001–5.0; smaller = more triangles, larger file). The GLB scene contains one named node per component, so a viewer can list / hide / colour parts by `node.name`. |

### CAD 3D-viewer flow — `cad_mesh`

When the user wants to **see** a CAD file (vs read its parsed
component tree), chain `vre_request_asset` → `fetch_to_python_storage`
→ render in a report:

```python
# 1. Find a CAD file
hits = vre_search(query="bracket assembly")
file_id = hits[0]["file_id"]
file_path = hits[0]["file_path"]   # e.g. "bracket_v3.FCStd"

# 2. Request the whole-assembly GLB
asset = vre_request_asset(file_id=file_id, asset_type="cad_mesh")
url = asset["urls"]["mesh"]        # short-lived signed URL
# For just one component:
#   asset = vre_request_asset(file_id=file_id, asset_type="cad_mesh",
#                             slug="bracket-arm")
# For higher-fidelity tessellation:
#   asset = vre_request_asset(file_id=file_id, asset_type="cad_mesh",
#                             params={"linear_deflection": 0.05})

# 3. Pull the GLB into python_storage
snap = fetch_to_python_storage(url=url, name=file_path + ".glb")
handle = snap["handle"]

# 4. Embed in a report. `ctx.three_scene` takes a JavaScript string,
#    not a path — load the GLB via dynamic-import GLTFLoader, base64-
#    inline so the sandboxed iframe (null origin) can read it.
#    Full recipe + the wrong patterns that fail noisily:
#    docs/09-panel-threejs-reports.md "Loading a CAD model (GLB / GLTF)
#    into ctx.three_scene".
```

```python
# Minimum viable report — see the doc above for the full annotated version.
def build(ctx):
    rec = ctx.snapshot(handle)
    import base64, pathlib
    b64 = base64.b64encode(
        (pathlib.Path(rec["path"]) / rec["meta"]["stored_name"]).read_bytes()
    ).decode()
    return ctx.three_scene(f"""
        const {{ GLTFLoader }} = await import('three/addons/loaders/GLTFLoader.js');
        const bin = Uint8Array.from(atob("{b64}"), c => c.charCodeAt(0));
        const url = URL.createObjectURL(new Blob([bin], {{type: 'model/gltf-binary'}}));
        const gltf = await new GLTFLoader().loadAsync(url);
        scene.add(gltf.scene);
        camera.position.set(5, 3, 8);
    """, height=600)
```

`cad_mesh` preserves the part hierarchy: one named node per
component in the GLB scene. Downstream three.js code can iterate
`scene.children` to list parts, toggle visibility, or recolour by
`node.name`. The `slug` parameter (when used) matches the same
slugs `cad_projection` emits, so a single `vre_list_assets` call
gives the LLM the vocabulary for both view types.

## CAD retrieval — the component-slug model

Every indexed CAD file (`.fcstd`, `.step` / `.stp`, `.iges` / `.igs`) is a **tree of addressable components**, each named by a filesystem-shaped slug. Slugs are the single vocabulary the LLM uses to request anything CAD-related — projections, meshes, sub-assemblies, individual parts.

```
4-post-lift                                   ← whole assembly (top container)
4-post-lift/base-frame                        ← sub-assembly
4-post-lift/base-frame/longitudinal-rail-l    ← individual Part::Feature
4-post-lift/scissor-arms/upper-cross-arm-r
4-post-lift/hydraulic/cylinder-rod
```

A slug names either a **container** (App::Part — renders the whole subtree) or a **feature** (single Part::Feature). Both accept the same `vre_request_asset` calls. Slugs are stable across UI renames in FreeCAD because each spec also carries an `x-fcstd-internal-path` for identity round-trip.

### Discovery — how to find the slug you need

Three tools, used in that order most of the time:

| Tool | When to use it | What it returns |
|---|---|---|
| `vre_search(query, folder_ids=…, limit=20)` | "Find me the upper deck" — hybrid (dense + BM25) over the index. CAD files are indexed **one chunk per component**, so a search for `"longitudinal rail L"` lands on the exact feature's chunk. The chunk text contains the slug verbatim. | Hits with `chunk.text` — read the `Slug:` line out of it and pass it straight to `vre_request_asset`. |
| `vre_list_assets(file_id)` | "What's renderable in this file?" — full menu of derived views for one file. | `original` + `cad_mesh` (whole-file) + one `cad_projection` entry per addressable component, each with `slug`, `x-fcstd-path` (label segments), `x-fcstd-internal-path` (FreeCAD internal names), `x-fcstd-kind` (`container` / `feature` / `orphans` / `whole`), `x-fcstd-members` (the brp list the renderer composes). |
| `vre_list_indexed_folders(prefix=None)` | Browse storage as a filesystem. `prefix=None` → visible folders; `prefix="MyFolder/sub"` → subdirs + files. Hidden files (`.voitta.meta`, `.DS_Store`, …) are filtered. | Folder/file listing — use to discover `file_id`s when you don't have a search query. |

The component chunk text looks like this (verbatim — the LLM can copy-paste from it):

```
## Component: Longitudinal Rail L
Slug: `4-post-lift/base-frame/longitudinal-rail-l`
Internal path: `Part001 / Part005 / Body017`
Kind: Part::Feature (single addressable part)
Renderable via:
- vre_request_asset(file_id=<N>, asset_type="cad_projection", slug="4-post-lift/base-frame/longitudinal-rail-l")
- vre_request_asset(file_id=<N>, asset_type="cad_mesh",       slug="4-post-lift/base-frame/longitudinal-rail-l")
```

### Asset-type reference (CAD-specific)

| `asset_type`     | `slug` | `params` | Response | Notes |
|---|---|---|---|---|
| `cad_mesh`       | optional — omit for whole file, supply for one component | `linear_deflection` in mm (range 0.001–5.0; default 0.5; smaller = more triangles) | `urls["mesh"]` → GLB (`model/gltf-binary`) | Whole-file or single-component three.js scene with one named node per Part::Feature (FCStd) / per top-level XCAF label (STEP). |
| `cad_projection` | **required** — slug names the component | `size` in pixels (64–2048; default 512) | `urls = {front, top, side, iso}` → four PNGs | Use for thumbnail strips, side-by-side feature comparisons, and any "show me what this part looks like" prompt where 3D interactivity isn't needed. |
| `original`       | not used | not used | `urls["file"]` → raw source bytes | Stream into your own pipeline (custom OCP, openpyxl, etc.). No Voitta extraction. |

### Typical flows

**"Show me the upper deck of this lift."**
1. `vre_search("upper deck", folder_ids=[42])` → top hit's chunk contains slug `4-post-lift/upper-deck-assembly`.
2. `vre_request_asset(file_id, "cad_mesh", slug="4-post-lift/upper-deck-assembly")` → GLB URL.
3. `fetch_to_python_storage` → `ctx.three_scene` (see the recipe above and [docs/09-panel-threejs-reports.md](../../../docs/09-panel-threejs-reports.md) for the full pattern).

**"Render all four scissor arms side by side."**
1. `vre_search("scissor arm")` → chunks for the four feature slugs.
2. Four `vre_request_asset(file_id, "cad_projection", slug=…)` calls in parallel.
3. Display the 4-up grid of PNG URLs inline.

**"Ingest a STEP file into my own OCP pipeline."**
1. `vre_request_asset(file_id, "original")` → signed URL.
2. Stream into your `STEPControl_Reader` (no Voitta tessellation involved).

### Lifetime and errors

- Signed URLs **embed the credential** — no `Authorization` header needed on the fetch. They're short-lived (`expires_at` ≈ 1 h). Don't cache; re-mint via `vre_request_asset`.
- A URL is per-request: hitting the same URL after `expires_at` gets a 401, not a 410.
- If a file is deleted between mint and fetch → **410** on the asset URL.
- If a file was reindexed and the slug is stale → `KeyError: slug … not in asset menu` from `vre_request_asset` itself, before any URL is minted. Re-run `vre_list_assets` to get the fresh slugs.
- `vre_request_asset(asset_type="cad_mesh")` on a non-CAD extension → `ValueError: cad_mesh: unsupported extension …`. Check the file's extension via `vre_get_file` or `vre_list_assets` first.

### Refreshing after a source change

If a CAD file changed in the source folder, hit **Reindex** on that folder in the SPA (or `POST /api/folders/{id}/reindex`). The new component breakdown appears on the next `vre_list_assets` / `vre_search` call. No service restart; no cache to bust on the bookmarklet side.

## Setup

1. **Run a voitta-rag-enterprise server.** From that repo:

   ```bash
   # local single-user dev (no API key required)
   VOITTA_SINGLE_USER=true \
   VOITTA_ROOT_PATH=/tmp/vre-data \
   .venv/bin/uvicorn voitta_rag_enterprise.main:app \
     --host 0.0.0.0 --port 8000
   ```

   Or for the dedicated MCP-only server: `make mcp` (default port 8001).

2. **Mint an API key** (production only — single-user mode skips this).
   In the rag-enterprise SPA: Settings → API keys → New token.
   The token starts with `vk_`.

3. **Configure the bookmarklet plugin.** Open Voitta Settings, switch
   to the `voitta-enterprise` tab, fill in:

   - **Server URL** — `http://localhost:8000/mcp` for the unified app,
     `http://localhost:8001/mcp` for `make mcp`.
   - **API key** — paste the `vk_…` token. Leave blank in single-user
     mode.

   Hit **Save**. The schema renderer auto-saves each field on input.

4. **Click "Refresh tool list."** The plugin probes the MCP server,
   pulls `list_tools()`, and synthesises a `ToolSpec` per remote tool.
   The status badge flips to **● Connected — N tools**. On a fresh
   chat with the `enterprise.voitta.ai` plugin host (or in the
   developer's chat, when this plugin is active globally), the LLM
   sees all `vre_*` tools.

## Status badge legend

| Badge              | Meaning                                                       |
|--------------------|---------------------------------------------------------------|
| ● Connected — N tools | `list_tools()` succeeded; N tools are live.                   |
| ○ Not configured   | URL field is empty; no probe attempted.                       |
| ⚠ Unreachable      | Connection refused, TLS error, or timeout. Existing tools (if any) stay registered so a brief outage doesn't rip the chat's tool catalog out mid-conversation. |
| ✕ Unauthorized     | Server returned 401/403. Tools are dropped — they wouldn't work anyway. Mint a new token. |
| · Not probed yet   | Connector registered, hasn't been refreshed since startup.    |

## When to click Refresh

Tool lists are pulled **only** on backend startup and when the user
clicks the Refresh button — never per chat turn (the latency would add
up on every conversation). Refresh after:

* Upgrading the voitta-rag-enterprise server to a version with new
  tools.
* Changing the URL or API key (the credentials are picked up live on
  every tool *call*, so this is only for the tool *catalog*).

## Settings keys

The plugin's settings live under the `plugins.voitta-enterprise.*`
namespace in `~/.config/voitta-bookmarklet/settings.json`:

```json
{
  "plugins": {
    "voitta-enterprise": {
      "mcp": {
        "url": "http://localhost:8000/mcp",
        "api_key": "vk_..."
      }
    }
  }
}
```

Same dot-paths are referenced from `manifest.json` (`url_setting`,
`token_setting`) — wire format matches.
