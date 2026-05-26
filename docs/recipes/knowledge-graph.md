# Recipe: Knowledge graph from corporate documents

Extract actors, actions, and approval steps from unstructured text, store
them as RDF triples, query with SPARQL, and render as a flowchart via ELK.

**Use case:** paste a policy doc, email thread, or SOP — the LLM extracts
the process as triples, stores them in a persistent graph, and you can
visualise or query across multiple documents.

## Libraries

Both are installed at first launch (no extra step needed):

| Library | Role |
|---|---|
| `rdflib` | RDF triple store, SPARQL queries, serialisation (Turtle / JSON-LD / N-Triples) |
| `networkx` | Graph algorithms, cycle detection, ELK-compatible edge list for rendering |

---

## Full pipeline overview

```
document text
     │
     ▼
LLM extraction prompt  →  JSON triples
     │
     ▼
rdflib Graph  ──serialize──▶  process.ttl  (persistent on disk)
     │
     ├── SPARQL queries  →  approval chains, role lists, transitive paths
     │
     └── networkx DiGraph  →  ELK layout  →  SVG flowchart in report pane
```

---

## Step 1 — LLM extraction prompt

Ask the model to return triples from the document.  Use the `extract_process`
tool pattern below rather than expecting triples in the chat message — it
gives you structured output you can JSON-parse reliably.

```python
EXTRACT_SYSTEM = """
You extract business process steps as RDF-style triples.
Return a JSON array only — no commentary, no markdown fences.

Each element: {"subject": "...", "predicate": "...", "object": "..."}

Preferred predicates (use these verbatim for consistent SPARQL queries):
  approves          — who must sign off on what
  submits_to        — who sends work to whom
  triggers          — event or step that starts another
  requires          — prerequisite
  notifies          — who is informed (but not a blocker)
  escalates_to      — fallback path when approval is rejected/stalled
  delegates_to      — who acts in someone's absence
  is_role_of        — "FinanceManager is_role_of Alice"
  decides           — who makes a binary decision
  is_blocked_by     — step cannot start until another completes

For decision nodes add a fourth key "condition": "if amount > $10k"
"""
```

Example model output for a purchase-order policy:
```json
[
  {"subject": "Engineer",        "predicate": "submits_to",    "object": "Manager"},
  {"subject": "Manager",         "predicate": "approves",      "object": "PurchaseOrder"},
  {"subject": "Manager",         "predicate": "escalates_to",  "object": "Director",
   "condition": "if amount > $10k"},
  {"subject": "PurchaseOrder",   "predicate": "triggers",      "object": "Procurement"},
  {"subject": "Procurement",     "predicate": "notifies",      "object": "Finance"},
  {"subject": "Director",        "predicate": "is_role_of",    "object": "Jane Smith"},
  {"subject": "Manager",         "predicate": "delegates_to",  "object": "TeamLead",
   "condition": "if Manager absent"}
]
```

---

## Step 2 — Build and persist the graph

```python
import json
from pathlib import Path
from rdflib import Graph, Literal, Namespace, URIRef, XSD

P = Namespace("urn:process:")
GRAPH_PATH = Path.home() / "Library/Application Support/Voitta Compute/process.ttl"


def load_or_create() -> Graph:
    """Load existing graph from disk, or start fresh."""
    g = Graph()
    g.bind("p", P)
    if GRAPH_PATH.is_file():
        g.parse(str(GRAPH_PATH), format="turtle")
    return g


def save(g: Graph) -> None:
    GRAPH_PATH.parent.mkdir(parents=True, exist_ok=True)
    g.serialize(str(GRAPH_PATH), format="turtle")


def add_triples(g: Graph, triples: list[dict], source_doc: str = "") -> int:
    """Merge new triples into the graph. Returns count added."""
    added = 0
    for t in triples:
        s = P[t["subject"].replace(" ", "_")]
        p = P[t["predicate"]]
        o = P[t["object"].replace(" ", "_")]
        if (s, p, o) not in g:
            g.add((s, p, o))
            added += 1
        # attach condition label if present
        cond = t.get("condition")
        if cond:
            g.add((s, P["condition_for_" + t["predicate"]], Literal(cond)))
        # tag the source document so you can filter by doc later
        if source_doc:
            g.add((s, P["mentioned_in"], Literal(source_doc)))
    return added
```

### Serialisation formats

```python
# Turtle — human-readable, good default
g.serialize("process.ttl", format="turtle")

# JSON-LD — LLM-friendly, easy to paste back into a prompt
g.serialize("process.jsonld", format="json-ld")

# N-Triples — one triple per line, appendable with a text editor
g.serialize("process.nt", format="nt")

# Reload any format — rdflib auto-detects from extension
g2 = Graph().parse("process.ttl")
```

### Turtle output example

```turtle
@prefix p: <urn:process:> .

p:Manager      p:approves      p:PurchaseOrder ;
               p:escalates_to  p:Director ;
               p:delegates_to  p:TeamLead .
p:Engineer     p:submits_to    p:Manager .
p:Director     p:is_role_of    "Jane Smith" .
```

---

## Step 3 — SPARQL queries

All queries use `PREFIX p: <urn:process:>`.

### Who approves what
```python
def approval_pairs(g: Graph) -> list[tuple[str, str]]:
    q = """
    PREFIX p: <urn:process:>
    SELECT ?who ?what WHERE { ?who p:approves ?what }
    ORDER BY ?who
    """
    return [(str(r.who).split(":")[-1], str(r.what).split(":")[-1])
            for r in g.query(q)]
```

### All roles (person → role mapping)
```python
def roles(g: Graph) -> list[tuple[str, str]]:
    q = """
    PREFIX p: <urn:process:>
    SELECT ?role ?person WHERE { ?role p:is_role_of ?person }
    """
    return [(str(r.role).split(":")[-1], str(r.person))
            for r in g.query(q)]
```

### Approval count per person (who is a bottleneck?)
```python
def approval_load(g: Graph) -> dict[str, int]:
    q = """
    PREFIX p: <urn:process:>
    SELECT ?who (COUNT(?what) AS ?n) WHERE {
        ?who p:approves ?what
    } GROUP BY ?who ORDER BY DESC(?n)
    """
    return {str(r.who).split(":")[-1]: int(r.n) for r in g.query(q)}
```

### Full path from node A to node B (transitive)
```python
def path_exists(g: Graph, start: str, end: str) -> bool:
    q = f"""
    PREFIX p: <urn:process:>
    ASK {{
        p:{start.replace(' ', '_')} (p:submits_to|p:triggers|p:approves|p:escalates_to)+ p:{end.replace(' ', '_')}
    }}
    """
    return bool(g.query(q))
```

### Find all escalation paths
```python
def escalations(g: Graph) -> list[tuple[str, str, str]]:
    """Returns (from, to, condition) triples."""
    q = """
    PREFIX p: <urn:process:>
    SELECT ?from ?to ?cond WHERE {
        ?from p:escalates_to ?to .
        OPTIONAL { ?from ?cp ?cond .
                   FILTER(STRENDS(STR(?cp), "condition_for_escalates_to")) }
    }
    """
    return [
        (str(r["from"]).split(":")[-1],
         str(r.to).split(":")[-1],
         str(r.cond) if r.cond else "")
        for r in g.query(q)
    ]
```

### Detect approval cycles (A approves something that eventually approves A)
```python
def has_cycle(g: Graph) -> bool:
    q = """
    PREFIX p: <urn:process:>
    ASK { ?x (p:approves|p:submits_to)+ ?x }
    """
    return bool(g.query(q))
```

Or use networkx (faster for large graphs):
```python
import networkx as nx

def nx_from_graph(g: Graph) -> nx.DiGraph:
    G = nx.DiGraph()
    for s, p, o in g:
        if isinstance(o, URIRef):
            G.add_edge(
                str(s).split(":")[-1],
                str(o).split(":")[-1],
                label=str(p).split(":")[-1],
            )
    return G

G = nx_from_graph(g)
print("has cycle:", not nx.is_directed_acyclic_graph(G))
print("approval load:", nx.in_degree_centrality(G))  # bottleneck nodes
```

---

## Step 4 — Render the flowchart (report script)

> **Always include zoom / pan / fit-all controls** on ELK charts unless the
> user explicitly asks for a static image.  Process graphs grow large quickly
> and are unreadable without them.  See
> [elk.md § Adding zoom / pan / fit-all controls](elk.md#adding-zoom--pan--fit-all-controls)
> for the full pattern and screenshot rules.

Node shapes and colours differentiate roles, actions, and documents.

```python
import json
from pathlib import Path
from rdflib import Graph, URIRef, Literal, Namespace

P = Namespace("urn:process:")
GRAPH_PATH = Path.home() / "Library/Application Support/Voitta Compute/process.ttl"

EDGE_COLORS = {
    "approves":      "#27ae60",
    "submits_to":    "#2980b9",
    "triggers":      "#8e44ad",
    "escalates_to":  "#e74c3c",
    "notifies":      "#f39c12",
    "delegates_to":  "#16a085",
    "requires":      "#7f8c8d",
    "is_blocked_by": "#c0392b",
}

NODE_COLORS = {
    "is_role_of": "#fdebd0",
    "approves":   "#d5e8d4",
    "default":    "#dae8fc",
}


def build(ctx):
    if not GRAPH_PATH.is_file():
        return "<p style='padding:16px'>No knowledge graph yet. Extract a process first.</p>"

    g = Graph()
    g.parse(str(GRAPH_PATH), format="turtle")

    nodes_seen: dict[str, dict] = {}
    edges: list[dict] = []
    edge_meta: list[dict] = []

    for s, p, o in g:
        if not isinstance(o, URIRef):
            continue
        pred = str(p).split(":")[-1]
        if pred.startswith("condition_") or pred == "mentioned_in":
            continue
        s_id = str(s).split(":")[-1]
        o_id = str(o).split(":")[-1]
        for nid in (s_id, o_id):
            if nid not in nodes_seen:
                nodes_seen[nid] = {"id": nid, "width": 170, "height": 52,
                                   "labels": [{"text": nid}]}
        edges.append({
            "id": f"e{len(edges)}",
            "sources": [s_id],
            "targets": [o_id],
            "labels": [{"text": pred}],
        })
        edge_meta.append({"color": EDGE_COLORS.get(pred, "#95a5a6")})

    if not nodes_seen:
        return "<p style='padding:16px'>Graph is empty.</p>"

    approvers  = {str(s).split(":")[-1] for s, p, _ in g if str(p).split(":")[-1] == "approves"}
    role_nodes = {str(s).split(":")[-1] for s, p, _ in g if str(p).split(":")[-1] == "is_role_of"}
    for nid, n in nodes_seen.items():
        if nid in role_nodes:
            n["color"] = NODE_COLORS["is_role_of"]
        elif nid in approvers:
            n["color"] = NODE_COLORS["approves"]
        else:
            n["color"] = NODE_COLORS["default"]

    elk_graph = json.dumps({
        "id": "root",
        "layoutOptions": {
            "elk.algorithm": "layered",
            "elk.direction": "DOWN",
            "elk.spacing.nodeNode": "60",
            "elk.layered.spacing.nodeNodeBetweenLayers": "70",
            "elk.edgeRouting": "ORTHOGONAL",
        },
        "children": list(nodes_seen.values()),
        "edges": edges,
    })
    edge_meta_json = json.dumps(edge_meta)

    return f"""<!doctype html>
<html>
<head>
  <script src="https://cdn.jsdelivr.net/npm/elkjs@0.9.3/lib/elk.bundled.js"></script>
  <style>
    body {{ margin:0; padding:0; background:#fff; font-family:system-ui; }}
    svg  {{ display:block; transform-origin:0 0; }}
    #toolbar button {{
      width:32px; height:32px; font-size:16px; cursor:pointer;
      background:#fff; border:1px solid #ccc; border-radius:4px;
    }}
    #toolbar button:hover {{ background:#f0f0f0; }}
  </style>
</head>
<body>
<div id="wrap" style="position:relative; display:inline-block; padding:16px">
  <svg id="d"></svg>
  <!-- position:absolute so the toolbar appears in screenshots at the correct position -->
  <div id="toolbar" style="position:absolute; top:24px; right:24px; z-index:10; display:flex; gap:4px">
    <button onclick="zoom(1.25)" title="Zoom in">+</button>
    <button onclick="zoom(1/1.25)" title="Zoom out">−</button>
    <button onclick="fitAll()" title="Fit all">⬚</button>
  </div>
</div>
<script>
const graph = {elk_graph};
const meta  = {edge_meta_json};
const elk = new ELK();
let scale = 1, tx = 0, ty = 0, dgW = 0, dgH = 0;
const svg = document.getElementById("d");

function applyTransform() {{
  svg.style.transform = `translate(${{tx}}px,${{ty}}px) scale(${{scale}})`;
}}
function zoom(f) {{ scale *= f; applyTransform(); }}
function fitAll() {{
  // Use window.innerWidth/Height — NOT clientHeight of any container.
  // clientHeight reads the probe height (~8000px) during screenshot and
  // would shrink the diagram to a thumbnail.
  scale = Math.min(window.innerWidth / dgW, window.innerHeight / dgH) * 0.92;
  tx = (window.innerWidth  - dgW * scale) / 2;
  ty = 20;
  applyTransform();
}}

// pan support
let dragging = false, px = 0, py = 0;
svg.addEventListener("mousedown", e => {{ dragging=true; px=e.clientX; py=e.clientY; }});
window.addEventListener("mousemove", e => {{
  if (!dragging) return;
  tx += e.clientX - px; ty += e.clientY - py;
  px = e.clientX; py = e.clientY;
  applyTransform();
}});
window.addEventListener("mouseup", () => dragging = false);

elk.layout(graph).then(g => {{
  const pad = 24;
  dgW = g.width + pad * 2;
  dgH = g.height + pad * 2;
  svg.setAttribute("width",  dgW);
  svg.setAttribute("height", dgH);

  const defs = document.createElementNS("http://www.w3.org/2000/svg","defs");
  const colors = [...new Set(meta.map(m => m.color))];
  defs.innerHTML = colors.map(c => {{
    const id = "arr" + c.replace("#","");
    return `<marker id="${{id}}" markerWidth="8" markerHeight="8"
      refX="8" refY="3" orient="auto">
      <path d="M0,0 L0,6 L8,3 z" fill="${{c}}"/>
    </marker>`;
  }}).join("");
  svg.appendChild(defs);

  for (const n of g.children || []) {{
    const r = document.createElementNS("http://www.w3.org/2000/svg","rect");
    r.setAttribute("x", n.x+pad); r.setAttribute("y", n.y+pad);
    r.setAttribute("width", n.width); r.setAttribute("height", n.height);
    r.setAttribute("rx","7");
    r.setAttribute("fill",         n.color || "#dae8fc");
    r.setAttribute("stroke",       "#555");
    r.setAttribute("stroke-width", "1.2");
    svg.appendChild(r);
    const t = document.createElementNS("http://www.w3.org/2000/svg","text");
    t.setAttribute("x", n.x+pad+n.width/2);
    t.setAttribute("y", n.y+pad+n.height/2+5);
    t.setAttribute("text-anchor","middle");
    t.setAttribute("font-size","13"); t.setAttribute("fill","#1a1a2e");
    t.textContent = (n.labels||[])[0]?.text || n.id;
    svg.appendChild(t);
  }}

  for (let i = 0; i < (g.edges||[]).length; i++) {{
    const e   = g.edges[i];
    const col = (meta[i] || {{}}).color || "#888";
    const aid = "arr" + col.replace("#","");
    for (const sec of e.sections || []) {{
      const pts = [sec.startPoint, ...(sec.bendPoints||[]), sec.endPoint];
      const d = pts.map((p,j)=>`${{j?"L":"M"}} ${{p.x+pad}} ${{p.y+pad}}`).join(" ");
      const path = document.createElementNS("http://www.w3.org/2000/svg","path");
      path.setAttribute("d", d); path.setAttribute("fill","none");
      path.setAttribute("stroke", col); path.setAttribute("stroke-width","1.6");
      path.setAttribute("marker-end", `url(#${{aid}})`);
      svg.appendChild(path);
    }}
    const lbl = (e.labels||[])[0];
    if (lbl) {{
      const t = document.createElementNS("http://www.w3.org/2000/svg","text");
      t.setAttribute("x", lbl.x+pad+(lbl.width||0)/2);
      t.setAttribute("y", lbl.y+pad+11);
      t.setAttribute("text-anchor","middle");
      t.setAttribute("font-size","11"); t.setAttribute("fill", col);
      t.setAttribute("font-weight","600");
      t.textContent = lbl.text;
      svg.appendChild(t);
    }}
  }}

  fitAll();  // call directly in .then() — no rAF delay needed
}});
</script>
</body>
</html>"""
```

---

## Step 5 — Putting it together as a Chainlit tool

Register a `build_process_graph` tool so the LLM can extract and persist in
one shot.  Put this in a plugin's `tools.py` or a new file under
`backend/app/tools/server/`.

```python
import json
from typing import Any

from app.tools.registry import ToolCtx, ToolSpec, registry
from app.config import PROJECT_ROOT
from pathlib import Path

GRAPH_PATH = Path.home() / "Library/Application Support/Voitta Compute/process.ttl"


async def _handler(args: dict[str, Any], _ctx: ToolCtx) -> dict[str, Any]:
    raw = args.get("triples_json", "")
    source = args.get("source_doc", "")

    try:
        triples = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(triples, list):
            raise ValueError("expected a JSON array")
    except Exception as exc:
        return {"ok": False, "error": f"bad triples JSON: {exc}"}

    from rdflib import Graph, Namespace, Literal, URIRef
    P = Namespace("urn:process:")

    g = Graph()
    g.bind("p", P)
    if GRAPH_PATH.is_file():
        g.parse(str(GRAPH_PATH), format="turtle")

    before = len(g)
    for t in triples:
        try:
            s = P[t["subject"].replace(" ", "_")]
            p = P[t["predicate"]]
            o = P[t["object"].replace(" ", "_")]
            g.add((s, p, o))
            if t.get("condition"):
                g.add((s, P[f"condition_for_{t['predicate']}"], Literal(t["condition"])))
            if source:
                g.add((s, P["mentioned_in"], Literal(source)))
        except (KeyError, TypeError):
            continue

    GRAPH_PATH.parent.mkdir(parents=True, exist_ok=True)
    g.serialize(str(GRAPH_PATH), format="turtle")

    return {
        "ok": True,
        "triples_added": len(g) - before,
        "total_triples": len(g),
        "graph_path": str(GRAPH_PATH),
    }


registry.register(ToolSpec(
    name="build_process_graph",
    description=(
        "Extract process triples from corporate text and persist them "
        "in the knowledge graph. Pass the document text to the LLM first "
        "with the extraction prompt, then call this with the resulting "
        "JSON array.  After calling this, run the 'process_flowchart' "
        "script to visualise."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "triples_json": {
                "type": "string",
                "description": "JSON array of {subject, predicate, object} triples",
            },
            "source_doc": {
                "type": "string",
                "description": "Label for this document (for provenance filtering)",
            },
        },
        "required": ["triples_json"],
        "additionalProperties": False,
    },
    side="server",
    handler=_handler,
))
```

---

## Incremental updates — adding more documents

The graph on disk accumulates triples across sessions.  Each call to
`add_triples` (or the `build_process_graph` tool) merges without
duplicating — rdflib silently ignores `g.add(triple)` when it already
exists.

```python
# Session 1: extract from "procurement_policy.pdf"
g = load_or_create()
add_triples(g, triples_from_doc1, source_doc="procurement_policy.pdf")
save(g)

# Session 2: extract from "finance_approval_sop.pdf" — merges cleanly
g = load_or_create()
add_triples(g, triples_from_doc2, source_doc="finance_approval_sop.pdf")
save(g)

# Query only triples from a specific doc
def triples_from_source(g: Graph, doc: str):
    q = f"""
    PREFIX p: <urn:process:>
    SELECT ?s ?pred ?o WHERE {{
        ?s p:mentioned_in "{doc}" .
        ?s ?pred ?o .
        FILTER(?pred != p:mentioned_in)
    }}
    """
    return list(g.query(q))
```

To wipe and restart:
```python
GRAPH_PATH.unlink(missing_ok=True)
```

---

## Tips for corporate process extraction

- **Condition edges:** the `"condition"` key on a triple becomes a labelled
  edge annotation — use it for `"if amount > $10k"`, `"if urgent"`, etc.
- **Bottleneck detection:** `nx.in_degree_centrality(G)` ranks nodes by how
  many processes depend on them — high-centrality nodes are approval choke
  points.
- **Transitive closure:** SPARQL property paths (`+`, `*`) let you ask
  "can Engineer ultimately reach Finance?" without walking the graph manually.
- **Merge conflicts:** if two docs say `Manager approves PurchaseOrder` and
  `Director approves PurchaseOrder`, both triples coexist — query returns two
  rows.  That is intentional; the graph models reality, not a single policy.
- **Export to Mermaid:** once you have the networkx DiGraph, generate Mermaid
  text for a quick text-only view alongside the ELK render:

```python
def to_mermaid(G: nx.DiGraph) -> str:
    lines = ["graph TD"]
    for u, v, d in G.edges(data=True):
        lbl = d.get("label", "")
        lines.append(f"    {u} -->|{lbl}| {v}")
    return "\n".join(lines)
```
