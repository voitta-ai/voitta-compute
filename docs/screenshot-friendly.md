# Screenshot-friendly authoring

The screenshot tool captures the report iframe using `html-to-image` (SVG foreignObject snapshot). Follow these rules to get clean screenshots.

## SVG: use inline attributes, not CSS

`html-to-image` serialises the DOM to SVG. CSS `fill` / `stroke` properties on SVG elements are not reliably inlined into the SVG output. Use inline attributes instead:

```html
<!-- Bad: fill in CSS -->
<circle style="fill: red;" cx="50" cy="50" r="40"/>

<!-- Good: fill as attribute -->
<circle fill="red" cx="50" cy="50" r="40"/>
```

Same for `stroke`, `stroke-width`, `opacity`, etc.

## No `100vh` / `100vw`

Avoid `height: 100vh` or `width: 100vw` in your report HTML. The iframe has a dynamically-sized height; `100vh` inside it means the viewport of the iframe, which can be wrong during capture. Use explicit pixel heights or `min-height` instead.

```css
/* Bad */
.container { height: 100vh; }

/* Good */
.container { min-height: 600px; }
```

## Three.js: `preserveDrawingBuffer: true`

WebGL canvases are not captured by `html-to-image` (they render as blank rectangles). The shim composites Three.js scenes separately via a `voitta_three_capture` postMessage protocol. For this to work, **the renderer must have `preserveDrawingBuffer: true`**:

```javascript
const renderer = new THREE.WebGLRenderer({
  canvas: canvas,
  preserveDrawingBuffer: true,   // required for screenshot
  antialias: true,
});
```

The shim asks each `<iframe>` containing a Three.js scene for its `canvas.toDataURL()` and blits the result onto the screenshot at the correct position.

## Animation loops

If your report has a `requestAnimationFrame` loop, it will still be running when the screenshot is taken. The screenshot captures one frame — this is fine. No special handling needed.

## Fonts

Web fonts loaded via `@import` or `<link>` from cross-origin CDNs (e.g. `fonts.googleapis.com`) will be skipped during CSS inlining (CSSOM rule access is blocked cross-origin). The fallback font will appear in the screenshot. To guarantee font rendering in screenshots, embed fonts as base64 data URIs or use system fonts.

## iframe sizing

The report iframe is sized to match the content's natural height via a `measure` / `reflow` cycle before screenshotting. Reports that return a full-page layout should not set `overflow: hidden` on `<body>` — the measure pass needs the true scroll height.

## Background color

The screenshot background is read from `getComputedStyle(document.body).backgroundColor`. If `body` has a transparent background, the screenshot defaults to white. Set a background color explicitly for dark-theme reports:

```html
<body style="background: #1a1a2e;">
```
