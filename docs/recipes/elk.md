# Recipe: ELK diagram via CDN

ELK (Eclipse Layout Kernel) computes graph layouts. You call its JS API to lay out nodes and edges, then paint the result as SVG.

## Basic pattern

```python
import json

def build(ctx):
    # Define graph structure for ELK
    graph = {
        "id": "root",
        "layoutOptions": {
            "elk.algorithm": "layered",
            "elk.direction": "RIGHT",
            "elk.spacing.nodeNode": "40",
        },
        "children": [
            {"id": "n1", "width": 100, "height": 40, "labels": [{"text": "Input"}]},
            {"id": "n2", "width": 100, "height": 40, "labels": [{"text": "Process"}]},
            {"id": "n3", "width": 100, "height": 40, "labels": [{"text": "Output"}]},
        ],
        "edges": [
            {"id": "e1", "sources": ["n1"], "targets": ["n2"]},
            {"id": "e2", "sources": ["n2"], "targets": ["n3"]},
        ],
    }

    t = ctx.theme()
    bg     = t.get("--voitta-bg",     "#ffffff")
    text   = t.get("--voitta-text",   "#111111")
    accent = t.get("--voitta-accent", "#5b5fc7")

    return f"""<!DOCTYPE html>
<html>
<head>
<style>
  body {{ margin: 0; padding: 16px; background: {bg}; }}
  svg {{ width: 100%; overflow: visible; }}
  .node rect {{ fill: {accent}; rx: 6; ry: 6; }}
  .node text {{ fill: #fff; font-size: 13px; font-family: sans-serif; dominant-baseline: middle; text-anchor: middle; }}
  .edge path {{ stroke: {text}; stroke-width: 2; fill: none; marker-end: url(#arrow); }}
</style>
</head>
<body>
<svg id="svg"><defs>
  <marker id="arrow" markerWidth="10" markerHeight="7" refX="9" refY="3.5" orient="auto">
    <polygon points="0 0, 10 3.5, 0 7" fill="{text}"/>
  </marker>
</defs></svg>
<script src="https://cdn.jsdelivr.net/npm/elkjs@0.9.0/lib/elk.bundled.js"></script>
<script>
const elk = new ELK();
const graph = {json.dumps(graph)};

elk.layout(graph).then(g => {{
  const svg = document.getElementById('svg');
  const PAD = 20;
  // Compute bounds
  let maxX = 0, maxY = 0;
  g.children.forEach(n => {{
    maxX = Math.max(maxX, n.x + n.width);
    maxY = Math.max(maxY, n.y + n.height);
  }});
  svg.setAttribute('viewBox', `${{-PAD}} ${{-PAD}} ${{maxX + PAD*2}} ${{maxY + PAD*2}}`);
  svg.setAttribute('height', maxY + PAD*2);

  // Draw edges
  g.edges.forEach(edge => {{
    const pts = edge.sections?.[0];
    if (!pts) return;
    const start = pts.startPoint;
    const end = pts.endPoint;
    const bends = pts.bendPoints || [];
    let d = `M ${{start.x}} ${{start.y}}`;
    bends.forEach(p => d += ` L ${{p.x}} ${{p.y}}`);
    d += ` L ${{end.x}} ${{end.y}}`;
    const el = document.createElementNS('http://www.w3.org/2000/svg', 'g');
    el.setAttribute('class', 'edge');
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', d);
    el.appendChild(path);
    svg.appendChild(el);
  }});

  // Draw nodes
  g.children.forEach(node => {{
    const g_el = document.createElementNS('http://www.w3.org/2000/svg', 'g');
    g_el.setAttribute('class', 'node');
    const rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
    rect.setAttribute('x', node.x); rect.setAttribute('y', node.y);
    rect.setAttribute('width', node.width); rect.setAttribute('height', node.height);
    rect.setAttribute('rx', 6);
    const label = node.labels?.[0]?.text || node.id;
    const text_el = document.createElementNS('http://www.w3.org/2000/svg', 'text');
    text_el.setAttribute('x', node.x + node.width/2);
    text_el.setAttribute('y', node.y + node.height/2);
    text_el.textContent = label;
    g_el.appendChild(rect);
    g_el.appendChild(text_el);
    svg.appendChild(g_el);
  }});
}});
</script>
</body></html>"""
```

## ELK layout algorithms

```javascript
// Layered (default) — good for DAGs, pipelines
"elk.algorithm": "layered"

// Force-directed — good for general graphs
"elk.algorithm": "force"

// Tree — good for hierarchies
"elk.algorithm": "mrtree"

// Radial
"elk.algorithm": "radial"
```

## Direction

```javascript
"elk.direction": "RIGHT"   // left-to-right (default for layered)
"elk.direction": "DOWN"    // top-to-bottom
"elk.direction": "LEFT"    // right-to-left
"elk.direction": "UP"      // bottom-to-top
```

## Notes

- ELK computes layout only — drawing is your responsibility.
- For complex graphs, build the `children` and `edges` arrays in Python before embedding as JSON.
- For SVG screenshots: use inline `fill` attributes on `<rect>` elements, not CSS `fill` (see `../screenshot-friendly.md`).
- See `knowledge-graph.md` for a full networkx → ELK pipeline.
