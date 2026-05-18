# Panel report — interactive 3D with `ctx.three_scene`

For any "rotating", "interactive", "draggable", or "WebGL" 3D, use `ctx.three_scene(scene_js, height=…, bg=…)`. Don't render 3D as matplotlib GIFs or `view_init` sweeps — they look janky and bloat the report.

## When to use what

| User says | Use |
|---|---|
| "rotating 3D" / "spinning" / "drag to rotate" / "3D scatter/surface/cube/etc." | `ctx.three_scene(...)` |
| "Plotly" by name, OR 3D scientific plot with axes/colorbar/hover | `pn.pane.Plotly` with `Scatter3d` / `Surface` / `Mesh3d` |
| Geographic 3D map, hexbin, ArcLayer | `pn.pane.DeckGL` |
| "3D visualization" with no qualifier | `ctx.three_scene` (interactive beats static) |

## Minimum scene

```python
def build(ctx):
    return ctx.three_scene("""
        const geom = new THREE.BoxGeometry(1, 1, 1);
        const mat  = new THREE.MeshNormalMaterial();
        scene.add(new THREE.Mesh(geom, mat));
        camera.position.set(2, 1.5, 3);
    """, height=520)
```

Inside `scene_js` you have `THREE`, `scene`, `camera`, `renderer` in scope. Default lighting (ambient + directional) is pre-added. The user can drag to orbit and wheel-zoom out of the box.

## What the helper handles for you — do NOT duplicate

| Concern | The helper does | You should NOT add to `scene_js` |
|---|---|---|
| Camera aimed at origin after `position.set` | Calls `camera.lookAt(0,0,0)` after your script | `camera.lookAt(0,0,0)` (redundant) |
| Canvas sizing on first paint + every reflow | `ResizeObserver` on `document.body` | `renderer.setSize` or `resize` listeners |
| 0-size first frame | Skips render when canvas is 0×0 | `if (canvas.width === 0) return` guards |
| Aspect ratio on resize | `camera.aspect = w/h; camera.updateProjectionMatrix()` | Manual aspect updates |
| Pointer-capture during drag | `canvas.setPointerCapture` on pointerdown | Window/document move listeners |

If you find yourself writing any of those, file an issue — the helper has a bug.

## Background colour

Default is `#1d1d1f` (matches the enterprise dark portal). On a light host, set explicitly:

```python
def build(ctx):
    theme = ctx.get_theme(host="enterprise.voitta.ai")
    bg = theme["palette"]["surfaces"]["bg"]
    return ctx.three_scene(scene_js, height=520, bg=bg)
```

The iframe is sandboxed — `ctx.apply_theme` does **not** reach inside it. The `bg=` parameter is the only way to theme the scene background.

## Grid of multiple scenes

```python
def build(ctx):
    import panel as pn
    return pn.GridBox(
        ctx.three_scene("scene.add(new THREE.Mesh(new THREE.BoxGeometry(1,1,1), new THREE.MeshNormalMaterial())); camera.position.set(2,1.5,3);", height=300),
        ctx.three_scene("scene.add(new THREE.Mesh(new THREE.TorusKnotGeometry(0.6,0.2,64,16), new THREE.MeshNormalMaterial())); camera.position.set(2,1.5,3);", height=300),
        ncols=2,
        sizing_mode="stretch_width",
    )
```

Each scene is independent (independent camera, drag, zoom).

## Loading a CAD GLB into the scene

```python
def build(ctx):
    import base64, pathlib
    rec = ctx.snapshot("snap_glb_001")
    glb_bytes = (pathlib.Path(rec["path"]) / rec["meta"]["stored_name"]).read_bytes()
    b64 = base64.b64encode(glb_bytes).decode()

    return ctx.three_scene(f"""
        const {{ GLTFLoader }} = await import('three/addons/loaders/GLTFLoader.js');
        const bin = Uint8Array.from(atob("{b64}"), c => c.charCodeAt(0));
        const url = URL.createObjectURL(new Blob([bin], {{type: 'model/gltf-binary'}}));
        const gltf = await new GLTFLoader().loadAsync(url);
        scene.add(gltf.scene);
        // CAD is Z-up; Three.js is Y-up. Rotate before bounding-box pass.
        gltf.scene.rotation.x = -Math.PI / 2;
        gltf.scene.updateMatrixWorld(true);
        camera.position.set(5, 3, 8);
    """, height=600)
```

The sandboxed iframe has `null` origin — you can't fetch the GLB by URL inside `scene_js`. Inlining as base64 is the only way.

## The `camera.position.set` footgun (raw Three.js only)

In raw Three.js, `camera.position.set(x, y, z)` **places** the camera but doesn't aim it. Without `camera.lookAt(...)`, the camera looks down the default `-Z` axis and your geometry renders crammed into a corner. **Inside `ctx.three_scene` the helper auto-aims at origin after your script runs**, so this is closed — you don't need to call `lookAt(0, 0, 0)` yourself.

## Verifying a Three.js report

**`screenshot_report` captures `ctx.three_scene` content.** The shim walks Bokeh shadow roots to find each three_scene iframe, requests its canvas pixels via postMessage, and composites the result. The renderer uses `preserveDrawingBuffer: true` to make `canvas.toDataURL()` work. Verified on both cold CDN load and warm re-show.

Check `nested_scenes_captured` in the screenshot response:
- Non-zero → scenes captured; trust the pixels.
- `0` with three_scene panes present → scene is still loading. Retry in a moment, or use `verify_report` for structural confirmation.

**Other verification options when screenshot isn't enough:**
- Ask the user. "Do you see the cube rotating?" "What colour?"
- `verify_report(report_id)` — structural inventory: confirms N roots exist at expected bboxes, without rasterising anything.

**Errors inside `scene_js`** propagate up: the nested iframe forwards its own `window.error` / `unhandledrejection` / `console.error` to the outer shim, tagged `source: "nested:..."`. Visible in `get_report_render_errors`. For deeper debugging, ask the user to open DevTools with the iframe selected in the console's context picker.

(See [panel-screenshot-limits.md](panel-screenshot-limits.md) for the full blindness list.)
