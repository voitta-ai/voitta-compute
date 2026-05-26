# ELK design templates

Three coordinated style families plus standalone patterns. Each
template returns an **SVG fragment** (a Python string). You splice
it into your HTML report's ELK-rendering JavaScript or into a
static SVG document.

These are the same Svenya skins ported to the strip-everything
architecture. Pair this doc with `recipes/elk.md` for the full
ELK-in-HTML-report pattern (load elkjs from CDN, lay out, paint).

Color palettes here are illustrative. Swap them for `ctx.theme()`
values to match the host theme.

## How to use a template

Each helper takes `width` + `height` and returns an SVG fragment
in **local coordinates** (0,0 to width, height). Inside the ELK
rendering loop, you wrap with a `<g transform="translate(x, y)">`
based on ELK's laid-out position:

```python
# In your build(ctx):
NODES_PY = {
  "src": (180, 80, schematic_module_svg(180, 80, "Source")),
  "inv": (180, 80, schematic_module_svg(180, 80, "Inverter", cool=True)),
  ...
}

graph = {
    "id": "root", "layoutOptions": {...},
    "children": [
        {"id": nid, "width": w, "height": h}
        for nid, (w, h, _svg) in NODES_PY.items()
    ],
    "edges": [...],
}
node_svg_by_id_js = json.dumps({nid: svg for nid, (_, _, svg) in NODES_PY.items()})

# In the iframe JS (after elk.layout().then(...)):
return f"""<!doctype html><html>
<head><script src="https://unpkg.com/elkjs@0.11.1/lib/elk.bundled.js"></script></head>
<body>
<svg id="diag"></svg>
<script>
const NODE_SVG = {node_svg_by_id_js};
const graph = {json.dumps(graph)};
new ELK().layout(graph).then(laid => {{
  const svg = document.getElementById("diag");
  // ...edge rendering (see recipes/elk.md)...
  for (const n of laid.children) {{
    const g = document.createElementNS("http://www.w3.org/2000/svg", "g");
    g.setAttribute("transform", `translate(${{n.x}}, ${{n.y}})`);
    g.innerHTML = NODE_SVG[n.id];
    svg.appendChild(g);
  }}
}});
</script>
</body></html>"""
```

---

## Style 1: SCHEMATIC

Bold drop-shadow rounded boxes. Bold uppercase titles. Slate
canvas pairs well — set `body { background: #5a6878 }` in your
report's CSS for full effect.

```python
SCHEMATIC_FLOW = {
    "primary":    "#5fd0e0",
    "storage":    "#f1d24a",
    "mechanical": "#e23b3b",
    "sensor":     "#7bc96f",
    "feedback":   "#2a55d6",
    "command":    "#f0a8c0",
}

def schematic_module_svg(w, h, title, *, cool=False):
    """Cream / cool-blue rounded box, hard shadow, bold uppercase title."""
    fill = "#c9e2f3" if cool else "#f7e8c8"
    return f"""
      <rect x="4" y="4" width="{w}" height="{h}" rx="10"
            fill="rgba(0,0,0,0.35)"/>
      <rect x="0" y="0" width="{w}" height="{h}" rx="10"
            fill="{fill}" stroke="#2b3140" stroke-width="2.5"/>
      <text x="{w/2}" y="{h/2}" text-anchor="middle"
            dominant-baseline="central" font-size="16"
            font-weight="800" style="letter-spacing:0.6"
            fill="#0b1320">{title.upper()}</text>
    """

def schematic_hub_svg(w=38, h=38):
    """Small dark circular junction. Edges multi-fan from these."""
    return f"""
      <circle cx="{w/2}" cy="{h/2}" r="{min(w, h)/2 - 2}"
              fill="#2b3140" stroke="#1a2230" stroke-width="2.5"/>
    """

def schematic_decision_svg(w, h, condition, *, cool=False):
    """Diamond with the condition text centered."""
    fill = "#c9e2f3" if cool else "#f1d24a"
    return f"""
      <polygon points="{w/2},4 {w-4},{h/2} {w/2},{h-4} 4,{h/2}"
               fill="rgba(0,0,0,0.35)" transform="translate(2 2)"/>
      <polygon points="{w/2},0 {w},{h/2} {w/2},{h} 0,{h/2}"
               fill="{fill}" stroke="#2b3140" stroke-width="2.5"/>
      <text x="{w/2}" y="{h/2}" text-anchor="middle"
            dominant-baseline="central" font-size="13"
            font-weight="800" fill="#0b1320">{condition}</text>
    """
```

For edges in this style: thick colored stroke (8-10px), polygon
arrowhead in the same color. In your elkjs render loop:

```js
function paintSchematicEdge(svg, edge, color) {
  const sec = edge.sections[0];
  const pts = [sec.startPoint, ...(sec.bendPoints || []), sec.endPoint];
  const d = pts.map((p, i) => `${i === 0 ? "M" : "L"} ${p.x} ${p.y}`).join(" ");
  const p = document.createElementNS("http://www.w3.org/2000/svg", "path");
  p.setAttribute("d", d);
  p.setAttribute("fill", "none");
  p.setAttribute("stroke", color);
  p.setAttribute("stroke-width", "10");
  p.setAttribute("stroke-linecap", "butt");
  p.setAttribute("stroke-linejoin", "round");
  svg.appendChild(p);
  // Arrowhead at endpoint
  const tip = sec.endPoint, prev = sec.bendPoints?.slice(-1)[0] || sec.startPoint;
  const dx = tip.x - prev.x, dy = tip.y - prev.y, L = Math.hypot(dx, dy) || 1;
  const ux = dx / L, uy = dy / L, SIZE = 14;
  const base = { x: tip.x - ux * SIZE, y: tip.y - uy * SIZE };
  const px = -uy, py = ux;
  const left  = { x: base.x + px * SIZE/2, y: base.y + py * SIZE/2 };
  const right = { x: base.x - px * SIZE/2, y: base.y - py * SIZE/2 };
  const poly = document.createElementNS("http://www.w3.org/2000/svg", "polygon");
  poly.setAttribute("points", `${tip.x},${tip.y} ${left.x},${left.y} ${right.x},${right.y}`);
  poly.setAttribute("fill", color);
  svg.appendChild(poly);
}
```

---

## Style 2: ENERGY-MONITOR

Dark dashboard. Off-white panels with dark title bands. Thin neon
edges. Control-room aesthetic. Pairs with `body { background: #0b0f14 }`.

> **See [`recipes/elk-energy-monitor.md`](recipes/elk-energy-monitor.md)
> for the full proven recipe** — animated marching-dash edges,
> per-type SVG glyphs (gear/battery/lightning/sine wave/chip/leaf),
> grid-wrapped layout via `MULTI_EDGE` strategy + `aspectRatio`.
> The helpers below are the same Svenya-style node SVGs you'd drop
> into that recipe's NODE_SVG dict.

```python
EM_FLOW = {
    "primary":    "#ef3a25",
    "storage":    "#f7d54a",
    "mechanical": "#f59021",
    "sensor":     "#3ad36a",
    "feedback":   "#5fd0e0",
    "command":    "#a780ff",
}

def em_module_svg(w, h, title, *, cool=False, glyph=None):
    """Pale rounded body, dark title band, optional glyph below."""
    fill = "#cfe1ef" if cool else "#e8edf2"
    band_h = 22
    glyph_svg = _em_glyph(glyph, cx=w/2, cy=band_h + (h - band_h)/2)
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
      {glyph_svg}
    """

def _em_glyph(kind, *, cx, cy):
    if kind == "battery":
        cells = "".join(
            f'<rect x="{2 + (i%5)*26}" y="{(i//5)*24}" '
            f'width="22" height="20" rx="2" '
            f'fill="#7dc6ee" stroke="#0e3a52" stroke-width="1.2"/>'
            for i in range(10)
        )
        return f'<g transform="translate({cx - 65},{cy - 22})">{cells}</g>'
    if kind == "gear":
        import math
        teeth = "".join(
            f'<line x1="{math.cos(i*math.pi/4)*18}" '
            f'y1="{math.sin(i*math.pi/4)*18}" '
            f'x2="{math.cos(i*math.pi/4)*24}" '
            f'y2="{math.sin(i*math.pi/4)*24}" '
            f'stroke="#1a2230" stroke-width="3" stroke-linecap="round"/>'
            for i in range(8)
        )
        return f'''<g transform="translate({cx},{cy})">
          <circle r="18" fill="#cdd6e0" stroke="#1a2230" stroke-width="2"/>
          {teeth}
          <circle r="5" fill="#1a2230"/>
        </g>'''
    if kind == "cylinder":
        return f'''<g transform="translate({cx-30},{cy-24})">
          <rect x="0" y="4" width="60" height="36" rx="14"
                fill="#cfd4da" stroke="#1a2230" stroke-width="2"/>
          <ellipse cx="30" cy="10" rx="28" ry="5"
                   fill="#eef1f4" stroke="#1a2230" stroke-width="1.2"/>
          <circle cx="30" cy="10" r="3" fill="#1a2230"/>
          <line x1="6" y1="22" x2="54" y2="22"
                stroke="#1a2230" stroke-opacity="0.45" stroke-width="1.5"/>
          <line x1="6" y1="30" x2="54" y2="30"
                stroke="#1a2230" stroke-opacity="0.45" stroke-width="1.5"/>
        </g>'''
    if kind == "grid":
        bars = "".join(
            f'<rect x="{i*20}" y="0" width="16" height="44" '
            f'fill="#7e8590" stroke="#1a2230" stroke-width="1"/>'
            for i in range(5)
        )
        return f'<g transform="translate({cx-50},{cy-22})">{bars}</g>'
    if kind == "wave":
        return (f'<polyline transform="translate({cx},{cy})" '
                'points="-50,0 -36,-12 -22,0 -8,12 6,0 20,-12 34,0 48,12" '
                'fill="none" stroke="#1a2230" stroke-width="2.5"/>')
    return ""
```

Edges: thin colored stroke (2-3px). Use `stroke-dasharray="8 4"`
for visual rhythm. Polygon arrowhead at endpoint.

---

## Style 3: HYBRID

Deep cyan canvas (`#15546d`). Pastel boxes with dark title bands.
Thick colored edges. Good middle ground.

```python
HYBRID_FLOW = {
    "primary":    "#e84a2d",
    "storage":    "#f4d543",
    "mechanical": "#f5871f",
    "sensor":     "#5dc466",
    "feedback":   "#7adef0",
    "command":    "#e84a8c",
}

def hybrid_module_svg(w, h, title, *, cool=False):
    """Pastel box with dark title band, drop shadow."""
    fill = "#b8d2e0" if cool else "#c8e0ec"
    band_h = 22
    return f"""
      <rect x="3" y="3" width="{w}" height="{h}" rx="6"
            fill="rgba(0,0,0,0.45)"/>
      <rect x="0" y="0" width="{w}" height="{h}" rx="6"
            fill="{fill}" stroke="#0a2535" stroke-width="2"/>
      <rect x="0" y="0" width="{w}" height="{band_h}" rx="6"
            fill="#0a2535"/>
      <rect x="0" y="{band_h-6}" width="{w}" height="6" fill="#0a2535"/>
      <text x="{w/2}" y="{band_h/2 + 4}" text-anchor="middle"
            font-size="11" font-weight="700"
            style="letter-spacing:0.8" fill="#fff">{title.upper()}</text>
    """
```

Edges: medium-thick colored stroke (5px). Polygon arrowhead in
matching color.

---

## Standalone patterns

### Multi-line label (per-tspan styling)

```python
def multiline_label_svg(w, h, lines, *,
                        font_size=13, line_gap=4,
                        fill="#0b1320", weight=400):
    line_h = font_size + line_gap
    total = (len(lines) - 1) * line_h
    first_dy = -total / 2
    tspans = "".join(
        f'<tspan x="{w/2}" dy="{first_dy if i == 0 else line_h}">{ln}</tspan>'
        for i, ln in enumerate(lines)
    )
    return f"""
      <rect x="0" y="0" width="{w}" height="{h}" rx="6"
            fill="#fff" stroke="#1a2230" stroke-width="1.5"/>
      <text x="{w/2}" y="{h/2}" text-anchor="middle"
            dominant-baseline="central"
            font-size="{font_size}" font-weight="{weight}"
            fill="{fill}">{tspans}</text>
    """
```

### Gradient fill

```python
def gradient_node_svg(w, h, title, *, gradient_id, top_color, bottom_color):
    return f"""
      <defs>
        <linearGradient id="g_{gradient_id}" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%"  stop-color="{top_color}"/>
          <stop offset="100%" stop-color="{bottom_color}"/>
        </linearGradient>
      </defs>
      <rect x="0" y="0" width="{w}" height="{h}" rx="8"
            fill="url(#g_{gradient_id})" stroke="#0a2535" stroke-width="2"/>
      <text x="{w/2}" y="{h/2}" text-anchor="middle"
            dominant-baseline="central" font-size="14"
            font-weight="700" fill="#fff">{title}</text>
    """
```

Gradient IDs must be UNIQUE per node — pass the node id as
`gradient_id` to avoid collisions.

### KPI card

```python
def kpi_node_svg(w, h, label, value, *, color="#3ad36a"):
    return f"""
      <rect x="0" y="0" width="{w}" height="{h}" rx="8"
            fill="#fff" stroke="#1a2230" stroke-width="1.5"/>
      <text x="{w/2}" y="{h*0.35}" text-anchor="middle"
            font-size="10" font-weight="600"
            style="letter-spacing:0.8" fill="#666">
        {label.upper()}
      </text>
      <text x="{w/2}" y="{h*0.7}" text-anchor="middle"
            font-size="22" font-weight="800" fill="{color}">
        {value}
      </text>
    """
```

### Themed node (using ctx.theme() vars)

```python
def themed_module_svg(w, h, title, ctx):
    t = ctx.theme()
    surf = t.get("--voitta-surface", "#fff")
    text = t.get("--voitta-text", "#0b1320")
    bord = t.get("--voitta-border", "#1a2230")
    return f"""
      <rect x="0" y="0" width="{w}" height="{h}" rx="6"
            fill="{surf}" stroke="{bord}" stroke-width="1.5"/>
      <text x="{w/2}" y="{h/2}" text-anchor="middle"
            dominant-baseline="central" font-size="14"
            fill="{text}">{title}</text>
    """
```

### foreignObject — full HTML inside a node

```python
def html_node_svg(w, h, html_inner):
    return f'''
      <foreignObject x="0" y="0" width="{w}" height="{h}">
        <div xmlns="http://www.w3.org/1999/xhtml"
             style="width:{w}px;height:{h}px;
                    display:flex;align-items:center;
                    justify-content:center;
                    font-family:system-ui;background:#fff;
                    border:1.5px solid #1a2230;
                    border-radius:6px;padding:8px;
                    box-sizing:border-box">
          {html_inner}
        </div>
      </foreignObject>
    '''
```

### Dashed connector

In the elkjs render loop, add `stroke-dasharray` to the path:

```js
path.setAttribute("stroke-dasharray", "8 4");  // 8px dash, 4px gap
```

Common patterns:
- `"4 4"` — tight dots
- `"12 4"` — long dashes
- `"2 6"` — sparse dots
- `"10 4 2 4"` — dash-dot

---

## Zoom / pan / fit-all toolbar

When pairing any template with interactive controls, follow the
rules in [`recipes/elk.md` — "Adding zoom / pan / fit-all controls"](recipes/elk.md):

- Toolbar must use `position: absolute` inside a `position: relative`
  wrapper — **not `position: fixed`** (fixed jumps to wrong positions
  during the screenshot probe).
- Fit-all must read `window.innerWidth/Height`, not any container's
  `.clientHeight`.
- Fit-all button icon: **⬚** (dashed square, U+2B1A).

---

## What this doc deliberately doesn't include

- **Animated edges** (Hybrid's flowing-blocks effect): possible
  via SMIL `<animateMotion>` or CSS animations in the iframe.
  Animations are mid-frame at screenshot time — design for a
  stable end-frame if you care what's captured.
- **Custom edge routing**: ELK lays edges out. You can't override
  the polyline path from your script. Use port-side hints
  (`port.side: "NORTH"|"SOUTH"|"EAST"|"WEST"`) to nudge ELK.
- **Reactive content**: SVG fragments are static. For
  interactivity, return HTML with widgets — see
  `recipes/interactivity.md`.

For screenshot-friendly rules (cross-origin fonts inside
`<foreignObject>`, etc.), see `screenshot-friendly.md`.
