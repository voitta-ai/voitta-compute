# Recipe: ELK energy-monitor — animated flow

Dark control-room aesthetic. Pale-panel nodes with dark title bands
+ per-type SVG glyphs. Marching-dash edges (CSS `@keyframes`
animation on `stroke-dashoffset`).

Tested & proven for screenshot capture and live viewing.

## Stack

- `elkjs` from CDN — layout
- Pure SVG — all rendering, no canvas, no external images
- CSS `@keyframes` — dash animation on edges
- INLINE SVG attributes (never CSS classes) — html-to-image
  doesn't inline `<style>` rules into its snapshot

## Full pattern

```python
import json, math, random

EM_FLOW = {
    "primary":    "#ef3a25",  # red    — electrical
    "storage":    "#f7d54a",  # yellow — battery/tank
    "mechanical": "#f59021",  # orange — rotating parts
    "sensor":     "#3ad36a",  # green  — sensors
    "feedback":   "#5fd0e0",  # cyan   — loops/cooling
    "command":    "#a780ff",  # purple — control units
}


def em_module_svg(w, h, title, *, cool=False, flow_type=None):
    """Pale rounded body, dark title band, per-type inline illustration."""
    fill = "#cfe1ef" if cool else "#e8edf2"
    band_h = 22
    illus = _em_illustration(flow_type,
                              cx=w / 2,
                              cy=band_h + (h - band_h) / 2,
                              s=14)
    return f"""
      <rect x="2" y="2" width="{w}" height="{h}" rx="10"
            fill="rgba(0,0,0,0.5)"/>
      <rect x="0" y="0" width="{w}" height="{h}" rx="10"
            fill="{fill}" stroke="#1a2230" stroke-width="2.5"/>
      <path d="M1,{band_h} L1,10 Q1,1 10,1 L{w-10},1
               Q{w-1},1 {w-1},10 L{w-1},{band_h} Z" fill="#2f3a4a"/>
      <text x="{w/2}" y="{band_h/2 + 4}" text-anchor="middle"
            font-size="11" font-weight="700"
            style="letter-spacing:1" fill="#fff">{title.upper()}</text>
      {illus}
    """


def em_hub_svg(w=38, h=38):
    """Cyan-rimmed dark dot for one-to-many routing junctions."""
    return f"""
      <circle cx="{w/2}" cy="{h/2}" r="{min(w, h)/2 - 2}"
              fill="#2f3a4a" stroke="#5fd0e0" stroke-width="2"/>
      <circle cx="{w/2}" cy="{h/2}" r="4" fill="#5fd0e0"/>
    """


def _em_illustration(kind, *, cx, cy, s=14):
    """Per-flow-type glyph centred at (cx, cy). Pure SVG primitives."""
    if kind == "mechanical":
        # 8-tooth gear, orange
        teeth = "".join(
            f'<line x1="{math.cos(i*math.pi/4)*s}" '
            f'y1="{math.sin(i*math.pi/4)*s}" '
            f'x2="{math.cos(i*math.pi/4)*(s+5)}" '
            f'y2="{math.sin(i*math.pi/4)*(s+5)}" '
            f'stroke="#f59021" stroke-width="2.5" stroke-linecap="round"/>'
            for i in range(8)
        )
        return f'''<g transform="translate({cx},{cy})">
          <circle r="{s}" fill="none" stroke="#f59021" stroke-width="2"/>
          {teeth}
          <circle r="4" fill="#f59021"/>
        </g>'''
    if kind == "storage":
        # 3-cell battery, yellow
        cell_w = s; cell_h = s * 1.4
        cells = "".join(
            f'<rect x="{(i - 1) * (cell_w + 2)}" y="{-cell_h/2}" '
            f'width="{cell_w}" height="{cell_h}" rx="2" '
            f'fill="#f7d54a" stroke="#1a2230" stroke-width="1.5"/>'
            for i in range(3)
        )
        return f'<g transform="translate({cx},{cy})">{cells}</g>'
    if kind == "primary":
        # Lightning bolt, cyan
        return (f'<polygon transform="translate({cx},{cy})" '
                f'points="-{s/2},-{s} {s/4},-{s/4} -{s/4},-{s/4} '
                f'{s/2},{s} -{s/4},{s/4} {s/4},{s/4}" '
                f'fill="#5fd0e0" stroke="#1a2230" stroke-width="1"/>')
    if kind == "feedback":
        # Sine wave, cyan
        pts = []
        for i in range(13):
            t = i / 12
            x = -s + 2 * s * t
            y = -s / 2 * math.sin(t * 2 * math.pi)
            pts.append(f"{x:.1f},{y:.1f}")
        return (f'<polyline transform="translate({cx},{cy})" '
                f'points="{" ".join(pts)}" '
                f'fill="none" stroke="#5fd0e0" stroke-width="2.5"/>')
    if kind == "command":
        # Chip with pins, purple
        pins = "".join(
            f'<line x1="{-s + 2 * s * i / 4}" y1="{-s}" '
            f'x2="{-s + 2 * s * i / 4}" y2="{-s - 4}" '
            f'stroke="#a780ff" stroke-width="2"/>'
            for i in range(5)
        ) + "".join(
            f'<line x1="{-s + 2 * s * i / 4}" y1="{s}" '
            f'x2="{-s + 2 * s * i / 4}" y2="{s + 4}" '
            f'stroke="#a780ff" stroke-width="2"/>'
            for i in range(5)
        )
        return f'''<g transform="translate({cx},{cy})">
          <rect x="-{s}" y="-{s*0.7}" width="{2*s}" height="{1.4*s}" rx="2"
                fill="#2f3a4a" stroke="#a780ff" stroke-width="2"/>
          {pins}
        </g>'''
    if kind == "sensor":
        # Leaf, green
        return f'''<g transform="translate({cx},{cy})">
          <path d="M -{s} 0 Q -{s/2} -{s} 0 -{s} Q {s} -{s/2} {s} 0
                   Q {s/2} {s} 0 {s} Q -{s} {s/2} -{s} 0 Z"
                fill="#3ad36a" stroke="#1a2230" stroke-width="1.5"/>
          <line x1="-{s}" y1="0" x2="{s}" y2="0"
                stroke="#1a2230" stroke-width="1.5"/>
        </g>'''
    return ""


def build(ctx):
    # 1. Define nodes — (id, title, flow_type, cool?)
    node_defs = [
        ("eng",  "Engine",     "mechanical", False),
        ("bat",  "Battery",    "storage",    False),
        ("inv",  "Inverter",   "primary",    True),
        ("mot",  "Motor",      "mechanical", False),
        ("bms",  "BMS",        "command",    True),
        ("ecu",  "ECU",        "command",    True),
        ("rad",  "Radiator",   "feedback",   False),
        ("sen",  "Throttle",   "sensor",     False),
    ]
    nodes = {}
    children = []
    for nid, title, ftype, cool in node_defs:
        w, h = 180, 70
        svg = em_module_svg(w, h, title, cool=cool, flow_type=ftype)
        nodes[nid] = (w, h, svg)
        children.append({"id": nid, "width": w, "height": h})

    # 2. Edges — (src, tgt, flow_type)
    edge_defs = [
        ("eng", "mot", "mechanical"),
        ("bat", "inv", "storage"),
        ("inv", "mot", "primary"),
        ("bat", "bms", "command"),
        ("bms", "inv", "command"),
        ("ecu", "inv", "command"),
        ("sen", "ecu", "sensor"),
        ("mot", "rad", "feedback"),
        ("rad", "eng", "feedback"),
    ]
    edges = []
    edge_colors = {}
    for i, (src, tgt, ftype) in enumerate(edge_defs):
        eid = f"e{i}"
        edges.append({"id": eid, "sources": [src], "targets": [tgt]})
        edge_colors[eid] = EM_FLOW.get(ftype, EM_FLOW["primary"])

    graph = {
        "id": "root",
        "layoutOptions": {
            "elk.algorithm": "layered",
            "elk.direction": "DOWN",
            "elk.edgeRouting": "ORTHOGONAL",
            "elk.spacing.nodeNode": "40",
            "elk.layered.spacing.nodeNodeBetweenLayers": "60",
            # Wraps long chains into a grid instead of a 1×N column
            "elk.layered.wrapping.strategy": "MULTI_EDGE",
            "elk.aspectRatio": "1.6",
        },
        "children": children,
        "edges": edges,
    }

    node_svg_js   = json.dumps({nid: svg for nid, (_, _, svg) in nodes.items()})
    edge_colors_js = json.dumps(edge_colors)
    graph_js       = json.dumps(graph)

    return f"""<!doctype html>
<html>
<head>
  <script src="https://unpkg.com/elkjs@0.11.1/lib/elk.bundled.js"></script>
  <style>
    /* IMPORTANT: NO `min-height: 100vh` / `height: 100%` cascades.
       The screenshot path resizes the iframe to ~8000px during the
       height probe; viewport-sized elements grow to fill it and the
       screenshot ends up giant. Use natural body height. */
    html, body {{
      margin: 0; padding: 16px;
      background: #0b0f14;
      font-family: system-ui, sans-serif;
    }}
    /* Marching-dash animation. -24 = 2× (dash + gap) = one seamless
       loop. Edges set their own per-stroke duration inline for
       organic, slightly desync'd feel. */
    @keyframes dash-march {{
      to {{ stroke-dashoffset: -24; }}
    }}
    /* SVG element styling lives INLINE on each <rect>/<text>/<path>
       in the JS below — html-to-image doesn't inline <style> rules
       into its snapshot. */
  </style>
</head>
<body>
<svg id="diagram" xmlns="http://www.w3.org/2000/svg"
     preserveAspectRatio="xMidYMid meet"></svg>
<script>
const NODE_SVG    = {node_svg_js};
const EDGE_COLORS = {edge_colors_js};
const graph       = {graph_js};

new ELK().layout(graph).then(laid => {{
  const svg = document.getElementById("diagram");

  // 3. Fit canvas: viewBox covers all content + padding; pixel size
  //    matches the viewBox so the iframe doesn't blow up to 100vh.
  const PAD = 40;
  const vbW = laid.width + PAD * 2;
  const vbH = laid.height + PAD * 2;
  svg.setAttribute("viewBox", `-${{PAD}} -${{PAD}} ${{vbW}} ${{vbH}}`);
  svg.setAttribute("width",  vbW);
  svg.setAttribute("height", vbH);

  function el(tag, attrs) {{
    const e = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const k in attrs) e.setAttribute(k, attrs[k]);
    return e;
  }}

  // 4. Paint edges (behind nodes)
  for (const edge of laid.edges) {{
    const sec = edge.sections[0];
    const pts = [sec.startPoint, ...(sec.bendPoints || []), sec.endPoint];
    const d = pts.map((p, i) => `${{i === 0 ? "M" : "L"}} ${{p.x}} ${{p.y}}`).join(" ");
    const color = EDGE_COLORS[edge.id] || "#5fd0e0";

    const path = el("path", {{
      d, fill: "none", stroke: color,
      "stroke-width": "2.5",
      "stroke-dasharray": "8 4",
      "stroke-linecap": "round",
    }});
    // Randomise per-edge for organic flow
    const dur = (0.8 + Math.random() * 0.8).toFixed(2);
    path.style.animation = `dash-march ${{dur}}s linear infinite`;
    svg.appendChild(path);

    // Arrowhead at endpoint, in matching colour
    const tip  = sec.endPoint;
    const prev = sec.bendPoints?.slice(-1)[0] || sec.startPoint;
    const dx = tip.x - prev.x, dy = tip.y - prev.y;
    const L = Math.hypot(dx, dy) || 1;
    const ux = dx / L, uy = dy / L, SIZE = 14;
    const base  = {{ x: tip.x - ux * SIZE,         y: tip.y - uy * SIZE }};
    const left  = {{ x: base.x - uy * SIZE/2,      y: base.y + ux * SIZE/2 }};
    const right = {{ x: base.x + uy * SIZE/2,      y: base.y - ux * SIZE/2 }};
    svg.appendChild(el("polygon", {{
      points: `${{tip.x}},${{tip.y}} ${{left.x}},${{left.y}} ${{right.x}},${{right.y}}`,
      fill: color,
    }}));
  }}

  // 5. Paint nodes (on top)
  for (const node of laid.children) {{
    const g = el("g", {{ transform: `translate(${{node.x}}, ${{node.y}})` }});
    g.innerHTML = NODE_SVG[node.id];
    svg.appendChild(g);
  }}
}});
</script>
</body>
</html>"""
```

## Key rules & gotchas

| Rule | Why |
|---|---|
| All SVG styling via inline attributes, not CSS classes | html-to-image doesn't reliably inline `<style>` rules into its SVG snapshot — class-styled elements paint as black boxes |
| Animation via `stroke-dashoffset` keyframe, not `getTotalLength()` | Simpler, no async timing; offset by -(dash + gap) = one seamless loop |
| Node SVGs are static strings serialised with `json.dumps` into JS | Avoids escaping nightmares; JS receives a plain dict |
| `elk.layered.wrapping.strategy: "MULTI_EDGE"` + `elk.aspectRatio: 1.6` | Prevents a single long horizontal/vertical chain; wraps into a grid-like layout |
| Edges painted **before** nodes | Nodes sit on top; edges don't clip node bodies |
| `preserveAspectRatio="xMidYMid meet"` + explicit width/height matching viewBox | Centres and fits diagram cleanly without distortion or iframe-size blowup |
| `body { margin: 0; padding: 16px }` — NO `100vh` / `height: 100%` | Screenshot path resizes iframe to 8000 px during height probe; viewport-sized bodies fill it → giant capture |

## Screenshot notes

- Animation is captured mid-frame. Each edge's dash position at
  screenshot time is whatever it was at that millisecond. The
  visual is fine — dashed lines look "snapshot during motion"
  in both live and screenshot views.
- If you want a stable end-frame for documentation, drop the
  `animation:` line and the dash sits still.

## Variations

### Static (no animation)
Remove the `animation` style and the `@keyframes`. Same look,
no motion.

### Vary dash pattern per flow type
```js
// Long dashes for power, short for sensor
const dash = flow_type === "primary" ? "12 4" : "6 4";
path.setAttribute("stroke-dasharray", dash);
```

### Glow effect on edges
```js
// Stack two paths: blurred wide stroke under crisp narrow stroke
path_glow.setAttribute("stroke-width", "8");
path_glow.setAttribute("opacity", "0.4");
path_glow.setAttribute("filter", "url(#blur)");
// + <defs><filter id="blur"><feGaussianBlur stdDeviation="3"/></filter></defs>
```

### Pulse hubs
```css
@keyframes pulse { 50% { opacity: 0.5 } }
/* on the hub circle, inline: */
style="animation: pulse 1.2s ease-in-out infinite"
```
