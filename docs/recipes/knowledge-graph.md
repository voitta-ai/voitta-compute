# Recipe: Knowledge graph from networkx

Build a graph with networkx on the server, pass it to ELK for layout, render as SVG in the iframe.

## Pattern

```python
import json
import networkx as nx

def build(ctx):
    # 1. Build graph with networkx
    G = nx.DiGraph()
    G.add_edges_from([
        ("CEO",    "CTO"),
        ("CEO",    "CFO"),
        ("CTO",    "Engineering"),
        ("CTO",    "Platform"),
        ("CFO",    "Finance"),
        ("CFO",    "Legal"),
    ])

    # 2. Convert to ELK format
    NODE_W, NODE_H = 120, 36
    elk_graph = {
        "id": "root",
        "layoutOptions": {
            "elk.algorithm":      "layered",
            "elk.direction":      "DOWN",
            "elk.spacing.nodeNode": "30",
            "elk.layered.spacing.nodeNodeBetweenLayers": "50",
        },
        "children": [
            {"id": n, "width": NODE_W, "height": NODE_H,
             "labels": [{"text": n}]}
            for n in G.nodes()
        ],
        "edges": [
            {"id": f"{u}__{v}", "sources": [u], "targets": [v]}
            for u, v in G.edges()
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
</style>
</head>
<body>
<svg id="svg">
  <defs>
    <marker id="arrow" markerWidth="8" markerHeight="6" refX="7" refY="3" orient="auto">
      <polygon points="0 0, 8 3, 0 6" fill="{text}"/>
    </marker>
  </defs>
</svg>
<script src="https://cdn.jsdelivr.net/npm/elkjs@0.9.0/lib/elk.bundled.js"></script>
<script>
const elk = new ELK();
const graph = {json.dumps(elk_graph)};
const BG = "{accent}", TEXT_COL = "#fff", EDGE_COL = "{text}";

function svgEl(tag, attrs) {{
  const el = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  return el;
}}

elk.layout(graph).then(g => {{
  const svg = document.getElementById('svg');
  const PAD = 24;
  let maxX = 0, maxY = 0;
  g.children.forEach(n => {{
    maxX = Math.max(maxX, n.x + n.width);
    maxY = Math.max(maxY, n.y + n.height);
  }});
  svg.setAttribute('viewBox', `${{-PAD}} ${{-PAD}} ${{maxX + PAD*2}} ${{maxY + PAD*2}}`);
  svg.setAttribute('height', maxY + PAD*2);

  // Edges
  (g.edges || []).forEach(edge => {{
    const sec = edge.sections?.[0];
    if (!sec) return;
    const pts = [sec.startPoint, ...(sec.bendPoints||[]), sec.endPoint];
    const d = pts.map((p,i) => (i===0?'M':'L') + p.x + ' ' + p.y).join(' ');
    svg.appendChild(svgEl('path', {{
      d, stroke: EDGE_COL, 'stroke-width': '1.5', fill: 'none',
      'marker-end': 'url(#arrow)'
    }}));
  }});

  // Nodes
  g.children.forEach(node => {{
    const label = node.labels?.[0]?.text || node.id;
    const g_el = svgEl('g', {{}});
    g_el.appendChild(svgEl('rect', {{
      x: node.x, y: node.y, width: node.width, height: node.height,
      rx: 6, fill: BG
    }}));
    const t = svgEl('text', {{
      x: node.x + node.width/2, y: node.y + node.height/2,
      fill: TEXT_COL, 'font-size': '12', 'font-family': 'sans-serif',
      'dominant-baseline': 'middle', 'text-anchor': 'middle'
    }});
    t.textContent = label;
    g_el.appendChild(t);
    svg.appendChild(g_el);
  }});
}});
</script>
</body></html>"""
```

## Loading from a snapshot

```python
import json, networkx as nx

def build(ctx):
    data = ctx.raw("my-graph-snapshot")
    # data expected shape: {"nodes": [...], "edges": [{"source": ..., "target": ...}]}
    G = nx.DiGraph()
    for node in data["nodes"]:
        G.add_node(node["id"], label=node.get("label", node["id"]))
    for edge in data["edges"]:
        G.add_edge(edge["source"], edge["target"])
    # ... rest of the pattern above
```

## Layout options

```python
# Horizontal pipeline
"elk.algorithm": "layered", "elk.direction": "RIGHT"

# Hierarchical tree
"elk.algorithm": "mrtree", "elk.direction": "DOWN"

# Force-directed (organic)
"elk.algorithm": "force"
```

## Screenshot notes

SVG `fill` / `stroke` must be **inline attributes** (not CSS) for `html-to-image` to capture them correctly. The pattern above uses `svgEl('rect', {fill: BG})` — this sets `fill` as an attribute. Do not use `style="fill: ..."` on SVG elements.
