# Screenshot-friendly authoring

The screenshot system captures whatever your code renders into the
DOM. It uses `html-to-image` (SVG-foreignObject snapshot) + a 3D
canvas compositor for three.js scenes. Both have limits — write
your reports to stay inside them.

## Two rules above all others

### Rule 1: SVG styling must be INLINE attributes, not CSS classes

The screenshot library snapshots the DOM into an SVG
`<foreignObject>` blob. It does NOT reliably inline `<style>`
rules. SVG elements styled via classes end up unstyled in the
screenshot — typically painting black/transparent with no text.

**Wrong** (produces black-box screenshots):
```html
<style>
  .node-rect { fill: #fff; stroke: #1a2230; stroke-width: 2; }
  .node-text { font-size: 14px; fill: #000; text-anchor: middle; }
</style>
<svg><rect class="node-rect"/><text class="node-text">A</text></svg>
```

**Right** (screenshots clean):
```html
<svg>
  <rect fill="#fff" stroke="#1a2230" stroke-width="2"/>
  <text font-size="14" fill="#000" text-anchor="middle">A</text>
</svg>
```

This applies ONLY to SVG element styling. Regular HTML elements
(`<div>`, `<body>`, etc.) styled via `<style>` blocks work fine —
the screenshot captures the cascaded computed style for those.

### Rule 2: Stable end-state

The screenshot happens at one moment. There is no "wait for
animation to finish" logic beyond a small settle delay (~1500 ms
after `networkidle`). If your content is moving when the screenshot
fires, the frame is whatever it was at that millisecond. For
dashboards you want crisp captures of, the content must be
visually settled before the timer expires.

### Rule 3: SVG width/height for size-correct iframe

When your report's content is mostly an SVG, set the `<svg>`'s
`width` and `height` attributes to the actual pixel extent of
your content (matching the viewBox). Using `width="100%"
height="100%"` lets the iframe-auto-sizer measure intrinsic
aspect-ratio against the desktop width, which can produce a
multi-thousand-pixel-tall screenshot of a small diagram.

```js
// Compute extent from your layout, set both viewBox AND pixel size:
svg.setAttribute("viewBox", `${minX} ${minY} ${w} ${h}`);
svg.setAttribute("width", w);
svg.setAttribute("height", h);
```

### Rule 4: NO viewport-sized containers (`100vh`, `100vw`)

The screenshot path resizes the iframe to ~8000 px during the
height-probe so it can measure your content's natural extent.
If any element in your CSS uses `100vh` / `100vw` / `min-height:
100vh` / `height: 100%` cascaded from `body { height: 100% }`,
that element grows to fill the probe height — and the iframe
measurement now says "your report is 8000 px tall."

The trim pass tries to crop the trailing background tail, but
if it fails (canvas tainted, ambiguous corner color, etc.), you
get a giant near-empty screenshot with your tiny diagram at the
top.

**Do NOT use:**
- `body { min-height: 100vh }` ← classic offender
- `body { height: 100% }` + `html { height: 100% }`
- `.container { min-height: 100vh }`
- Any flex container with `flex: 1 0 100vh`

**Do use:**
- `body { margin: 0; padding: 16px }` — let height be natural
- Fixed `px` / `rem` sizes
- Grids that size to their content rows

```html
<!-- WRONG: blows the screenshot up to ~8000px tall -->
<body><div style="min-height: 100vh; padding: 40px">
  <h1>Title</h1>
  <svg width="600" height="500">…</svg>
</div></body>

<!-- RIGHT: iframe sizes to natural content extent -->
<body style="margin: 0; padding: 40px">
  <h1>Title</h1>
  <svg width="600" height="500">…</svg>
</body>
```

## Network-loaded content

The shim waits for `networkidle` then sleeps `expand_settle_ms`
(default 1500 ms). If your code:

- `fetch()`s data on mount → ensure it completes within that window
- Lazy-loads images via `IntersectionObserver` → may not have
  triggered before the screenshot. Set `loading="eager"` on
  critical images or render them above the fold.
- Uses streaming responses → won't finish before the screenshot.

If you need a longer wait, pass `expand_settle_ms` to
`screenshot_report` (default 1500, max ~10000).

## Three.js

For 3D canvases to appear in the screenshot, the WebGL renderer
**must** be created with `preserveDrawingBuffer: true`:

```js
const renderer = new THREE.WebGLRenderer({
  canvas,
  antialias: true,
  preserveDrawingBuffer: true,   // ← required
});
```

Without it, `canvas.toDataURL()` returns blank because WebGL
clears the drawing buffer after compositing.

`ctx.three_scene(...)` does this for you automatically. If you're
writing raw three.js in an HTML report, you must set it yourself.

## Cross-origin content

### Images
A `<img src="https://other-domain.example/pic.png">` will render
in the page, but on screenshot the canvas gets "tainted" and
`toDataURL()` throws `SecurityError`. To avoid this:

- `<img crossorigin="anonymous" src="...">` AND
- The source server must send `Access-Control-Allow-Origin: *` (or
  matching) headers. If it doesn't, you can't include the image.

### Stylesheets (`<link rel="stylesheet" href="cross-origin">`)
Cross-origin stylesheets fail CSSOM rule access during the
inlining pass. The screenshot still works — fonts/colors fall
back to whatever the SVG-foreignObject path can do — but the
result may visually differ from the live page. Self-host fonts
and CSS for pixel-perfect screenshots.

### Iframes
- **Same-origin iframes**: captured if their document responds to
  the `voitta_three_capture` postMessage. The `ctx.three_scene`
  viewer does this; nothing else does by default.
- **Cross-origin iframes**: render as blank rectangles. No
  workaround from page JS.

## CSS that doesn't translate cleanly

`html-to-image` approximates these. They render, but may look
slightly different in the screenshot vs live:

- `mix-blend-mode`, `backdrop-filter`, complex `filter:` chains
- `box-shadow` with very large blur radii
- `clip-path` with SVG references
- Custom fonts loaded via `@font-face` from cross-origin sources

Workarounds:
- For shadows that matter, draw them as SVG `<filter>` so the
  screenshot path captures them faithfully
- For blend modes, pre-composite as a single static image
- For fonts, self-host the WOFF2 and load via same-origin

## Layout pitfalls

### Viewport-relative units
`100vh`, `100vw` resolve against the iframe size, NOT the parent
window. The screenshot shim auto-resizes the iframe to natural
content height before capture — `100vh` becomes whatever ends up
being chosen. Prefer fixed `px` / `rem` / `em` if you care about
the exact size in the screenshot.

### Horizontal scrolling
The screenshot captures the natural content width but doesn't
scroll horizontally. Anything past the iframe's chosen width
gets clipped. Lay content out vertically; wrap or stack at the
viewport edge.

### `position: fixed`
Fixed elements anchor to the iframe viewport. Since the iframe
gets resized for capture, fixed elements end up at unexpected
positions in the screenshot. Avoid `position: fixed` in report
output.

### Interactive zoom / pan / fit-all controls

Adding zoom/pan/fit-all buttons creates a hard conflict with the
probe pass **if** you also use `100vh` for layout. The probe
resizes the iframe to ~8000 px — any `fitAll()` that reads
`viewport.clientHeight` or a flex container's `.clientHeight`
sees that inflated height and scales the diagram down to ~9%,
producing a tiny thumbnail in a near-empty 8000 px screenshot.

`requestAnimationFrame` delays do not fix this; the root cause is
the wrong height source, not the timing.

**The correct pattern — natural body height + `window.innerWidth/Height`:**

```html
<!-- Body stays natural height — no height:100%, no 100vh -->
<body style="margin:0; padding:16px; background:#0b0f14">
  <div id="wrap" style="position:relative; display:inline-block">
    <svg id="diagram"></svg>
    <!-- toolbar: position:absolute (NOT fixed) so it moves with
         content during the probe and stays in the right place
         in the screenshot -->
    <div id="toolbar" style="position:absolute; top:10px; right:10px; z-index:10">
      <button onclick="zoom(1.2)">+</button>
      <button onclick="zoom(1/1.2)">−</button>
      <!-- fit-all icon: dashed square (⬚  U+2B1A) -->
      <button onclick="fitAll()" title="Fit all">⬚</button>
    </div>
  </div>
</body>
```

```js
let scale = 1, tx = 0, ty = 0;
const svg = document.getElementById("diagram");

function applyTransform() {
  svg.style.transform = `translate(${tx}px,${ty}px) scale(${scale})`;
  svg.style.transformOrigin = "0 0";
}
function zoom(factor) { scale *= factor; applyTransform(); }

// Called inside elk.layout().then() after setting svg width/height:
let dgW, dgH;
function fitAll() {
  scale = Math.min(window.innerWidth / dgW, window.innerHeight / dgH) * 0.92;
  tx = (window.innerWidth - dgW * scale) / 2;
  ty = 20;
  applyTransform();
}

elk.layout(graph).then(laid => {
  // ... compute vbW, vbH, set viewBox ...
  dgW = vbW; dgH = vbH;
  svg.setAttribute("width", vbW);
  svg.setAttribute("height", vbH);
  // ... paint nodes/edges ...
  fitAll();  // no rAF needed — window dimensions are synchronous
});
```

`window.innerWidth/Height` resolve against the real browser window,
not the iframe probe height, so `fitAll()` computes the correct scale
in both interactive and screenshot contexts.

**Result:**
- Interactive: diagram fills the user's screen, zoom/pan works. ✅
- Screenshot: body height = natural SVG height → full diagram captured at
  its unscaled pixel size; toolbar appears at its absolute position. ✅

**If you must use `100vh`** (e.g., you want a viewport-clipped interactive
shell), accept that automated screenshots require `expand_height` set to
the real pixel height:
```python
screenshot_report(expand_height=900)  # must match actual viewport height
```
This bypasses the probe entirely but requires knowing the height in advance.

Summary table:

| Technique | Interactive fit-all | Screenshot |
|---|---|---|
| `100vh` + `viewport.clientHeight` | ✅ | ❌ 8000 px probe destroys scale |
| `100vh` + `screenshot_report(expand_height=N)` | ✅ | ✅ manual height required |
| Natural body height + `window.innerHeight` | ✅ | ✅ full diagram |
| `position: fixed` toolbar | ✅ | ❌ wrong position |
| `position: absolute` toolbar | ✅ | ✅ moves with content |
| Double `requestAnimationFrame` delay | ❌ wrong root cause | ❌ |

### Tall reports
Vertical extent is handled — the iframe grows to fit content and
the full-page screenshot follows. No special handling needed for
tall dashboards. Just don't scroll horizontally.

## ELK reports specifically

- The renderer paints raw SVG inside each node's `<g>`. SVG
  primitives screenshot cleanly — no font/CSS edge cases
- For `<foreignObject>` content inside a node, the rules above
  about HTML apply: cross-origin fonts/images, complex CSS, etc.
- Edges read styling attrs (`stroke`, `arrowhead`, etc.) — those
  go through ELK's polyline + minimal SVG, no edge cases

## HTML reports specifically

- Your raw HTML becomes the iframe document
- The screenshot shim is auto-injected into `<head>`
- CSS variables from `ctx.theme()` are NOT auto-injected — you
  must include the `:root { --voitta-bg: ...; }` block yourself,
  OR read the dict and substitute values directly into your CSS

## Quick checklist before submitting an HTML report

- [ ] Doctype + `<html>` + `<head>` + `<body>` all present
- [ ] No `position: fixed` on content
- [ ] No `100vh` / `100vw` for sizing
- [ ] Self-hosted fonts (or accept system-font fallback)
- [ ] No animations without a settled end-frame
- [ ] All images same-origin OR have `crossorigin="anonymous"`
      + source CORS headers
- [ ] Three.js renderers (if any) use `preserveDrawingBuffer: true`
- [ ] Data fetches complete within `expand_settle_ms` (default 1500)
- [ ] Zoom/pan toolbars use `position: absolute` (not `fixed`) inside
      a `position: relative` wrapper; fit-all reads `window.innerWidth/Height`,
      not `viewport.clientHeight`
