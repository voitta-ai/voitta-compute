# Working with `.a4db` files (ANSA / Animator crash database)

Companion to [09-panel-threejs-reports.md](09-panel-threejs-reports.md). That doc covers the iframe / postMessage / geometry-pipeline mechanics; this one is the playbook for **getting clean data out of an `.a4db` in the first place** — what the file actually is, the schema gotchas, and the culling passes that turn it into something a viewer can render without ghost stretching.

The reference implementation is in [backend/app/services/a4db.py](../backend/app/services/a4db.py); the run-compute adapter is [scripts/compute/a4db_parse/code.py](../scripts/compute/a4db_parse/code.py); the animated viewer is [scripts/reports/a4db_3d/code.py](../scripts/reports/a4db_3d/code.py).

## TL;DR

| Topic | Rule |
| ---- | ---- |
| File format | HDF5. Open with `h5py.File(path, "r")`. Don't trust the extension to mean "proprietary". |
| Compression | `.a4db.fz` is FEMZIP (SIDACT). Closed format, no FOSS reader — reject by magic bytes (`19 4d 95 04`). |
| Topology values | **0-based array indices**, not the sparse FEM IDs in `grids/ids/`. Don't go through the IDs. |
| Coordinates | Solver is **Z-up**; Three.js is Y-up. Swap at load time: `[x, z, -y]`. |
| Time units | Stored in **seconds**. Multiply by 1000 for ms. |
| Element erosion | Solvers delete failed elements but keep streaming displacement. **Cull before subsampling**, three passes (initial + edge-growth + per-quad mag). |
| Wire format | Interleaved `Float32Array` per frame `[x,y,z,damage]`, base64-inline. Cap **~15 MB** total payload. |

---

## 1. File format — HDF5 underneath

Despite the proprietary extension, `.a4db` is just HDF5. Verify before you start:

```python
with open(path, "rb") as f:
    if f.read(8) != b"\x89HDF\r\n\x1a\n":
        raise RuntimeError("not HDF5")
```

If the first 4 bytes are `19 4d 95 04` you have a **FEMZIP-A4DB** (`.a4db.fz`) — SIDACT's proprietary compression. There is no FOSS decompressor; reject with a clear error pointing the user at Animator's "Save As" or SIDACT's `femunzip`. Don't bundle binaries you can't redistribute.

`h5dump`, `h5py`, ParaView all open `.a4db` directly. The `info/creator` group's attributes identify the writing tool (e.g. *Altair Animator 2.6.2, type=Animator, version=2.1*).

---

## 2. Schema

```
model_0/
  geometry_0/
    nodes_0/
      grids/
        coordinates/x, y, z   ← float32, mm
        ids/                  ← sparse FEM node IDs (NOT array indices)
    elements_0/
      quads/
        ids/                  ← element IDs in topology row order
        topologies/           ← flat int32; reshape(-1, 4); values are
                                0-based indices into nodes_0/grids
      hexas/                  ← same shape, K=8
      beams/                  ← same shape, K=2
    properties_0/
      quads/<name>/<name>     ← int32 array; 0-based topology indices
                                belonging to this property (PID).
                                attrs: title, id (PID)
      hexas/<name>/<name>
      beams/<name>/<name>
  results_0/
    step_1, step_2, … step_N/
      attrs:
        time / Time             ← float32, seconds (×1000 for ms)
        title                   ← optional human label
      displacements/x, y, z   ← float32 per node, mm — DELTA from base
      function_*/...          ← per-element scalars (thickness, damage, …)
      hexa/failed, quad/failed ← element IDs eroded by this step
```

Sizes seen in practice on a BMW G60 front-crash dump: 1.87 M nodes, 0.86 M quads, 3.85 M hexas, 31 timesteps, 1.6 GB on disk.

---

## 3. The topology gotcha

`elements_0/quads/topologies` stores **0-based array indices** into `nodes_0/grids/coordinates/{x,y,z}`. It does **not** reference `nodes_0/grids/ids`. The `ids` array is *sparse FEM node IDs* used elsewhere by the solver — e.g. when an external `.key` file mentions node 100732, that's the FEM ID. Inside an `.a4db`, topologies have already been reindexed to positional form.

```python
# CORRECT
quads = topologies.reshape(-1, 4)
xyz   = np.stack([cx[quads], cy[quads], cz[quads]], axis=-1)  # (Q, 4, 3)

# WRONG — do not do this, you'll get garbage
nid_to_idx = {nid: i for i, nid in enumerate(node_ids)}
quads = np.vectorize(nid_to_idx.get)(topologies.reshape(-1, 4))
```

Heuristic: if `topo.max() < n_nodes`, you have indices. If `topo.max()` is much larger, you have node IDs and need to remap. In practice a4db topologies are always indices.

The same applies to `properties_0/<kind>/<name>/<name>` datasets — those are 0-based **topology row indices** (a non-overlapping partition of the element array), not element IDs.

---

## 4. Coordinate system

Solvers (LS-DYNA, RADIOSS, …) use **Z-up**: X = length, Y = width, Z = vertical. Three.js uses **Y-up**, and `OrbitControls` ignores any attempt to override `camera.up`.

Swap at load time, once, on the numpy side — never in a shader, never in `camera.up = (0,0,1)`:

```python
coords_three = np.stack([x, z, -y], axis=1)
# Apply the SAME transform to displacements before adding to base coords:
disp_three   = np.stack([dx, dz, -dy], axis=1)
position_t   = coords_three + disp_three
```

After the swap, `THREE.AxesHelper`, camera, orbit, and every diagnostic helper just work. See [09-panel-threejs-reports.md §4](09-panel-threejs-reports.md) for the wider context.

---

## 5. Reading frames

```python
import h5py, numpy as np

with h5py.File(path, "r") as f:
    rg = f["model_0/results_0"]
    step_keys = sorted(
        (k for k in rg.keys() if k.startswith("step_")),
        key=lambda k: int(k.split("_")[1]),
    )
    for sk in step_keys:
        attrs = rg[sk].attrs
        t_s   = float(attrs.get("time", attrs.get("Time", 0.0)))
        t_ms  = t_s * 1000.0
        dx = rg[f"{sk}/displacements/x"][:]
        dy = rg[f"{sk}/displacements/y"][:]
        dz = rg[f"{sk}/displacements/z"][:]
        # deformed_position = base + displacement
```

`step_1` typically has `time = 0` and zero displacement (initial / undeformed state). Treat that as the static base; later steps are deltas.

When you only need a vertex subset (after culling + subsampling), index after the bulk read — it's faster than h5py fancy indexing on multi-million-row datasets:

```python
dx = rg[f"{sk}/displacements/x"][:][active_sorted]   # bulk read, then index
```

---

## 6. Filtering rogue elements — the **critical** stage

Crash solvers erode (delete) failed elements mid-run, but the underlying **nodes keep streaming displacement** for the rest of the simulation. The result: in any naive visualisation you get giant stretching triangles flying across the scene at frame 30 — the silhouette looks like a hairball, the camera frustum needs `far=100` instead of `far=10`, and the screenshot is unusable.

Three culling passes are needed. **All three** masks must be computed on the full mesh before subsampling. Subsampling first hides ghost elements behind the random sampler.

### Pass 1 — initial edge length (catches degenerate input mesh)

```python
init_edges = np.linalg.norm(coords[quads[:, 1]] - coords[quads[:, 0]], axis=1)
# repeat for the other 3 edges; take the per-quad max
init_max = ...
threshold = np.percentile(init_max, 99.9) * 1.5
bad_init  = init_max > threshold
```

Catches pre-existing degenerate quads in the input mesh — sliver elements introduced by mesher quirks or from CAD imports.

### Pass 2 — edge growth across all frames *(the one that actually matters)*

```python
# per-edge max length across every timestep
max_deformed = np.max([
    edge_lengths_at_step(t)        # (Q, 4) per quad's edge lengths
    for t in range(num_steps)
], axis=0)                          # (Q, 4)

edge_growth = max_deformed / (init_edges_per_quad + 1e-6)   # (Q, 4)
bad_growth  = (edge_growth > 5.0).any(axis=1)               # any edge stretched 5×
```

This is the only pass that catches eroded elements reliably. Their nodes drift in different directions — neither node individually has a huge displacement magnitude (so per-node thresholds miss them), but the **edge between them** stretches from ~5 mm to thousands of mm. If you skip this pass, you ship a viewer with hairball frames.

### Pass 3 — per-quad displacement magnitude (catches ghost nodes)

```python
all_mag       = np.linalg.norm(disp_all_steps, axis=-1)   # (T, N)
max_per_node  = all_mag.max(axis=0)                       # (N,)
quad_max_mag  = np.stack(
    [max_per_node[quads[:, i]] for i in range(4)]
).max(axis=0)                                             # (Q,)
bad_mag = quad_max_mag > np.percentile(quad_max_mag, 99) * 2.0
```

Catches the rare case where a single ghost node integrates to extreme displacement values — its parent quad gets dropped even if its edges happened not to stretch beyond the Pass 2 threshold.

### Compose then cull

```python
bad        = bad_init | bad_growth | bad_mag
clean_mask = ~bad
clean_quads = quads[clean_mask]
```

Typical reduction on a crash dump: **2–8 %** of quads dropped. That's the difference between a usable viewer and a hairball.

---

## 7. Subsampling for visualisation

After culling — never before — randomly sample to a target quad count, then re-index the node array to only the vertices actually used:

```python
TARGET = 200_000
keep_idx  = np.random.default_rng(42).choice(
    len(clean_quads), TARGET, replace=False,
)
sub_quads  = clean_quads[keep_idx]
used_nodes = np.unique(sub_quads.ravel())

# Build the dense remap original-idx → packed-idx
node_map  = np.full(len(coords), -1, dtype=np.int32)
node_map[used_nodes] = np.arange(len(used_nodes), dtype=np.int32)
sub_quads_remapped   = node_map[sub_quads]                    # (TARGET, 4)

# Quads → triangles for THREE.Mesh / TrianglesGeometry:
#   tri_a = (0, 1, 2)
#   tri_b = (0, 2, 3)
tris = np.concatenate([
    sub_quads_remapped[:, [0, 1, 2]],
    sub_quads_remapped[:, [0, 2, 3]],
], axis=0)
```

Using a dense `node_map` here means downstream per-frame position arrays are sized `(T, len(used_nodes), 3)` — a 5–10× memory reduction over keeping the full node array.

---

## 8. Binary transfer to Three.js

Pack positions + per-vertex damage into a **single interleaved `Float32Array`** per frame `[x, y, z, damage]`, base64-encode once on the Python side, and inline in the iframe `srcdoc`:

```python
import base64
# positions: (T, M, 3)  damage: (T, M)
interleaved = np.concatenate([positions, damage[..., None]], axis=-1)  # (T, M, 4)
b64 = base64.b64encode(interleaved.astype("<f4").tobytes()).decode("ascii")
viewer_html = f'<script>const FRAMES_B64 = "{b64}"; const M = {M}; ...'
```

Decode in the iframe:

```js
const buf = Uint8Array.from(atob(FRAMES_B64), c => c.charCodeAt(0)).buffer;
const f32 = new Float32Array(buf);
// frame t, vertex i:
//   x = f32[t*M*4 + i*4 + 0]
//   y = f32[t*M*4 + i*4 + 1]
//   z = f32[t*M*4 + i*4 + 2]
//   d = f32[t*M*4 + i*4 + 3]
```

### Size budget — the real ceiling

| Format | Working ceiling |
| ---- | ---- |
| Base64 binary inlined in `srcdoc` | **~10–15 MB encoded** before the browser stalls on decode |
| Raw JSON (`JSON.parse`) | ~25 MB hard wall |
| `fetch()` from `srcdoc` | unreliable (origin = `null`); don't use |

Plan for **15 MB encoded** as the practical max. With `M` active vertices, `T` frames, 16 bytes per vertex (interleaved float32 ×4), base64 expands by 4/3:

```
encoded_MB ≈ T · M · 16 / (1024 · 1024) · 4 / 3
```

So **15 MB → ~750 K vertex-frames**. For a 31-frame run that's 24 K vertices per frame — too tight. Reach for the levers in this order:

1. **Reduce `M`** — tighten per-property edge caps. Halving M doubles your frame budget.
2. **Quantize positions to int16** (range ±9 unit-cube, scale `32767/9`). Halves the wire payload, decode is one division per channel in JS.
3. **Drop frames** — uniform stride. Last resort; jumpy playback.

Combining (1) + (2) gets a 31-frame BMW crash dump down to ~13 MB encoded with ~150 K vertices.

---

## 9. Animation loop

Use a **shared position attribute** across every group + interior `LineSegments`. Per-frame work then becomes a single typed-array copy + one `needsUpdate = true`:

```js
const sharedPosArr = new Float32Array(M * 3);
const sharedPos    = new THREE.BufferAttribute(sharedPosArr, 3);
sharedPos.setUsage(THREE.DynamicDrawUsage);

for (const g of GROUPS) {
  const slice = indices.subarray(g.start, g.end);
  const geo   = new THREE.BufferGeometry();
  geo.setAttribute('position', sharedPos);            // SAME ref across groups
  geo.setIndex(new THREE.BufferAttribute(slice, 1));
  scene.add(new THREE.LineSegments(geo, new THREE.LineBasicMaterial({color: g.color})));
}

function setFrame(t) {
  sharedPosArr.set(positions.subarray(t * M * 3, (t + 1) * M * 3));
  sharedPos.needsUpdate = true;
}
```

Per-group `BufferGeometry` is fine; per-group `BufferAttribute` is wasted memory. With 324 groups sharing one float buffer, the iframe stays under 250 MB JS heap during 31-frame playback.

---

## 10. End-to-end recipe

The full pipeline used by [a4db_parse](../scripts/compute/a4db_parse/code.py) + [a4db_3d](../scripts/reports/a4db_3d/code.py):

1. **Magic check** — HDF5 or FEMZIP? Reject FEMZIP cleanly.
2. **Open** with `h5py`, read `nodes_0/grids/coordinates`.
3. **Read topologies** for quads/hexas/beams; reshape to `(E, K)`, treat values as 0-based indices.
4. **Walk `properties_0`** per element type; each property dataset is a list of 0-based topology indices (a partition of the element array). Group by `attrs.title`.
5. **Build edges**: shell silhouette = quad edges shared by exactly 1 quad; hex skin = faces shared by exactly 1 hex, then dedupe edges.
6. **Run the three culling passes** on the full mesh.
7. **Subsample** clean quads → re-index to dense vertex set (the "active nodes").
8. **Read displacements** at active nodes for every step; transform to Y-up unit-cube space.
9. **Quantize** to int16, base64-encode, inline in `srcdoc`.
10. **Render** with shared position attribute + per-group index buffers.

Bug-class summary, in order of how often each bites:

| Bug | Symptom | Fix |
| ---- | ---- | ---- |
| Skipped Pass 2 (edge-growth culling) | hairball at frame 30, viewer unusable | run Pass 2 unconditionally on the full mesh |
| Subsampled before culling | random ghost stretches survive | always cull → then subsample |
| Used `nodes_0/grids/ids` as indices | every element renders at origin or fails | use 0-based array positions, ignore `ids` |
| `camera.up = (0,0,1)` instead of axis swap | OrbitControls broken, helpers wrong | swap on the Python side: `[x, z, -y]` |
| Used `pn.pane.Matplotlib` for damage colour | render-time error past the smoke test | render eagerly to PNG bytes (see 07) |
| Inlined raw JSON of positions | iframe hangs > 25 MB | switch to base64 interleaved Float32Array |
| Per-group `BufferAttribute` instead of shared | JS heap balloons; FPS tanks | one `BufferAttribute`, many `BufferGeometry`s |

---

## 11. Reference pipeline architecture

The compute → JSON → report split is the load-bearing pattern for animated FEM views. A working two-script implementation looks like this:

```
┌─────────────────────────────────────────────────────────────────────┐
│  SOURCE                                                             │
│  py_xxx   <run>.a4db                ~1–2 GB  (HDF5 container)       │
└──────────────────────────────┬──────────────────────────────────────┘
                               │ ctx.snapshot(handle).path
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│  COMPUTE  a4db_precompute_binary   (h5py + numpy + base64)          │
│                                                                     │
│   model_0/geometry_0/              results_0/step_<i>/              │
│   ├─ nodes/coords      ──┐         ├─ displacements/{x,y,z}  ──┐    │
│   ├─ tris/topology     ──┤         └─ function_*/.../damage   ──┤    │
│   ├─ tris/pid          ──┤                                     │    │
│   └─ parts (PID→name,t)──┤                                     │    │
│                          ▼                                     ▼    │
│   ┌──────────────────────────────┐    ┌────────────────────────────┐│
│   │ STATIC                       │    │ PER-FRAME                  ││
│   │  • nodes  (N×3)  float32     │    │  pos = coords + disp       ││
│   │  • index  (3·T)  uint32      │    │  dmg = elem→node mean      ││
│   │  • pid_per_tri   int32       │    │  pack: x,y,z,d float32     ││
│   │  • tri_layer_grp int8        │    │  (NF frames)               ││
│   │    (top-group of tri's PID)  │    └──────────┬─────────────────┘│
│   └──────────────┬───────────────┘               │                  │
│                  │                               │                  │
│   ┌──────────────┴───────────────┐               │                  │
│   │ COMPONENT TREE (3 levels)    │               │                  │
│   │  PID → part-name regex →     │               │                  │
│   │  sub-group → top-group       │               │                  │
│   │  layers_tree[].subs[].parts[]│               │                  │
│   │  layer_rgb[gi] = [r,g,b]     │               │                  │
│   └──────────────┬───────────────┘               │                  │
│                  │                               │                  │
│                  └────────────┬──────────────────┘                  │
│                               │ base64-encode all typed arrays      │
│                               ▼                                     │
│            json.dump → /tmp/<run>_mesh.json   (~tens of MB)         │
└──────────────────────────────┬──────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│  REPORT  a4db_threejs_anim   (Panel HTML iframe, srcdoc)            │
│                                                                     │
│  Python build(ctx): read JSON → embed verbatim in <script> as DATA  │
│                                                                     │
│  Browser (three.js, ES module):                                     │
│   ① decode b64 → typed arrays (Float32 / Uint32 / Int32 / Int8)     │
│   ② loop frames:  buf → allPos[fi] (xyz),  allDmg[fi] (d)           │
│   ③ build pidToTris  +  vertGrp[i] ← group of first tri using i     │
│                                                                     │
│   BufferGeometry                                                    │
│    ├─ index    ← liveIdx (mutable, gated by hiddenPids)             │
│    ├─ position ← allPos[currentFrame]                               │
│    └─ color    ← per-vertex, computed each frame:                   │
│         LAYERS: LAYER_RGB[ vertGrp[i] ]   (pure, frame-invariant)   │
│         DAMAGE: plasmaRGB( dmg[i] )                                 │
│                                                                     │
│   UI                                                                │
│    ├─ slider / play  → updateFrame(fi)                              │
│    ├─ LAYERS / DAMAGE buttons → colorMode                           │
│    └─ component tree checkboxes → setPidVisible → rebuildIndices    │
└─────────────────────────────────────────────────────────────────────┘
```

The split exists because `build(ctx)` has a hard ~120 s timeout and runs on every page load (see [07-report-scripts.md](07-report-scripts.md)). Anything iterating Python over per-element data — building the component tree, computing per-vertex damage, packing per-frame interleaved arrays — belongs in the compute step. The report just `json.load`s the precomputed artefact and embeds it verbatim into the iframe `<script>` as `DATA`.

### Wire format — the seven arrays

| Array | dtype | size | Meaning |
| ---- | ---- | ---- | ---- |
| `nodes` | f32 | N×3 | Rest coordinates (Y-up unit-cube) |
| `index` | u32 | 3·T | Triangle vertex indices (after quad→tri split) |
| `pid_per_tri` | i32 | T | Property / part ID per triangle |
| `tri_layer_grp` | i8 | T | Top-level group index per triangle (`-1` if unmapped) |
| `frames[fi]` | f32 | N×4 | `(x, y, z, damage)` per node, per frame |
| `layer_rgb` | json | G×3 | Base RGB colour per top-group |
| `layers_tree` | json | tree | top → sub → parts (PIDs, names, thickness `t`, element count) |

All numeric arrays are base64-encoded `bytes` of contiguous typed arrays. `frames` is a *list* of base64 chunks — one per timestep — so neither side has to materialise the full `(NF, N, 4)` cube at once.

### Static vs per-frame split

**Computed once, sent once:**
- `nodes` (rest coords, Y-up swap already applied)
- `index` (triangle topology, after quad → 2-tri fan)
- `pid_per_tri`, `tri_layer_grp`
- `layer_rgb`, `layers_tree`

**Computed per timestep, sent per timestep:**
- `pos = nodes + disp` (Y-up swap applied to disp first)
- `dmg` = element-scalar → node mean (per-element scalars become per-vertex via averaging over each node's incident elements)
- Packed as `[x, y, z, d]` interleaved float32, one frame at a time

The element→node mean for damage is the only step where you need an incidence table. Build it once on the Python side: for each node `i`, the list of triangles using it; then `node_dmg[i] = mean(tri_dmg[t] for t in tris_of[i])`. ParaView's "Cell Data to Point Data" filter does the same thing.

### Browser side — colour gating

Per-vertex colour is recomputed inside the animation loop based on `colorMode`:

| Mode | Source | Per-frame work |
| ---- | ---- | ---- |
| LAYERS | `LAYER_RGB[ vertGrp[i] ]` | None — frame-invariant; can be cached on first set |
| DAMAGE | `plasmaRGB(dmg[i])` | One LUT lookup per vertex per frame |

`BufferGeometry` keeps `position` and `color` as separate `DynamicDrawUsage` attributes; only `position` and (for DAMAGE mode) `color` are touched per frame. LAYERS-mode playback skips the colour update entirely.

### Vertex-to-group attribution

`vertGrp[i]` is set to the top-group of the **first triangle that uses node `i`** during the topology read. O(1) lookup per vertex at colour time, at the cost of arbitrary group assignment for nodes shared between groups. In practice the seam is invisible because boundary silhouettes are usually rendered as a separate wireframe overlay in its own colour.

### Component-tree visibility — no geometry rebuild

The checkbox tree builds a `pidToTris: Map<int, int[]>` at startup. Toggling visibility goes through:

```js
function setPidVisible(pid, visible) {
  if (visible) hiddenPids.delete(pid); else hiddenPids.add(pid);
  rebuildIndices();   // writes liveIdx in-place
}

function rebuildIndices() {
  let w = 0;
  for (let t = 0; t < TRI_COUNT; t++) {
    if (hiddenPids.has(pidPerTri[t])) continue;
    liveIdx[w++] = index[3 * t];
    liveIdx[w++] = index[3 * t + 1];
    liveIdx[w++] = index[3 * t + 2];
  }
  geom.index.array = liveIdx.subarray(0, w);
  geom.index.needsUpdate = true;
  geom.setDrawRange(0, w);
}
```

No buffer reallocation, no geometry rebuild — just a write into a preallocated `Uint32Array` and a `setDrawRange`. Toggling 50 % of parts visible/hidden is sub-millisecond on a 200 K-triangle mesh.

---

## After editing this file

The chat backend uses a hybrid (BM25 + dense) RAG index over `docs/`. Changes here are not visible to the agent until the index is rebuilt:

```
python rag/build_rag.py
```

Each run is a full rewrite of `rag/.chroma/` and `rag/.bm25/`.
