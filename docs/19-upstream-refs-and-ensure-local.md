# Upstream refs and `ctx.ensure_local`

Reports (`define_report`, `define_flow_report`) and compute scripts (`run_compute`) persist their source code to disk and run again days, weeks, or months later. If a script bakes a `py_<handle>` snapshot id into its source, the moment the user deletes that snapshot (intentionally — via the file browser, or automatically by garbage collection), the script breaks. Worse: someone else opens the same conversation later, sees `py_a1b2c3` referenced, and has no idea what file it wanted.

The solution: **scripts reference upstream artefacts by their canonical identity, and the runtime materialises local copies on demand.**

## The ref grammar

A *ref* is a URI-shaped string:

    <scheme>://<key>=<value>(&<key>=<value>)*

The two schemes currently registered:

| Scheme | Source | Keys |
|---|---|---|
| `vre://` | voitta-rag-enterprise MCP server | `file_id`, `asset`, `slug?`, `export?` |
| `drive://` | Google Drive (read-only OAuth) | `file_id`, `export?` |

Each plugin's [`prompt.md`](../plugins/voitta-enterprise/prompt.md) spells out its scheme's keys and what they mean. Add a scheme by registering a resolver — see *Adding a scheme* below.

Canonical form: keys are sorted alphabetically when stored in `meta.json::origin.ref`, so `vre://asset=cad_mesh&file_id=42` and `vre://file_id=42&asset=cad_mesh` are the *same* ref and share the same cache entry.

## The runtime: `ctx.ensure_local(ref)`

Inside `build(ctx)` (reports) or `run(ctx, args)` (compute), call:

```python
csv_path = ctx.ensure_local("drive://file_id=1AbC...XYZ&export=text/csv")
glb_path = ctx.ensure_local("vre://file_id=42&asset=cad_mesh&slug=base-frame")
```

What happens, in order:

1. **Parse** the ref. Malformed refs raise `EnsureLocalError` synchronously — typos surface at the call site, not at render time.
2. **Cache lookup.** Walk `python_storage/cache/snapshot_*/meta.json` looking for an `origin.ref` that matches the canonical form. Hit → return its local path. Done.
3. **Miss → dispatch** to the scheme's registered resolver:
   - `vre://` runs the 2-step flow: call `request_asset` over MCP → fresh signed URL → stream into a new snapshot. The signed-URL TTL never leaks into the script.
   - `drive://` calls the plugin's download handler, which handles OAuth refresh transparently.
4. **Stamp** the canonical ref into the new snapshot's `meta.json::origin.ref` so step 2 wins next time.
5. **Return** the local path. For single-file snapshots the path is the file itself (`Path(p).read_bytes()` Just Works). For multi-variant fetches (e.g. `cad_projection` without `&export=…`), the path is the snapshot directory; the script enumerates its contents.

## The right vs wrong pattern, in code

```python
# ❌ Brittle: breaks if user deletes the snapshot; opaque to future readers.
def build(ctx):
    rec = ctx.snapshot("py_a1b2c3")
    df = pd.read_csv(rec["path"] + "/" + rec["meta"]["stored_name"])

# ✅ Self-healing: works after deletes, on fresh laptops, two months later.
def build(ctx):
    csv_path = ctx.ensure_local("drive://file_id=1AbCdEf...XYZ&export=text/csv")
    df = pd.read_csv(csv_path)
```

## Cache sharing semantics

Two reports referencing the same canonical ref share **one** cache entry. Disk and download cost are deduplicated. If a user deletes that shared snapshot from the file browser, every report referencing it triggers a refetch on the next run — that's the expected failure mode. No periodic invalidation, no background pollers; the runtime catches up at the next call.

## When *not* to use `ensure_local`

- **Genuinely local data** with no upstream — a CSV the user typed into a snapshot in-pane, anything created by `run_compute` as a derived artefact. Embed the handle; the rule is "if it originated upstream, reference upstream."
- **One-shot analysis** (not authoring a `define_*` script). Going through `drive_download_to_python_storage` and reading the resulting handle is fine — the handle's lifetime matches the conversation.

## Error handling in scripts

`EnsureLocalError` covers every recoverable failure (unknown scheme, network error, provider rejected the request, signed URL minting failed). Scripts that want to degrade gracefully wrap the call:

```python
try:
    glb_path = ctx.ensure_local(ref)
    glb_bytes = Path(glb_path).read_bytes()
except Exception as e:
    ctx.log(f"Asset unavailable: {e}")
    glb_bytes = None
```

The error message names the canonical ref and the underlying cause — enough context to diagnose without instrumenting the resolver.

## Adding a scheme

A scheme = one async resolver function plus a one-line registration. Pattern:

```python
# backend/app/services/resolvers/<scheme>.py
from app.services import ensure_local, refs

async def resolve(ref: refs.Ref) -> Path:
    # fetch, write snapshot under python_storage.STORAGE_ROOT,
    # stamp meta.json::origin.ref = ref.canonical, return path.
    ...

ensure_local.register("<scheme>", resolve)
```

Then `import` the module from `services/resolvers/__init__.py` (or, for a plugin-shipped scheme, from the plugin's backend `__init__.py` so it loads at plugin-discovery time).

## Files

- Parser + canonical form: [`backend/app/services/refs.py`](../backend/app/services/refs.py)
- Cache lookup + dispatch: [`backend/app/services/ensure_local.py`](../backend/app/services/ensure_local.py)
- Bundled resolvers: [`backend/app/services/resolvers/`](../backend/app/services/resolvers/)
- The system-prompt rule that pressures the LLM toward this pattern: see `VOITTA_SYSTEM_PROMPT` § "REPORTS — REFERENCE UPSTREAM ARTEFACTS".
