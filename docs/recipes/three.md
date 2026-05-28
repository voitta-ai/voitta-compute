# Recipe: Three.js scenes

Embed a WebGL 3D scene in an HTML report. Three.js loads from CDN at iframe render time.

## Critical requirement

```javascript
const renderer = new THREE.WebGLRenderer({
  canvas: canvas,
  preserveDrawingBuffer: true,  // REQUIRED for screenshot capture
  antialias: true,
});
```

Without `preserveDrawingBuffer: true`, the canvas is cleared after each frame and the screenshot compositor gets a blank rectangle.

## Basic pattern

```python
def build(ctx):
    return """<!DOCTYPE html>
<html>
<head>
<style>
  body { margin: 0; background: #111; }
  canvas { display: block; width: 100%; height: 500px; }
</style>
</head>
<body>
<canvas id="c"></canvas>
<script src="https://cdn.jsdelivr.net/npm/three@0.160.0/build/three.min.js"></script>
<script>
  const canvas = document.getElementById('c');
  const renderer = new THREE.WebGLRenderer({
    canvas,
    preserveDrawingBuffer: true,
    antialias: true,
  });
  renderer.setSize(canvas.clientWidth, canvas.clientHeight);

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(75, canvas.clientWidth / canvas.clientHeight, 0.1, 1000);
  camera.position.z = 2;

  const geometry = new THREE.BoxGeometry();
  const material = new THREE.MeshNormalMaterial();
  const cube = new THREE.Mesh(geometry, material);
  scene.add(cube);

  function animate() {
    requestAnimationFrame(animate);
    cube.rotation.x += 0.01;
    cube.rotation.y += 0.01;
    renderer.render(scene, camera);
  }
  animate();
</script>
</body>
</html>"""
```

## Animation loop

The `requestAnimationFrame` loop runs continuously while the iframe is visible. Screenshots capture one frame — whichever frame is current when the screenshot is triggered. The loop does not need to stop for screenshotting; `preserveDrawingBuffer: true` ensures the last rendered frame is readable by `canvas.toDataURL()`.

## Sizing

Don't use `height: 100vh` on the canvas or container. Set an explicit pixel height:

```css
canvas { height: 500px; }
```

Or size based on the viewport width with a fixed aspect ratio:

```javascript
const W = window.innerWidth;
const H = Math.round(W * 9 / 16);
renderer.setSize(W, H);
```

## Multiple scenes

Each `<canvas>` with a Three.js renderer gets its own snapshot via the `voitta_three_capture` protocol. The compositor blits each one at its on-page position into the final screenshot. You can have multiple scenes in one report.

## Loading indicator

CDN loads can take a moment. Show a loading state:

```html
<div id="loading" style="color:white;padding:20px">Loading...</div>
<canvas id="c" style="display:none"></canvas>
<script>
  // after Three.js script loads:
  document.getElementById('loading').style.display = 'none';
  document.getElementById('c').style.display = 'block';
</script>
```
