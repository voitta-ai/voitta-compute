# Recipe: Three.js scenes

Embed a WebGL 3D scene in an HTML report.

**Critical**: `WebGLRenderer({preserveDrawingBuffer: true})`.
Without it, the canvas reads back blank when the screenshot path
calls `toDataURL()`.

## The full pattern

```python
def build(ctx):
    t = ctx.theme()
    bg = t.get("--voitta-bg", "#0b0f14")
    return f"""<!doctype html>
<html>
<head>
  <style>
    body {{ margin: 0; background: {bg}; }}
    #c {{ display: block; width: 100vw; height: 100vh; }}
  </style>
  <script type="importmap">
  {{
    "imports": {{
      "three": "https://unpkg.com/three@0.160.0/build/three.module.js",
      "three/addons/": "https://unpkg.com/three@0.160.0/examples/jsm/"
    }}
  }}
  </script>
</head>
<body>
  <canvas id="c"></canvas>
  <script type="module">
    import * as THREE from "three";
    import {{ OrbitControls }} from "three/addons/controls/OrbitControls.js";

    const canvas = document.getElementById("c");
    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(60, 1, 0.1, 1000);
    camera.position.set(3, 2, 5);

    // preserveDrawingBuffer: true is REQUIRED so the screenshot
    // path can read pixels via canvas.toDataURL().
    const renderer = new THREE.WebGLRenderer({{
      canvas, antialias: true, preserveDrawingBuffer: true,
    }});
    renderer.setPixelRatio(window.devicePixelRatio || 1);

    function resize() {{
      const w = canvas.clientWidth, h = canvas.clientHeight;
      renderer.setSize(w, h, false);
      camera.aspect = w / Math.max(h, 1);
      camera.updateProjectionMatrix();
    }}
    new ResizeObserver(resize).observe(canvas);
    resize();

    scene.add(new THREE.AmbientLight(0xffffff, 0.5));
    const dir = new THREE.DirectionalLight(0xffffff, 0.8);
    dir.position.set(2, 4, 3);
    scene.add(dir);

    // Your scene content here.
    const geom = new THREE.BoxGeometry(1, 1, 1);
    const mat  = new THREE.MeshNormalMaterial();
    scene.add(new THREE.Mesh(geom, mat));

    const controls = new OrbitControls(camera, canvas);

    function tick() {{
      controls.update();
      renderer.render(scene, camera);
      requestAnimationFrame(tick);
    }}
    tick();
  </script>
</body>
</html>"""
```

## Loading models via GLTFLoader

```js
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";
const gltf = await new GLTFLoader().loadAsync(modelUrl);
scene.add(gltf.scene);
```

## Screenshot timing

For static scenes: the screenshot path waits 1500ms after
`networkidle`. That's plenty of time for THREE module + addons
to load via CDN and render the first frame.

For scenes that need time to settle (physics, particle systems,
loaded models): pass higher `expand_settle_ms` on
`screenshot_report`. Or render a static frame to a `<canvas>`
on demand and remove the animation loop after settling.

## Screenshot-friendly notes

- `preserveDrawingBuffer: true` — see above. Non-negotiable.
- Cross-origin model assets (.glb, .gltf, textures): need CORS
  headers from the source, or `crossorigin="anonymous"` on the
  `<img>` for texture maps. Without these, the canvas taints
  and `toDataURL()` throws.
- Animations: screenshot fires at one moment. Design for a
  stable end-frame if you care what's captured.
