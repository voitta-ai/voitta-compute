# Recipe: ELK diagram via CDN

Load elkjs in the iframe, lay out a graph, paint as SVG. You write
the layout-then-paint code yourself — there's no fixed renderer.

> **Looking for a full proven recipe** with theme, glyphs per
> flow type, animated marching-dash edges, and grid-wrapped
> layout? See [`elk-energy-monitor.md`](elk-energy-monitor.md).
> The doc below is the minimal pattern — use it to learn the
> shape, then graduate to the energy-monitor recipe for
> production-quality diagrams.

## ⚠️ Two screenshot-critical rules

Read these BEFORE writing the recipe; they're the difference
between a clean screenshot and an unreadable one:

1. **Use INLINE SVG attributes, not CSS classes.** html-to-image
   (the screenshot library) does not reliably inline `<style>`
   rules into its SVG snapshot. Set `fill`, `stroke`, `stroke-width`,
   `font-size`, `fill` on text — directly as attributes on each
   element. Putting them in a `<style>` block looks fine in the
   live render but produces black-box screenshots.

2. **Set the SVG's width/height to its actual viewBox extent —
   NOT `100%` × `100%`.** With `100%` width and `100%` height,
   the iframe auto-sizing measures the SVG's intrinsic aspect
   ratio against the desktop width and produces a multi-thousand-
   pixel-tall screenshot. Set explicit pixel dimensions matching
   the layout extent.

3. **No `min-height: 100vh` on body or container.** Same failure
   mode — the screenshot path resizes the iframe to ~8000 px
   during the height-probe; any `100vh`-sized element grows to
   fill it and the screenshot captures a giant near-empty canvas
   with your diagram at the top. Let body height be natural;
   the iframe auto-sizes to content.

## The full pattern

```python
import json

def build(ctx):
    # 1. Build the abstract graph in Python.
    graph = {
        "id": "root",
        "layoutOptions": {
            "elk.algorithm": "layered",
            "elk.direction": "DOWN",
            "elk.edgeRouting": "ORTHOGONAL",
            "elk.spacing.nodeNode": "80",
        },
        "children": [
            {"id": "a", "width": 160, "height": 60, "labels": [{"text": "Source"}]},
            {"id": "b", "width": 160, "height": 60, "labels": [{"text": "Process"}]},
            {"id": "c", "width": 160, "height": 60, "labels": [{"text": "Sink"}]},
        ],
        "edges": [
            {"id": "e1", "sources": ["a"], "targets": ["b"]},
            {"id": "e2", "sources": ["b"], "targets": ["c"]},
        ],
    }

    t = ctx.theme()
    bg = t.get("--voitta-bg", "#fff")
    fg = t.get("--voitta-text", "#000")
    border = t.get("--voitta-border", "#1a2230")
    surface = t.get("--voitta-surface", "#fff")

    return f"""<!doctype html>
<html>
<head>
  <script src="https://unpkg.com/elkjs@0.11.1/lib/elk.bundled.js"></script>
  <style>
    body {{ background: {bg}; color: {fg}; font-family: system-ui;
            margin: 0; padding: 16px; }}
    /* NO min-height: 100vh, NO height: 100% — those make the
       screenshot path balloon the iframe to ~8000 px. Body height
       stays natural; the iframe auto-sizes to content extent. */
    /* SVG paint attributes do NOT live here — every fill/stroke/
       font-size is set inline on each <rect>/<text>/<path> in the
       JS below. html-to-image doesn't reliably inline <style> rules
       into its SVG snapshot; inline attributes always work. */
  </style>
</head>
<body>
  <svg id="diagram"></svg>
  <script>
  // Embed paint values so the JS doesn't have to read CSS vars.
  const PAINT = {{
    node_fill: {json.dumps(surface)},
    node_stroke: {json.dumps(border)},
    node_text: {json.dumps(fg)},
    edge_stroke: {json.dumps(border)},
  }};

  const elk = new ELK();
  const graph = {json.dumps(graph)};
  elk.layout(graph).then(laid => {{
    const svg = document.getElementById("diagram");

    // Compute extent from laid nodes + edge bend points.
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    for (const n of laid.children) {{
      minX = Math.min(minX, n.x); minY = Math.min(minY, n.y);
      maxX = Math.max(maxX, n.x + n.width); maxY = Math.max(maxY, n.y + n.height);
    }}
    for (const e of laid.edges || []) {{
      const sec = e.sections[0];
      for (const p of [sec.startPoint, ...(sec.bendPoints || []), sec.endPoint]) {{
        minX = Math.min(minX, p.x); minY = Math.min(minY, p.y);
        maxX = Math.max(maxX, p.x); maxY = Math.max(maxY, p.y);
      }}
    }}
    const PAD = 40;
    const vbW = maxX - minX + 2 * PAD;
    const vbH = maxY - minY + 2 * PAD;
    svg.setAttribute("viewBox", `${{minX - PAD}} ${{minY - PAD}} ${{vbW}} ${{vbH}}`);
    // CRITICAL: set pixel dimensions matching the viewBox so the
    // iframe doesn't blow up to a screen-fill measurement.
    svg.setAttribute("width", vbW);
    svg.setAttribute("height", vbH);

    function el(tag, attrs) {{
      const e = document.createElementNS("http://www.w3.org/2000/svg", tag);
      for (const k in attrs) e.setAttribute(k, attrs[k]);
      return e;
    }}

    // Draw edges first so nodes paint on top.
    for (const e of laid.edges || []) {{
      const sec = e.sections[0];
      const pts = [sec.startPoint, ...(sec.bendPoints || []), sec.endPoint];
      const d = pts.map((p, i) => `${{i === 0 ? "M" : "L"}} ${{p.x}} ${{p.y}}`).join(" ");
      // Every paint attribute INLINE.
      svg.appendChild(el("path", {{
        d, fill: "none",
        stroke: PAINT.edge_stroke,
        "stroke-width": 1.5,
      }}));
    }}

    // Draw nodes.
    for (const n of laid.children) {{
      const g = el("g", {{ transform: `translate(${{n.x}}, ${{n.y}})` }});
      g.appendChild(el("rect", {{
        x: 0, y: 0, width: n.width, height: n.height, rx: 6,
        fill: PAINT.node_fill,
        stroke: PAINT.node_stroke,
        "stroke-width": 2,
      }}));
      const text = el("text", {{
        x: n.width / 2, y: n.height / 2,
        "text-anchor": "middle",
        "dominant-baseline": "central",
        "font-size": 14,
        "font-family": "system-ui, sans-serif",
        fill: PAINT.node_text,
      }});
      text.textContent = (n.labels && n.labels[0] && n.labels[0].text) || n.id;
      g.appendChild(text);
      svg.appendChild(g);
    }}
  }});
  </script>
</body>
</html>"""
```

## What you control

Everything. ELK lays out positions; you decide how each node and
edge is painted. Want diamond decisions? Append a `<polygon>` instead
of a `<rect>` for nodes whose data says so. Want arrowheads? Compute
the final segment direction, append a `<polygon>` at the tip. Want
gradient fills? Define `<linearGradient>` in `<defs>` (give each
unique id), reference via `fill="url(#g_node_a)"`.

See `../elk-design-templates.md` for three coordinated style families
(schematic, energy-monitor, hybrid) and standalone patterns (dashed
connectors, gradient fills, KPI cards, `<foreignObject>` HTML).

## ELK algorithm options

- `"elk.algorithm": "layered"` — Sugiyama-style hierarchical
  (default). Orthogonal routing, honors `elk.layered.*` options.
- `"elk.algorithm": "stress"` — force-directed organic layout
- `"elk.algorithm": "mrtree"` — pure tree (fastest)

Full ELK option reference: `rag_query corpus="code" query="elk
layered options"` — the full Eclipse ELK Java source is indexed.

## Port-side hints (layered only)

```python
node = {
    "id": "a", "width": 160, "height": 60,
    "ports": [
        {"id": "a_in",  "layoutOptions": {"port.side": "NORTH"}},
        {"id": "a_out", "layoutOptions": {"port.side": "SOUTH"}},
    ],
    "properties": {"portConstraints": "FIXED_SIDE"},
}
edge = {"id": "e1", "sources": ["a_out"], "targets": ["b_in"]}
```

## Settle time

elkjs runs synchronously after the bundle loads, but if your
report is large the layout can take a moment. The screenshot path
waits for `networkidle` + 1500ms, which covers all but huge graphs.
For >200-node graphs, raise `expand_settle_ms` on `screenshot_report`.

## Adding zoom / pan / fit-all controls

Zoom/pan/fit-all buttons work fine **as long as you keep body at
natural height and read `window.innerWidth/Height` for fit
calculations**. Using `100vh` + `viewport.clientHeight` breaks
screenshots (see [screenshot-friendly.md](../screenshot-friendly.md)
for the full explanation and summary table).

Drop this pattern in after the `elk.layout().then(...)` block:

```html
<body style="margin:0; padding:16px; background:#0b0f14">
  <!-- position:relative wrapper lets the toolbar use position:absolute -->
  <div id="wrap" style="position:relative; display:inline-block">
    <svg id="diagram"></svg>
    <!-- position:absolute (NOT fixed) — moves with content during probe,
         appears in screenshots at the correct position -->
    <div id="toolbar" style="
      position:absolute; top:10px; right:10px; z-index:10;
      display:flex; gap:4px">
      <button onclick="zoom(1.2)">+</button>
      <button onclick="zoom(1/1.2)">−</button>
      <!-- fit-all icon: dashed square ⬚ (U+2B1A) -->
      <button onclick="fitAll()" title="Fit all">⬚</button>
    </div>
  </div>
</body>
```

```js
let scale = 1, tx = 0, ty = 0, dgW, dgH;
const svg = document.getElementById("diagram");

function applyTransform() {
  svg.style.transform = `translate(${tx}px,${ty}px) scale(${scale})`;
  svg.style.transformOrigin = "0 0";
}
function zoom(factor) { scale *= factor; applyTransform(); }
function fitAll() {
  // Use window.innerWidth/Height — NOT clientHeight of any container.
  // clientHeight would read the probe height (~8000px) and scale the
  // diagram down to a thumbnail in the screenshot.
  scale = Math.min(window.innerWidth / dgW, window.innerHeight / dgH) * 0.92;
  tx = (window.innerWidth - dgW * scale) / 2;
  ty = 20;
  applyTransform();
}

elk.layout(graph).then(laid => {
  // ... compute vbW, vbH from extent, paint edges + nodes ...
  dgW = vbW; dgH = vbH;
  svg.setAttribute("width", vbW);
  svg.setAttribute("height", vbH);
  fitAll();  // synchronous — no requestAnimationFrame needed
});
```

**Key rules:**
- `position: absolute` on the toolbar (never `position: fixed`)
- `window.innerWidth/Height` for fit scale (never `container.clientHeight`)
- Call `fitAll()` directly in the `.then()` callback — no `rAF` delay needed
- Fit-all button icon: dashed square **⬚** (`U+2B1A`) or an inline SVG
  `<rect stroke-dasharray="3 2">` for more control over appearance

## Common screenshot failure modes

| Symptom | Cause | Fix |
|---|---|---|
| All nodes black, no text visible | `<style>` block holds paint; html-to-image dropped it | Set fill/stroke/font as inline attrs |
| Screenshot is 2000px+ tall but live render is short | SVG `width=100% height=100%` blew up the iframe measurement | Set explicit pixel `width`/`height` on `<svg>` matching the viewBox extent |
| Diagram is tiny thumbnail in top-left of huge canvas | `fitAll()` read `clientHeight` during 8000 px probe | Use `window.innerHeight` instead; keep body at natural height |
| Text is the wrong font | Cross-origin web font failed to inline | Use `font-family: system-ui` or self-host the WOFF2 |
| 3D canvas inside `<foreignObject>` is blank | three.js renderer without `preserveDrawingBuffer: true` | Pass that flag to `new THREE.WebGLRenderer({...})` |
