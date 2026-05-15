# Panel + Three.js interactive reports — recipe

Companion to [07-report-scripts.md](07-report-scripts.md). That doc covers static (matplotlib → PNG) reports; this one is the playbook for reports with **interactive 3D, WebGL, or multi-iframe widgets** (model viewers, structure trees, animated dashboards).

## When to use Three.js (`ctx.three_scene`) — decision guide

**Reach for `ctx.three_scene(scene_js, height=…)` whenever the user asks for ANY of:**

- "rotating 3D" / "spinning" / "auto-rotating" / "orbiting" 3D visualization
- "interactive 3D" / "drag to rotate" / "draggable" 3D plot
- 3D scatter, 3D surface, 3D line, 3D cube/sphere/torus/etc.
- 3D model viewer, mesh viewer, point cloud viewer
- WebGL anything, Three.js anything
- Animated 3D, real-time 3D
- "3D visualization" without qualifier — default to Three.js (interactive beats static)
- Multiple side-by-side 3D plots (use a `pn.GridBox` of `ctx.three_scene(...)` panes)

**DO NOT use matplotlib + GIF / animation / `view_init` sweep for "rotating 3D":**

- Matplotlib animations are rasterised, look janky, take forever to render, and bloat the report.
- `ctx.three_scene` gives the user real drag-to-rotate + wheel-zoom out of the box with one Python call. Use it.

**Use Plotly's `pn.pane.Plotly` (with `Scatter3d` / `Surface` / `Mesh3d`) only when:**

- The user explicitly asks for "Plotly" by name
- You need built-in axes / tick labels / colorbar / hover tooltips for a 3D scientific plot
- The data is a dense surface/heatmap with picking semantics (Plotly's defaults are saner here)

**Use `pn.pane.DeckGL` only for:** geographic 3D maps, hexbin layers, ArcLayer, etc. — not for general 3D scenes.

These tips below are battle-scars from shipping a working car-FEM viewer (Three.js + a checkbox tree controlling per-group visibility & color, geometry baked from a numpy mesh). Almost every one represents a class of bug that wasted hours because the failure was silent — no error in the chat, just a black canvas or unresponsive UI.

> **Read 07 first.** The two-stage error model (build vs. render) applies here too; the smoke test only catches `build(ctx)` failures, never anything that goes wrong inside the iframe.

## Verifying that a Three.js report works — read this BEFORE you call screenshot_report

Three.js / WebGL reports render inside a sandboxed `<iframe srcdoc>` (this is the only way Panel will let live JS run — see TL;DR below). That sandbox boundary breaks two of your normal verification tools:

1. **`screenshot_report` returns a blank canvas for the Three.js area.** TWO compounding causes, both unavoidable from Python:
   - WebGL's drawing buffer is cleared after compositing unless you set `preserveDrawingBuffer: true` on the renderer.
   - `ctx.three_scene` uses `sandbox="allow-scripts"` *without* `allow-same-origin`, which is correct for isolation but means `html2canvas` (running in the parent Bokeh document) cannot reach into the iframe's DOM. The canvas is invisible to the rasteriser — even with `preserveDrawingBuffer: true` you'd still get blank.
   - **What a blank screenshot of a Three.js report means: nothing.** Not "the scene is broken", not "the content changed". It means the rasteriser couldn't see into the sandbox. The scene is still running.
   - **Rule: never use `screenshot_report` to verify a Three.js / WebGL / `ctx.three_scene` report.** It will mislead you into "fixing" working code.

2. **`get_report_render_errors` does NOT see errors thrown inside `scene_js`.** The error listener lives in the Bokeh shim that runs in the outer document; errors inside the sandboxed `srcdoc` iframe don't propagate up to it. A syntax error or a `THREE is not defined` inside your `scene_js` will be invisible to that tool.
   - To see inner-iframe errors: ask the user to open DevTools → Console while focused on the iframe (Chrome lets you pick the iframe in the context selector at the top of the console).

**Ground truth for Three.js reports is the user's eyes, not your tools.** After `define_report` + `show_holoviz_report`, ask the user "do you see the cubes rotating?" or "what colours / shapes do you see?". Don't read a blank screenshot and rewrite working code.

The same rule applies to any iframe-srcdoc-based custom JS, not just Three.js — D3 inside an iframe, Cytoscape, observable-plot, any WebGL.

## What `ctx.three_scene` guarantees — do NOT add per-scene workarounds

The helper's boot script handles five things you might be tempted to redo inside `scene_js`. **Don't.** Adding them again either duplicates work or fights the helper's bookkeeping.

| Concern | How the helper handles it | What you should NOT write in `scene_js` |
|---|---|---|
| **Camera aimed at origin after `position.set`** | After your `scene_js` runs, the helper calls `camera.lookAt(target)` (where `target = Vector3(0,0,0)`). Without this, raw Three.js leaves the camera looking down -Z from wherever you placed it, and your geometry renders in the bottom-left corner. See "camera.position.set does NOT aim the camera" below. | Don't add `camera.lookAt(0, 0, 0)` yourself — it's redundant. If you want a non-origin lookAt, you need the custom-iframe pattern (drag-orbit is hard-coded to origin in this helper). |
| **Canvas sizing on initial layout AND every reflow** | `ResizeObserver` on `document.body` reading `contentBoxSize`. Fires once on observation (catches the first valid layout tick) and on every subsequent layout shift (drawer resize, window resize, grid reflow). | Don't add your own `ResizeObserver` / `window.addEventListener("resize", …)` / `renderer.setSize` calls. |
| **0-size first frame** | `renderer.render` is skipped when canvas is 0×0 — no garbage frame into a zero-pixel buffer during the brief window between iframe load and the first ResizeObserver tick. | Don't add `if (canvas.width === 0) return` guards. |
| **Camera aspect on resize** | `camera.aspect = w / h; camera.updateProjectionMatrix()` runs every viewport change. Guarded against divide-by-zero. | Don't update `camera.aspect` yourself. |
| **Drag works when the pointer leaves the canvas mid-drag** | `canvas.setPointerCapture(e.pointerId)` on pointerdown, released on pointerup / pointercancel. The canvas keeps receiving move/up regardless of where the pointer ends up. | Don't attach move/up handlers to `window` or `document` to compensate. |

If you find yourself writing a `ResizeObserver` inside `scene_js`, it's a sign the helper's misbehaving and the bug belongs in core. File it instead of working around it locally — workarounds get copied across reports.

### `camera.position.set(...)` does NOT aim the camera — `lookAt` is required

This is the #1 mistake every new Three.js user makes, and it produces a very specific symptom worth memorising.

**Symptom.** Geometry renders crammed into the bottom-left corner of an otherwise-empty viewport. Often just a tiny nub of the object pokes into view; the rest is outside the camera's frustum.

**Cause.** `camera.position.set(x, y, z)` only **places** the camera. It does **not** change where the camera looks. The camera's forward vector stays at its construction default of `(0, 0, -1)`. From `(2, 1.8, 3)` looking straight down the -Z axis, the origin (where you put the cube) sits behind-and-below-left of the frustum. Project the object's bounding-sphere centre into NDC and you get something like `(-1.17, -1.04)` — outside the `[-1, 1]²` clipping square on both axes, hence "bottom-left".

**Fix (raw Three.js / custom iframes).** Always call `camera.lookAt(...)` after `camera.position.set(...)`:

```js
camera.position.set(2, 1.8, 3);
camera.lookAt(0, 0, 0);   // ← THIS LINE. Without it: bottom-left disaster.
```

**Fix (`ctx.three_scene`).** The helper auto-aims at the drag-orbit centre (`Vector3(0, 0, 0)`) after your `scene_js` runs, so this footgun is closed inside the helper. You don't need to write `camera.lookAt(0, 0, 0)` yourself when using `ctx.three_scene`. (You still can — it's harmless; the helper's lookAt happens after your code.) If you want the camera looking at a non-origin point, the helper's hard-coded drag-orbit centre would override you on the first drag; in that case use the custom-iframe pattern documented further down.

### Diagnosing "geometry renders in the wrong place" — the NDC/screen-px trick

"Renders in the wrong place" is ambiguous between three completely different bugs:

1. **Canvas mis-sized** (renderer dimensions wrong, aspect wrong)
2. **Canvas mis-positioned** (iframe offset, CSS layout bug)
3. **Camera mis-aimed** (the bug above — geometry is correctly placed, but the camera isn't looking at it)

Projecting the object's bounding-sphere centre through the camera into NDC and pixel coordinates distinguishes all three in one log line. Add this temporarily inside `scene_js` (drop it once you've found the bug — diagnostic logs every frame are noise):

```js
// once, after scene + camera are set up, after one render frame
const obj = scene.children.find(c => c.isMesh);   // or whatever you want to inspect
if (obj) {
  obj.updateMatrixWorld(true);
  const bbox = new THREE.Box3().setFromObject(obj);
  const center = bbox.getCenter(new THREE.Vector3());
  const radius = bbox.getBoundingSphere(new THREE.Sphere()).radius;
  const ndc = center.clone().project(camera);  // (x, y) in [-1, 1] if visible
  const w = renderer.domElement.clientWidth, h = renderer.domElement.clientHeight;
  const px = { x: (ndc.x + 1) * 0.5 * w, y: (1 - ndc.y) * 0.5 * h };
  console.log({
    cam_pos: camera.position.toArray(),
    cam_forward: camera.getWorldDirection(new THREE.Vector3()).toArray(),
    obj_center: center.toArray(),
    bsphere_radius: radius,
    renderer_size: [w, h],
    canvas_xy: [renderer.domElement.offsetLeft, renderer.domElement.offsetTop],
    cam_aspect: camera.aspect,
    ndc: [ndc.x.toFixed(3), ndc.y.toFixed(3)],
    screen_px: [px.x.toFixed(1), px.y.toFixed(1)],
  });
}
```

**Reading the output:**

| What the log says | Diagnosis |
|---|---|
| `renderer_size: [314, 320]`, `canvas_xy: [0, 0]`, `cam_aspect ≈ 1.0`, `ndc in [-1, 1]²`, but nothing visible | Probably z-order / opacity / lighting / material bug, not layout |
| `renderer_size: [0, 0]` or `cam_aspect: Infinity` | Sizing bug. Helper's ResizeObserver hasn't fired yet, OR you're in a custom iframe and forgot `renderer.setSize` / aspect updates |
| `renderer_size` looks right, `ndc` is outside `[-1, 1]²` on one or both axes | Camera mis-aimed (the `lookAt` bug above) OR object is genuinely outside the frustum (move camera back, increase `camera.far`, etc.) |
| `screen_px` is negative or > viewport dims | Same as previous row — NDC is the canonical answer; screen pixels are just for human readability |

**Don't log every frame.** Log once at the end of init, then again once per resize event, then stop. Dumping per-frame from a 60 Hz `requestAnimationFrame` loop drowns the console and slows the browser. If you need to confirm the values are stable, log on a debounced timer instead.

## Quickest path: `ctx.three_scene(scene_js, height=…)`

For a simple scene where you just want to add meshes to a default-orbited camera, use the built-in helper. It handles the iframe wrapping (sections 1–2), CDN loading, drag-to-rotate, wheel-zoom, and resize for you:

```python
def build(ctx):
    return ctx.three_scene("""
        const geom = new THREE.BoxGeometry(1, 1, 1);
        const mat  = new THREE.MeshNormalMaterial();
        scene.add(new THREE.Mesh(geom, mat));
        camera.position.set(2, 1.5, 3);
    """, height=520)
```

Inside `scene_js` you have `THREE`, `scene`, `camera`, `renderer` in scope. Default lighting (ambient + directional) is pre-added. The user can drag to orbit and scroll-wheel to zoom — no extra controls code needed.

### Grid of multiple scenes — the "4 rotating 3D visualizations" pattern

```python
def build(ctx):
    import panel as pn
    return pn.GridBox(
        ctx.three_scene("""
            scene.add(new THREE.Mesh(new THREE.BoxGeometry(1,1,1), new THREE.MeshNormalMaterial()));
            camera.position.set(2, 1.5, 3);
        """, height=300),
        ctx.three_scene("""
            scene.add(new THREE.Mesh(new THREE.TorusKnotGeometry(0.6, 0.2, 64, 16), new THREE.MeshNormalMaterial()));
            camera.position.set(2, 1.5, 3);
        """, height=300),
        ctx.three_scene("""
            scene.add(new THREE.Mesh(new THREE.IcosahedronGeometry(1, 0), new THREE.MeshNormalMaterial({wireframe:true})));
            camera.position.set(2, 1.5, 3);
        """, height=300),
        ctx.three_scene("""
            // Random point cloud
            const N = 600, geom = new THREE.BufferGeometry();
            const pos = new Float32Array(N*3);
            for (let i=0; i<N*3; i++) pos[i] = (Math.random()-0.5)*3;
            geom.setAttribute('position', new THREE.BufferAttribute(pos, 3));
            scene.add(new THREE.Points(geom, new THREE.PointsMaterial({size:0.04, color:0x64b5ff})));
            camera.position.set(2, 1.5, 3);
        """, height=300),
        ncols=2,
        sizing_mode="stretch_width",
    )
```

That gives 4 independently-orbitable, wheel-zoomable 3D viz in a 2×2 grid. No GIFs, no PNGs, no matplotlib `view_init` sweeps.

### Dark theme — set it explicitly, not by claim

`ctx.three_scene` defaults to `bg="#1d1d1f"`, which matches the enterprise portal but is wrong on a light host. The end-to-end pattern for a host-themed Three.js report:

```python
def build(ctx):
    theme = ctx.get_theme(host="enterprise.voitta.ai")
    bg = theme["palette"]["surfaces"]["bg"]

    grid = pn.GridSpec(sizing_mode="stretch_width", height=600)
    grid[0, 0] = ctx.three_scene(scene_js_1, bg=bg, height=300)
    grid[0, 1] = ctx.three_scene(scene_js_2, bg=bg, height=300)
    grid[1, 0] = ctx.three_scene(scene_js_3, bg=bg, height=300)
    grid[1, 1] = ctx.three_scene(scene_js_4, bg=bg, height=300)

    # ctx.apply_theme themes the GRID surroundings (Markdown title,
    # body background, dividers) so the 4 dark iframes don't sit on
    # a default-white Panel template.
    return ctx.apply_theme(grid, theme=theme)
```

See `docs/07-report-scripts.md` "Theme the report to match the host page" for the full two-layer model (`apply_theme` for chrome vs `get_theme` for per-chart colours).

The **surrounding Panel template** is handled by our host wrapper automatically — `apply_theme` controls colour, and the host (`_wrap_template` in `backend/app/services/panel_app.py`) chooses `EditableTemplate` vs `VanillaTemplate` and attaches the iframe shim. **Never return `pn.template.*` yourself from `build(ctx)`.** Doing so nests a template inside the host's template and Bokeh rejects the resulting `ListLike` at session-init time (`source="server:template"`; see [07-report-scripts.md § Return a plain layout](07-report-scripts.md#return-a-plain-layout--never-a-template)).

```python
# GOOD — return a plain layout. apply_theme provides the dark surfaces.
def build(ctx):
    grid = pn.GridSpec(sizing_mode="stretch_width", height=600)
    grid[0, 0] = ctx.three_scene("...", height=300)
    grid[0, 1] = ctx.three_scene("...", height=300)
    return ctx.apply_theme(grid, host="enterprise.voitta.ai")
```

The report iframe defaults to whatever the host page set — `#1d1d1f` on the enterprise plugin, `#ffffff` on a plain Voitta install. `ctx.apply_theme` overrides that for both the chrome AND the iframe body, so a three-scene report on a light host can still come back with a dark surface if you ask for one. **Don't claim you set a dark theme without actually calling `apply_theme` (or setting your scene's bg colour to the matching `var(--voitta-bg)`).**

#### Why "I set the canvas background and it's still white" — three independent DOM layers

A three-scene report has **three** DOM trees that each need a background colour, and styling one does not reach the others:

| Layer | What it is | How to style it |
|---|---|---|
| **Three.js canvas** (inside the srcdoc iframe) | `scene.background` / `<canvas>` CSS / the `bg=` arg to `ctx.three_scene(...)` | Already isolated by the sandbox; styling it has zero effect on anything outside. |
| **Panel widget container** (the `pn.Column` / `pn.GridSpec` that holds the iframe) | A Panel-managed `<div>` in the report iframe's body | `styles={"background": "#1d1d1f"}` on the Column / Grid — or let `ctx.apply_theme` handle it. |
| **Outer Bokeh/Panel document** (`<html>` / `<body>` of the report iframe itself) | The Panel app's template chrome around the widget container | `ctx.apply_theme(...)` covers it; for one-offs, `ctx.add_css("html, body, .bk-root { background: #1d1d1f; }")`. |

The canvas being dark while the report is white is the symptom of fixing only layer 1. Reverse is also possible: `apply_theme` set, but `scene.background` left at Three.js's default `null` — outer chrome is dark, the canvas is a white square in the middle.

**Default fix path**: call `ctx.apply_theme(layout, host=...)` once on the returned layout. It handles layers 2 and 3 together. For layer 1, pass `bg=` to `ctx.three_scene(...)` matching the theme (or read it from `ctx.get_theme()` first).

**Escape hatch**: `ctx.add_css(...)` if you need to target Bokeh internals (`.bk-root`, `.bk-Column`, etc.) that `apply_theme` doesn't cover. Use sparingly — `add_css` is a sledgehammer and the selectors are Bokeh-version-coupled.

### Other libraries — `ctx.add_js`

If you need anything beyond simple scenes — multiple iframes talking to each other, custom layout, base64-inlined geometry, structure trees, etc. — read on; the helper is one wrapper around the same iframe-srcdoc pattern documented below.

For arbitrary JS libraries (D3, observable-plot, etc.), call `ctx.add_js("d3", "https://cdn.../d3.min.js")` from `build(ctx)`. The script lands in the report iframe's `<head>` via Panel's `js_files` mechanism, so its globals are available before any layout JS runs.

### Loading a CAD model (GLB / GLTF) into `ctx.three_scene`

This is the most common "real" use of the helper — display a `.glb` produced by `vre_request_asset(asset_type="cad_mesh")`, FreeCAD export, or any other GLTF source.

The helper emits a `<script type="module">` with a Three.js importmap, so `scene_js` can use:

- **Top-level `await`** — `await new GLTFLoader().loadAsync(url)` works.
- **`import 'three/addons/...'`** — bare specifier, resolved by the importmap. Mapping is `"three"` → `three.module.js` build, `"three/addons/"` → `examples/jsm/`. Anything in `examples/jsm/` (`OrbitControls`, `GLTFLoader`, `DRACOLoader`, `EXRLoader`, …) is reachable.

**Recipe** — copy this, change the snapshot handle:

```python
def build(ctx):
    # GLB already pulled into python_storage via fetch_to_python_storage
    rec = ctx.snapshot("py_6cd983a9")
    import base64, pathlib
    b64 = base64.b64encode(
        (pathlib.Path(rec["path"]) / rec["meta"]["stored_name"]).read_bytes()
    ).decode()

    scene_js = f"""
        // GLTFLoader is in examples/jsm/, addressable via the importmap
        // baked into the helper's iframe.
        const {{ GLTFLoader }} = await import('three/addons/loaders/GLTFLoader.js');

        // GLB bytes were base64-inlined from python_storage above.
        // fetch() doesn't work in this iframe — sandbox="allow-scripts"
        // gives it a null origin and every cross-origin response gets blocked.
        const bin = Uint8Array.from(atob("{b64}"), c => c.charCodeAt(0));
        const url = URL.createObjectURL(new Blob([bin], {{type: 'model/gltf-binary'}}));

        const gltf = await new GLTFLoader().loadAsync(url);
        scene.add(gltf.scene);
        camera.position.set(5, 3, 8);
    """
    return ctx.three_scene(scene_js, height=600)
```

**Wrong patterns and what breaks** — the LLM has burned hours on each of these. Don't redo them.

| Wrong pattern | What breaks | Why |
|---|---|---|
| `new THREE.OrbitControls(camera, renderer.domElement)` | `TypeError: THREE.OrbitControls is not a constructor` | The `THREE` namespace doesn't carry addons. Import from `'three/addons/controls/OrbitControls.js'`. And note: you usually **don't need** OrbitControls inside `ctx.three_scene` — drag-rotate + wheel-zoom are already wired. |
| `new THREE.GLTFLoader()` (bare `THREE.` namespace) | `TypeError: THREE.GLTFLoader is not a constructor` | Same reason. Import from `'three/addons/loaders/GLTFLoader.js'` via the importmap. |
| `await import('https://unpkg.com/.../GLTFLoader.js')` (absolute URL, bypassing the importmap) | Works — but the helper's importmap already maps `"three/addons/"`, so the bare-specifier form is shorter and version-pinned to whatever the helper was constructed with. | The absolute-URL form bypasses the importmap; if `GLTFLoader.js` transitively imports `'three'`, the resolution path differs and you can end up with two Three.js modules loaded simultaneously (constructors fail across instances). Prefer `import 'three/addons/...'`. |
| `fetch('https://…/model.glb')` from inside `scene_js` | Network error / opaque CORS failure | The helper's iframe runs under `sandbox="allow-scripts"` (no `allow-same-origin`), giving it a **null origin**. Network fetches from a null origin are blocked by every cross-origin response. Base64-inline the bytes from `python_storage` instead. |
| Adding your own `ResizeObserver` / `renderer.setSize` / `camera.aspect = …` / `camera.lookAt(0, 0, 0)` | Either nothing (redundant) or a fight with the helper | The helper owns sizing, aspect, the resize loop, and the post-`scene_js` `lookAt(target)`. See the guarantees table above. |
| Returning `pn.template.VanillaTemplate(...)` from `build(ctx)` | Bokeh `ValueError` at session-init, `source="server:template"` | Host wrapper already nests your layout in a template; nesting another crashes. Return a plain layout. |

**Inline size ceiling.** Base64 expansion is ~4/3 of the source bytes. A 10.5 MB GLB inlines to ~14 MB of HTML, which is at the practical upper bound for `srcdoc` — works, but slow first paint and high memory. For anything larger, slim the geometry in `run_compute` first (decimate, drop hidden internal parts, use `BufferGeometry` `Float32Array` indices rather than per-face attributes). If you genuinely need >10 MB models, the correct fix is a *served* GLB endpoint with proper CORS — not the sandboxed-srcdoc pattern.

**Tessellation knob.** When fetching a `cad_mesh` from the enterprise RAG, you can also ask the server for fewer triangles:

```python
asset = vre_request_asset(file_id=N, asset_type="cad_mesh",
                          params={"linear_deflection": 1.0})  # mm; default 0.5
```

Larger deflection → coarser tessellation → smaller GLB. Useful when you only need a preview; drop back to 0.05 for inspection-grade detail.

#### Centring and scaling the model

A loaded GLB lands at whatever world origin and scale its exporter chose — almost never (0,0,0) with sensible bounds. The helper's drag-orbit is hard-coded to orbit the origin, so an off-centre model rotates *around* you instead of *under* you. Always centre and scale, immediately on load:

```js
loader.load(url, (gltf) => {
    const model = gltf.scene;
    const box = new THREE.Box3().setFromObject(model);
    const center = box.getCenter(new THREE.Vector3());
    const size = box.getSize(new THREE.Vector3());
    const maxDim = Math.max(size.x, size.y, size.z);
    const s = 3.5 / maxDim;                          // normalise to ~3.5 units
    model.scale.setScalar(s);
    model.position.copy(center).multiplyScalar(-s);  // centroid → origin
    scene.add(model);

    // Frame the camera. The helper's orbit radius defaults to 5 if you
    // don't set camera.position; set position before camera.lookAt
    // would override anything you set on `target`. Helper does its own
    // lookAt(0,0,0) after scene_js, so just placing the camera is enough.
    const d = maxDim * s * 1.4;
    camera.position.set(d, d, d);
});
```

`3.5` is a magic number that pairs well with the helper's default 5-unit orbit radius and ~60° FOV — model fills ~70% of the viewport. Adjust if you want it tighter (smaller scale target) or looser.

#### Z-up data (engineering / CAD) — rotate the **model**, not the camera

STEP, IGES, FreeCAD, and most engineering pipelines emit Z-up geometry. Three.js's camera defaults are Y-up. Inside a custom iframe with `OrbitControls` you'd flip `camera.up = (0,0,1)` and let `controls.update()` re-derive the orbit — but inside `ctx.three_scene` there is no OrbitControls. The helper's drag-rotate math doesn't consult `camera.up`. Setting it on the camera changes nothing visible.

**The correct fix here is to rotate the model itself, before centring:**

```js
loader.load(url, (gltf) => {
    const model = gltf.scene;
    // Z-up → Y-up: rotate -90° around X. This is the same swap
    // recommended in section 4 below ("fix coordinates at export
    // time") but done at load time when you don't control the export.
    model.rotation.x = -Math.PI / 2;
    model.updateMatrixWorld(true);  // makes Box3 below see the rotation

    const box = new THREE.Box3().setFromObject(model);
    // …rest of centre/scale as above.
});
```

`updateMatrixWorld(true)` is necessary because `Box3.setFromObject` reads world matrices, and the rotation you just set lives only in `model.rotation` until the next render tick. Without this line you'd centre and scale based on the *pre-rotation* bbox, which puts the wrong axis vertical and overshoots / undershoots the scale.

Same trick applies for any other "exporter chose weird axes" case — figure out the rotation once, hard-code it on `model.rotation` before the bbox computation. Don't try to fix axes by editing camera or scene properties; that path leads to bugs the helper actively contradicts.

#### Lighting rig — engineering / CAD look

The helper's default lighting (ambient 0.55 + one directional at `(2, 4, 3)`, intensity 0.85) is good enough for "show me the topology" previews. For an engineering / machined-aluminium look — clear surface highlights, lifted shadows, visible edges — replace the default with a 3-light + ambient rig. **Scale every light position with the camera distance** so the rig works for both small parts and large assemblies:

```js
// ld is the per-model "light distance" — match it to the camera distance
// you computed above so the lighting scales with the model size.
const d  = maxDim * s * 1.4;
const ld = d;

// Wipe the helper's default lights so they don't interfere.
scene.children
    .filter(c => c.isLight)
    .forEach(l => scene.remove(l));

scene.add(new THREE.AmbientLight(0xffffff, 0.4));

const key = new THREE.DirectionalLight(0xfff4e0, 2.0);
key.position.set(ld, ld, 1.6 * ld);
key.castShadow = true;
scene.add(key);

const fill = new THREE.DirectionalLight(0xd0e8ff, 0.6);
fill.position.set(-ld, -0.5 * ld, ld);
scene.add(fill);

const rim = new THREE.DirectionalLight(0xffffff, 0.8);
rim.position.set(-0.5 * ld, ld, -ld);
scene.add(rim);
```

| Light | Position | Intensity | Colour | Role |
|---|---|---|---|---|
| Key | `(ld, ld, 1.6·ld)` | 2.0 | `#fff4e0` (warm) | Primary highlight, casts shadow |
| Fill | `(-ld, -0.5·ld, ld)` | 0.6 | `#d0e8ff` (cool) | Lifts dark faces opposite the key |
| Rim | `(-0.5·ld, ld, -ld)` | 0.8 | `#ffffff` | Edge separation from the background |
| Ambient | n/a | 0.4 | `#ffffff` | Floors shadow blacks; prevents pure black |

For the surface itself, override the GLB's materials with `MeshPhysicalMaterial` for the machined-aluminium look:

```js
model.traverse((child) => {
    if (child.isMesh) {
        child.material = new THREE.MeshPhysicalMaterial({
            color: 0xbcc4cc,
            metalness: 0.6,
            roughness: 0.4,
            clearcoat: 0.1,
        });
        child.castShadow = true;
        child.receiveShadow = true;
    }
});
```

Skip the material swap when the GLB already carries authored materials you want to preserve (e.g. assemblies with per-part colours from the CAD tool). The lighting rig works independently and improves both the override-material and original-material cases.

#### Showing a ground-plane grid

If the user asks for a grid, `THREE.GridHelper` is the one-liner:

```js
const grid = new THREE.GridHelper(maxDim * s * 2, 20);
// GridHelper defaults to the XZ plane (Y-up). For Z-up CAD models that
// were rotated -90° around X, rotate the grid to match so it sits
// under the model rather than slicing through it.
grid.rotation.x = Math.PI / 2;
scene.add(grid);
```

`maxDim * s * 2` makes the grid roughly twice the model's largest dimension; tweak if you want it bigger or smaller. Skip it when not asked — most CAD previews look cleaner without one.

#### Multi-component viewers — bake interactivity INTO the srcdoc, don't drive it from Panel

If the user wants a CAD viewer with a "pick component" UI (radio buttons, dropdown, anything that swaps which GLB is in `scene`), the *obvious* layout — Panel `RadioButtonGroup` outside the iframe, `jscallback` reaching into the iframe — does not work. **Avoid the entire pattern.**

| Outer pattern you'll be tempted to try | Why it fails |
|---|---|
| `document.getElementById('cad-viewer-iframe')` from a `jscallback` | Panel wraps each HTML pane in a **Bokeh shadow root**. `document.querySelector` from outer scope returns `null` for anything inside it. |
| `document.querySelector('iframe')` from outer JS | Same shadow-root opacity. |
| `window.postMessage` from a `jscallback` to the srcdoc iframe | `postMessage` on the *outer* `window` fires outer-window listeners only — never reaches inside the srcdoc. |
| postMessage with a "ready" handshake to discover `iframe.contentWindow` | Same shadow-root problem: the bridge code can't get a reference to `iframe.contentWindow` in the first place. |

Root cause in one sentence: **Panel wraps each HTML pane in a Bokeh shadow root, and JS in the outer Bokeh document is blind to nodes inside it.** No amount of cleverness in the outer scope works around this.

**What works:** keep the entire UI inside `scene_js`. The srcdoc is one DOM tree — buttons, click handlers, GLB data map, Three.js scene all live together with zero cross-boundary calls.

```python
def build(ctx):
    # Bake every component's bytes into the srcdoc at build time.
    # Signed URLs would expire; fetch() from a null-origin srcdoc
    # is blocked anyway (see section 9).
    import base64, json, pathlib
    glb_map = {}  # label → base64 bytes
    for label, handle in [("bracket", "py_abc"), ("housing", "py_def")]:
        rec = ctx.snapshot(handle)
        p = pathlib.Path(rec["path"]) / rec["meta"]["stored_name"]
        glb_map[label] = base64.b64encode(p.read_bytes()).decode()

    scene_js = f"""
        const GLB_MAP = {json.dumps(glb_map)};

        // Buttons live in the srcdoc body. Inject them next to the canvas;
        // any container element you create from scene_js works.
        const bar = document.createElement('div');
        bar.style.cssText = 'position:absolute;top:8px;left:8px;z-index:10;';
        for (const label of Object.keys(GLB_MAP)) {{
            const b = document.createElement('button');
            b.textContent = label;
            b.onclick = () => loadComponent(label);
            bar.appendChild(b);
        }}
        document.body.appendChild(bar);

        let currentRoot = null;
        const {{ GLTFLoader }} = await import(
            'https://unpkg.com/three@0.158.0/examples/jsm/loaders/GLTFLoader.js'
        );
        const loader = new GLTFLoader();

        async function loadComponent(label) {{
            const bin = Uint8Array.from(atob(GLB_MAP[label]), c => c.charCodeAt(0));
            const url = URL.createObjectURL(new Blob([bin], {{type: 'model/gltf-binary'}}));
            const gltf = await new Promise((res, rej) => loader.load(url, res, null, rej));
            if (currentRoot) scene.remove(currentRoot);
            currentRoot = gltf.scene;
            // …centre/scale (see "Centring and scaling the model" above)…
            scene.add(currentRoot);
        }}

        // Show the first component on load.
        loadComponent(Object.keys(GLB_MAP)[0]);
        camera.position.set(5, 3, 8);
    """
    return ctx.three_scene(scene_js, height=600)
```

**Mental model:** treat `ctx.three_scene` / srcdoc as a **fully isolated single-page app**. If it needs data, bake it in at Python build time. If it needs interactivity, put the controls inside the srcdoc. Don't try to drive it from Panel widgets outside — the shadow-root boundary makes that impossible regardless of how much wiring you add.

If you genuinely need two-way communication between the report's outer document and the iframe (rare — almost everything fits the "bake it in" model), use the parent-iframe relay from section 3 below. That works because the **parent** is itself an iframe one level above the srcdoc, so it owns both the message-poster side and the message-listener side without any shadow root between them.

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
