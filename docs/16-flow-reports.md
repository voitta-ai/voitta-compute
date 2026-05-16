# Flow reports — engineering-aesthetic process diagrams

Flow reports render a stored Python script as a flow chart in the
left-side report pane. Same lifecycle as HoloViz reports (define ·
edit · show · errors), but rendered by **ReactFlow inside the widget
shadow DOM** — no iframe, no Panel, no Bokeh.

The visual target is *engineering schematic*: orthogonal smoothstep
edges, dotted-grid canvas, monospace step IDs in slate title bars,
optional corner title-block with drawing ID and revision. Plugins
re-skin via theme tokens.

## When to reach for it

Process diagrams, approval workflows, decision trees, state machines,
incident-response playbooks, branch-and-merge release flows — anything
where the user wants to *see the shape of a process*.

Don't use a flow report for tabular or chart data — that's still
HoloViz territory (`define_report` + `show_holoviz_report`).

## LLM tool surface

| Tool | Notes |
|---|---|
| `define_flow_report(name, code)` | Persist `.py`. Smoke-tests `build(ctx)` immediately. |
| `edit_flow_report(name, edits)` | Search-replace edits, same shape as `edit_report_script`. |
| `show_flow_report(report_id, title?, wait_s?)` | Build → ship JSON → mount → wait for ready. |
| `list_flow_reports()` | Sidecar metadata for every persisted flow. |
| `get_flow_report(name)` | Read the stored source. |
| `delete_flow_report(name)` | Irreversible. |
| `get_flow_render_errors(report_id, ...)` | Tail of post-render errors. |

## The flow script — full surface

```python
def build(ctx):
    p = FlowBuilder("Approval", "Five-step approval flow")

    # ── diagram-level configuration (all optional) ─────────────────
    p.layout(direction="LR", engine="elk")
        # direction: TB | LR | BT | RL
        # engine:    elk (default, higher quality) | dagre (smaller)
    p.edge_style("smoothstep")
        # smoothstep (default, orthogonal + rounded corners)
        # step | straight | bezier
    p.background("dots")            # dots | lines | cross | none
    p.show_minimap(True)            # corner overview, default off
    p.title_block(drawing_id="APR-001", rev="B", author="voitta")
        # engineering-drawing-style corner panel

    # ── steps ───────────────────────────────────────────────────────
    p.trigger("start", "Submit Request",
              tone="info", icon="play",
              roles=["Requestor"], artifacts_out=["Request Form"])

    p.activity("review", "Review Request",
               tone="default", icon="search",
               badges=[
                   {"label": "Manager", "tone": "info"},
                   {"label": "SLA 48h", "tone": "warning"},
               ],
               meta=[("Input", "Request Form"),
                     ("Output", "Decision")],
               note="Skips review if amount < $1,000")

    p.decision("decision", "Approved?",
               tone="warning", icon="git-branch",
               branches=[("Yes", "notify"), ("No", "reject")])

    p.activity("notify", "Send Approval",
               tone="success", icon="mail")

    p.end("reject", "Request Denied",
          tone="critical", icon="x-circle")

    # ── edges ───────────────────────────────────────────────────────
    p.connect("start", "review")
    p.connect("review", "decision")
    p.connect("decision", "notify", tone="success")

    return p   # or:  return p.to_dict()
```

`FlowBuilder` is injected into the namespace automatically — no
import statement. The script runs in-process.

## Step types

| Type | Default icon | Visual cue |
|---|---|---|
| `trigger`  | `play`       | Start of a process. |
| `activity` | `square`     | An action / task. |
| `decision` | `git-branch` | Branching point — requires `branches=[(label, target), …]` ≥ 2. Has 4 shapes — see below. |
| `artifact` | `file-text`  | A document / deliverable. |
| `end`      | `circle-dot` | Terminal — rendered with a thicker bottom border. |

### Decision shapes

`p.decision()` takes a `shape=` kwarg with four values:

| Shape | When to use |
|---|---|
| `"rect"` (default) | 2–3 branches with short labels. Rectangle + DECISION chip; branch names ride on the edges. |
| `"port"` | 4+ branches, or any number of branches with descriptive labels. Schematic-style multi-port — each branch is a labeled output row on the node, edges leave unlabeled (the label is on the node). Reads like an IC datasheet pinout. |
| `"diamond"` | Binary yes/no where the *shape itself* should signal "this is a question". Classic BPMN rotated rhombus. |
| `"junction"` | Many branches where the labels ARE the content. Tiny labeled dot with labels on the outgoing edges. Drops title bar, badges, meta — rejects `roles=` / `meta=` / `note=`. |

```python
# port-shape decision (the engineering-schematic case)
p.decision("route", "Route by Priority",
           shape="port",
           branches=[
               ("Critical",  "critical_handler"),
               ("High",      "high_handler"),
               ("Medium",    "medium_handler"),
               ("Low",       "low_handler"),
               ("Deferred",  "defer_handler"),
           ])
```

For `"port"`, the auto-emitted branch connections carry a `source_handle`
field (e.g. `"port-0"`, `"port-1"`) so the frontend attaches each edge to
its dedicated handle.

## Visual customization (per-step `**viz`)

Every step method accepts the same customization kwargs:

### `tone` — semantic colour

```python
tone="default"   # slate
tone="info"      # sky-cyan
tone="success"   # emerald
tone="warning"   # amber
tone="critical"  # red
```

Colours the title bar and the accent on outgoing edges. Resolves to
`--voitta-flow-tone-{tone}-{bg,fg,border}` tokens — plugin themes
re-skin without touching scripts.

### `icon` — title-bar glyph

Two forms:

```python
icon="git-branch"                    # any lucide-icons name (kebab-case)
icon={"svg": "<svg viewBox='0 0 24 24'>...</svg>"}    # inline SVG escape hatch
```

The curated lucide set covers ~75 commonly-needed icons (`play`,
`pause`, `check`, `alert-triangle`, `git-branch`, `mail`, `database`,
`user`, `users`, `clock`, `dollar-sign`, etc. — see
`frontend/src/components/flow-nodes/icons.tsx` for the full list). An
unknown lucide name falls back to the step-type default — no error,
no broken render.

Inline SVG is sanitised by DOMPurify before injection. Cap: 4 KB.

### `badges` — small status pills

```python
badges=[
    "Requestor",                              # string → tone="default"
    {"label": "Manager", "tone": "info"},
    {"label": "SLA 48h", "tone": "warning"},
]
```

### `meta` — key/value rows in the node body

```python
meta=[
    ("Input", "Request Form"),                # 2-tuple
    {"key": "Output", "value": "Decision"},   # or dict
]
```

### `note` — italic annotation below the meta block

```python
note="Skips review if amount < $1,000"
```

### `style` / `title_style` — CSS escape hatch

```python
style={"background": "#1e293b", "border-radius": "8px"}
title_style={"background": "#0f172a", "color": "#f1f5f9"}
```

Validated server-side against a safe-list — only visual properties
(`background`, `color`, `border-*`, `font-*`, `padding`, `margin`,
`box-shadow`, etc.). Layout-affecting properties (`position`,
`transform`, `display`, `z-index`) are rejected. So is anything
containing `url(...)` or `expression(...)` — those could issue
network requests or escape the value context.

### Roles / artifacts (inferred meta)

If you pass `roles=[...]`, `artifacts_in=[...]`, or
`artifacts_out=[...]`, those render automatically as meta rows in the
node body, prefixed with appropriate icons. They're separate from the
`meta=[...]` rows so the LLM can mix domain-specific data into a step
without losing the standard process semantics.

## Per-edge customization

```python
p.connect("decision", "notify", label="Yes", style="solid",  tone="success")
p.connect("decision", "reject", label="No",  style="dashed", tone="critical")
```

`tone` colours the edge stroke and arrowhead. `style` chooses
solid/dashed. Decision branches auto-emit connections — first branch
gets `style="solid"`, remaining get `style="dashed"`. Don't call
`.connect()` for branch arrows; use `.decision(branches=[...])`.

## `ctx` — what's available in `build(ctx)`

Intentionally small. Flow scripts produce a *structural* definition,
not a live layout, so most of `ScriptContext` is irrelevant.

| Method | Purpose |
|---|---|
| `ctx.get_theme(host=…)` | Active palette dict (same shape as `get_active_theme` tool). Use to pick group colours from tokens, not hex codes. |
| `ctx.log(*args)` | Append debug line — surfaced as `log_lines` in the tool result. |

## The pipeline

```
LLM tool                                browser
────────                                ───────
define_flow_report(name, code)
    │
    ▼  parse-check + persist
scripts/flows/<slug>/code.py    ◄── edit_flow_report (find/replace)
scripts/flows/<slug>/meta.json
    │  (smoke_test_flow runs build(ctx))
    │
show_flow_report(report_id)
    │
    ▼  exec code.py with FlowBuilder injected
    │  build(ctx) → FlowBuilder → .to_dict() → JSON definition
    │
    ▼  call_browser("show_flow_report", { definition, render_id, … })
    │
    ▼  registerPrimitive("show_flow_report")
    │      sets activeReport = { kind: "flow", definition, … }
    │
    ▼  <FlowReportPane> mounts inside the widget shadow DOM
    │      <FlowDiagram> runs ELK/dagre layout → ReactFlow renders
    │      POST /api/report-render-events  { kind: "ready", render_id }
    │
    ▼  show_flow_report tool returns { status: "ready", elapsed_ms, … }
```

## Architecture (post-floating-edges refactor)

The rendering pipeline uses ReactFlow's native features wherever
possible, instead of hand-crafted scaffolding:

| Feature | How we use it | What it replaces |
|---|---|---|
| **Orthogonal edges** (ELK `edgeRouting=ORTHOGONAL` → `OrthogonalEdge` component) | Default when `engine="elk"` and `edge_routing="orthogonal"`. ELK precomputes N-bend polylines; the frontend renders them with a single `<path>` of `L` segments. True rectilinear routing — no zig-zag detours, no crossings. | ReactFlow's bundled `getSmoothStepPath` (a 2-bend approximation that produces bad routes for non-aligned source/target). |
| **Floating edges** (`useInternalNode` + `getEdgeParams`) | Fallback when no bend points were computed (dagre layouts, or ELK with non-orthogonal routing). Endpoints calculated from source/target geometry; smoothstep path. | Multi-handle scaffolding (one Handle per cardinal direction). Most nodes now have ONE hidden source and ONE hidden target handle. |
| **`useNodeConnections`** (in `DecisionPortNode`) | Each port row queries its outgoing connection by handle ID and resolves the target node's label via `useReactFlow().getNode()`. Renders `→ destination` inline next to the branch label. | Hand-wired prop-drilling of destination names. |
| **`EdgeLabelRenderer`** | DOM portal for rich edge labels. We render `<div class="flow-edge-label tone-X">` with full CSS styling; tones get distinct backgrounds and borders. | SVG `<text>` labels (limited typography, no per-tone background). |
| **`<Panel position="top-right">`** | Built-in positioned overlay for the title block. ReactFlow handles z-index and corner positioning. | Hand-positioned `position: absolute` div. |
| **`nodeOrigin=[0.5, 0.5]`** | Centre-anchored node positions. The layout pass (`flow-layout.ts`) returns CENTRE coords for each node; ReactFlow handles the half-size offset. | Manual `pos.x - sz.width/2` arithmetic. |
| **`colorMode` + `--xy-*` CSS variables** | `colorMode="auto"` reads host theme luminance and picks light/dark. ReactFlow adds a `.dark` class on the wrapper which swaps an internal variable bundle. | Manual `!important` overrides fighting ReactFlow's bundled CSS. |
| **`markerEnd: { color: null }`** | Arrowhead colour follows `--xy-edge-stroke`. We set that variable per-tone on the edge (`.flow-edge.tone-info { --xy-edge-stroke: #38bdf8 }`) — stroke AND arrowhead update together. | Duplicated TONE_HEX colour map for marker fills. |
| **`animated: true`** | Per-edge marching-ants animation, native ReactFlow behaviour. | Custom CSS keyframes. |
| **`elevateEdgesOnSelect: true`** | Selected edge rises above adjacent nodes. | Z-index gymnastics. |

The result: significantly less custom CSS, no `!important` declarations in the flow path, and per-edge / per-decision visual variation is just a flag in the wire format.

## Theming

The diagram lives in the widget shadow DOM, so `--voitta-*` token
inheritance is automatic. The flow-specific tokens are:

### Tone palette (5 tones × 4 properties each)

```
--voitta-flow-tone-{default|info|success|warning|critical}-bg
                                                          -fg
                                                          -border
--voitta-flow-edge-{default|info|success|warning|critical}
```

Defaults are sober engineering colours (slate / sky / emerald / amber
/ red, all `-900`/`-800` body with light text). Plugins override
freely in their `theme.css`:

```css
:host {
  --voitta-flow-tone-info-bg:     #003e7e;
  --voitta-flow-tone-info-border: #4ea0ff;
  --voitta-flow-edge-info:        #4ea0ff;
}
```

### Node-body palette (controlled by `p.palette()`)

```
--voitta-flow-node-bg          /* body fill */
--voitta-flow-node-fg          /* body text */
--voitta-flow-node-fg-muted    /* meta-row text */
--voitta-flow-node-fg-faint    /* meta-row labels */
--voitta-flow-node-border      /* node outer border */
--voitta-flow-grid             /* dotted-grid colour */
```

These live as bare defaults in `frontend/src/theme.css` (light card
preset). Override sources, most-specific wins:

  1. **Per-node `style={...}`** on a step in the build script —
     cascades onto `.flow-node` directly; wins everything below.
  2. **Diagram-level `p.palette("dark")`** in the build script —
     emitted on the wire as `config.palette` and applied as inline
     CSS variables on the ReactFlow wrapper.
  3. **Plugin `theme.css` `:host { --voitta-flow-node-*: ... }`** —
     host-wide override; loses to (1) and (2) but wins over (4).
  4. **Bare CSS defaults** in `frontend/src/theme.css` — light card.

The CSS rule `.flow-node__body { background: inherit }` makes (1)
paint the body directly without per-rule overrides — set
`style={"background": "#7f1d1d"}` on a step and the body turns red.

### Tone palette (independent of `palette`)

```
--voitta-flow-tone-{default|info|success|warning|critical}-bg
                                                          -fg
                                                          -border
--voitta-flow-edge-{default|info|success|warning|critical}
```

Tones drive the **title bar** of each node + the **outgoing edge
accent**, independent of the body palette. So a `tone="critical"`
node with `style={"background": "#0f172a"}` paints amber-on-red
title + nearly-black body. Plugins can override the tone tokens
in their `theme.css` like any other variable.

```css
:host {
  --voitta-flow-tone-info-bg:     #003e7e;
  --voitta-flow-tone-info-border: #4ea0ff;
  --voitta-flow-edge-info:        #4ea0ff;
}
```

That's the entire theming surface. No `apply_theme` walk-and-attach,
no shadow-root stylesheet machinery, no `.react-flow.dark`
auto-rebind — flow reports live in the widget's own shadow DOM where
plain CSS variables already inherit correctly.

## Layout engines

| Engine | Bundle | Layout quality | When to use |
|---|---|---|---|
| `elk` (default) | ~600 KB raw | Sugiyama layered, orthogonal edge routing, handles long labels well | Engineering / process diagrams; anything where edge crossings matter |
| `dagre`         | ~80 KB raw  | Layered, faster, cruder edge routing | Huge graphs (50+ nodes) where ELK feels slow |

Both produce the same output shape, so swapping is a one-line change:
`p.layout(engine="dagre")`.

## When something goes wrong

| Symptom | Likely cause | Where to look |
|---|---|---|
| Tool returns `status: "errored"`, source: `"server:builder"` | `build(ctx)` raised — bad reference, missing branch, invalid CSS in `style=`. | `errors[0].message`. Fix and re-call. |
| `style` validation rejected | Used a layout-affecting property (`position`, `transform`, etc.) or a value containing `url(...)`. | Drop the property or use a different visual approach via tones / icons. |
| Tool returns `status: "timeout"` | Layout took longer than `wait_s` (default 3s) or the user has no chat pane open. | Try increasing `wait_s`, or check for >100 nodes. |
| Unknown icon name | Spelling mistake — the icon falls back to the step-type default silently. | Check the curated list in `flow-nodes/icons.tsx`. |
| Node body colour is wrong on dark plugin theme | Forgot to call `p.palette("dark")` — flow renders with the light-card default regardless of the host. | Call `p.palette("dark")` at the top of `build(ctx)`. Or override per-node via `style={"background": ...}`. |
| Edges look thin / too pale on dark theme | `--voitta-flow-edge-default` falls back to `--voitta-border` which a dark plugin may have set very subtle. | Override `--voitta-flow-edge-default` to a more visible value. |

Errors that fire after `show_flow_report` returns post to
`/api/report-render-events` and surface via
`get_flow_render_errors(report_id)` — same channel as holoviz.

## What flow reports don't do

- **Interactive editing**. No node-drag, no port-drag-to-connect, no
  live state. Edits happen via `edit_flow_report` → re-render.
- **Dynamic data**. The script runs once at show time; no Bokeh
  session, no callbacks. For live data use HoloViz reports.
- **Multi-pane**. There is one report slot. Showing a flow replaces
  any open holoviz report (and vice versa).

## Known caveats

- **Bundle size.** ReactFlow + ELK + dagre bring the widget bundle to
  ~1.5 MB gzipped (was ~400 KB). If host pages are bandwidth-
  constrained this matters. A future optimization is lazy-loading
  the flow code as a separate chunk — IIFE bundle constraints make
  this non-trivial but feasible.
- **`@xyflow/react` is React.** We alias `react` / `react-dom` to
  `preact/compat` in `vite.config.ts`. If we hit a ReactFlow compat
  bug, flip those aliases to point at real React (~140 KB extra).
- **ELK is slow on huge graphs** (50+ nodes). Switch to dagre via
  `p.layout(engine="dagre")` if you see >2s layout times.
