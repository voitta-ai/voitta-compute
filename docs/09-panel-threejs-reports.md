# Panel + Three.js interactive reports — recipe

Companion to [07-report-scripts.md](07-report-scripts.md). That doc covers static (matplotlib → PNG) reports; this one is the playbook for reports with **interactive 3D, WebGL, or multi-iframe widgets** (model viewers, structure trees, animated dashboards).

These tips are battle-scars from shipping a working car-FEM viewer (Three.js + a checkbox tree controlling per-group visibility & color, geometry baked from a numpy mesh). Almost every one represents a class of bug that wasted hours because the failure was silent — no error in the chat, just a black canvas or unresponsive UI.

> **Read 07 first.** The two-stage error model (build vs. render) applies here too; the smoke test only catches `build(ctx)` failures, never anything that goes wrong inside the iframe.

## TL;DR

| If you're doing… | Reach for… |
| ---- | ---- |
| WebGL / Three.js inside a report | `pn.pane.HTML` containing **one** `<iframe srcdoc="...">` |
| Two interactive widgets that need to talk | A **single parent iframe** containing both children + a tiny relay script |
| Heavy geometry parsing | `run_compute` once, save JSON; report just loads & renders |
| Loading geometry into the iframe | **Base64-inline** in `srcdoc` — never `fetch()` |
| Z-up data (FEM/CAD) on a Y-up engine | Swap axes at **export time**, not in shaders or `camera.up` |

---

## 1. Don't put WebGL inside `pn.pane.HTML` directly

`pn.pane.HTML` renders inside a Bokeh `<div>` — it's not a real document context. Symptoms:

- `clientWidth` / `clientHeight` read as **0** at the moment your code runs
- `window.load` never fires
- Three.js canvas sizes to 0×0 → black void
- `addEventListener('resize', …)` works but the initial layout is wrong

**Fix:** wrap your WebGL in an `<iframe srcdoc="...">`. The iframe gets a real `Document`, real layout, real `window.load`, and proper sizing.

```python
import html
import panel as pn

VIEWER_HTML = """<!doctype html><html><head>...</head>
<body><canvas id="c"></canvas><script>...three.js init...</script></body></html>"""

def build(ctx):
    return pn.pane.HTML(
        f'<iframe srcdoc="{html.escape(VIEWER_HTML, quote=True)}" '
        f'style="width:100%;height:600px;border:0"></iframe>',
        sizing_mode="stretch_width",
    )
```

---

## 2. One parent iframe for everything interactive

When two widgets need to talk (e.g. a structure tree controlling a 3D viewer), the layout that **does not work**:

```
Bokeh document
  ├── pn.pane.HTML #1  → <iframe srcdoc> (tree)
  └── pn.pane.HTML #2  → <iframe srcdoc> (viewer)
```

`tree iframe → window.parent` lands in the Bokeh `div` — there's no listener there. The Bokeh layer doesn't relay anything. Messages disappear with no error, no console warning, nothing.

The layout that **does** work:

```
Bokeh pn.pane.HTML
  └── parent iframe srcdoc
        ├── viewer iframe (Three.js)
        ├── tree iframe   (checkboxes / controls)
        └── relay <script>  (forwards messages between siblings)
```

Each child iframe `postMessage`s to `window.parent`; the parent iframe's relay fans out to every other child.

---

## 3. The `postMessage` relay pattern

Inside the **parent iframe**'s `<script>`:

```js
const children = [
  document.getElementById('viewer'),  // <iframe id="viewer">
  document.getElementById('tree'),    // <iframe id="tree">
];

window.addEventListener('message', (e) => {
  // Don't echo back to the sender.
  for (const f of children) {
    if (f.contentWindow !== e.source) {
      f.contentWindow.postMessage(e.data, '*');
    }
  }
});
```

Children just `window.parent.postMessage({type: 'toggle-group', id: 7, on: true}, '*')` — the relay handles delivery.

> **Origin note:** all three iframes are `srcdoc` so their origin is `null` (same as the parent). Passing `'*'` as `targetOrigin` is fine inside this trust boundary because everything was generated server-side from the same script. **Don't** copy this pattern outside the report sandbox.

---

## 4. Three.js is Y-up — fix coordinates at export time

Three.js + `OrbitControls` assume **Y is up**. FEM / CAD data is usually **Z-up**. If you don't fix it, the model loads sideways, the camera orbits weirdly, and `axesHelper` lies to you.

**Don't** do it in a shader or with `camera.up = (0,0,1)` — `OrbitControls` ignores that, and every diagnostic helper is now wrong.

**Do** swap at numpy export time:

```python
# vertices: (N, 3) in Z-up source coords (X, Y, Z)
# Three.js wants (X, Z, -Y) for a standard "length-along-X, up-along-Y" view.
verts_three = np.column_stack([vertices[:, 0], vertices[:, 2], -vertices[:, 1]])
```

Now `axesHelper`, camera, and orbit all just work — no special cases anywhere downstream.

---

## 5. Use `requestAnimationFrame` polling for DOM readiness

Inside an iframe — especially one Panel just injected — `DOMContentLoaded` and `window.load` can fire before your target element exists, or never at all in pathological reload paths. Polling via `requestAnimationFrame` is robust and cheap (one frame ≈ 16 ms, costs nothing):

```js
function waitForEl(id, cb) {
  const el = document.getElementById(id);
  el ? cb(el) : requestAnimationFrame(() => waitForEl(id, cb));
}

waitForEl('viewer-canvas', (canvas) => {
  // Initialise Three.js here — canvas is guaranteed to exist & be in the DOM.
});
```

---

## 6. Precompute heavy geometry in `run_compute`, never in the report

`build(ctx)` has a hard timeout (~120 s — see `smoke_test_report`) and runs **on every page load**. Anything heavy belongs in a one-shot `run_compute` script that writes a JSON / pickle artefact; the report just loads the artefact.

What "heavy" looks like in practice:

- Boundary-edge computation on a 400 K-element mesh
- Group splitting + lookup table construction
- Coordinate transforms across a million vertices
- Anything iterating Python over per-element data

A 30 s `run_compute` step makes a 30 s report. Move it; report startup drops to under 2 s.

---

## 7. Boundary edges, not the full wireframe

Rendering all edges of a shell-element mesh (~400 K elements ≈ ~1.2 M edge segments) is slow and visually useless — the silhouette disappears into a solid grey blob.

Compute **boundary edges** instead — edges shared by exactly one element (i.e. on the surface):

```python
from collections import Counter

# For each element, generate its edge tuples (sorted endpoints so direction
# doesn't matter for the count).
edge_counts = Counter()
for elem in elements:               # e.g. quads/tris
    for a, b in pairs(elem):
        edge = (a, b) if a < b else (b, a)
        edge_counts[edge] += 1

boundary_edges = [e for e, c in edge_counts.items() if c == 1]
```

Typical reduction: ~80 %. Result: clean silhouette wireframe, ~5× fewer line segments, fast render.

---

## 8. One `THREE.LineSegments` per logical group

Don't merge everything into one giant geometry. Build **one `LineSegments` per group** (per body, per material, per part) at load time. Then a checkbox toggle is a one-liner:

```js
group.material.color.setHex(0xff8800);   // recolor — instant
group.visible = false;                    // hide   — instant
```

If you fuse everything into a single geometry, recoloring means rebuilding vertex attributes. Per-group geometry trades a tiny up-front memory cost for instant interaction.

---

## 9. Inline geometry as base64 in `srcdoc` — never fetch

`fetch('/api/geometry/foo.json')` from inside a `srcdoc` iframe is unreliable:

- Relative paths are anchored to `about:srcdoc`, not the parent origin
- Backend static routes may not match the iframe's effective origin
- Cross-origin rules treat `srcdoc` as a unique origin

**Inline the geometry directly.** Base64-encode the bytes server-side, embed as a JS string in the `srcdoc` HTML:

```python
import base64
geom_b64 = base64.b64encode(geom_bytes).decode("ascii")
viewer_html = f"""<!doctype html>...<script>
  const GEOM_B64 = "{geom_b64}";
  const bytes = Uint8Array.from(atob(GEOM_B64), c => c.charCodeAt(0));
  // decode bytes → vertices/indices...
</script>..."""
```

**Size limits in practice:**

| Format | Working ceiling |
| ---- | ---- |
| Base64 binary (typed array decoded in JS) | ~10–15 MB encoded |
| Raw JSON (`JSON.parse` of the whole blob) | hard wall around ~25 MB — Chrome chokes |

If you need more, switch to a binary format (Float32Array packed, plus an index Uint32Array) and decode incrementally. Don't ship 30 MB of JSON.

---

## 10. WebGL canvases are invisible to screenshot tools

The `take_screenshot` tool uses `html2canvas`, which **cannot** capture WebGL canvases — they come back black or white. Don't try to debug 3D scenes through screenshots; the screenshot lies.

Diagnostic loop that actually works:

1. Live iframe + browser DevTools console
2. `THREE.AxesHelper(1)` added to the scene — your most reliable orientation check
3. `console.log(camera.position, camera.up, scene.children.length)` from the iframe
4. Toggle `OrbitControls.enableDamping = false` to see immediate camera-state changes

---

## 11. Camera position formula — "good 3/4 view of an object aligned along X"

With Y-up, model centred at origin, length-along-X (after the swap from §4):

```js
camera.position.set(2.0, 0.8, 1.2);   // front-right, slightly above roofline
camera.lookAt(0, 0, 0);
camera.fov = 45;
camera.near = 0.01;
camera.far = 100;
camera.updateProjectionMatrix();
```

Sanity checks if it looks wrong:

| Symptom | Cause |
| ---- | ---- |
| Upside-down | Y-swap from §4 missing or sign-flipped |
| Looks "from below" | `camera.up` was overridden — should be the default `(0,1,0)` |
| Z-fighting on thin parts | `near` too small relative to scene scale; raise to `0.1` |
| Whole model clipped | `far` too small for scene scale; raise to `1000` |

---

## After editing this file

The chat backend uses a hybrid (BM25 + dense) RAG index over `docs/`. Changes here are not visible to the agent until the index is rebuilt:

```
python rag/build_rag.py
```

Each run is a full rewrite of `rag/.chroma/` and `rag/.bm25/`.
