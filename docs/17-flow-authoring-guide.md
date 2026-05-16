# Flow report authoring guide (for the LLM)

This is the field guide for writing `build(ctx)` flow scripts that
look like real engineering deliverables. It's written for the model
to consult as a cheat sheet — short, prescriptive, full of small
patterns to copy.

For the architectural reference (lifecycle, theming surface, error
plumbing), see [16-flow-reports.md](16-flow-reports.md). This file is
**how to author**, not how it works.

---

## 1. The script skeleton

Every flow report is one Python file with one top-level function:

```python
def build(ctx):
    p = FlowBuilder("Process Name", "One-line description")

    # diagram-level config (all optional)
    p.layout(direction="TB", engine="elk",
             edge_routing="orthogonal",   # ← true rectilinear routing
             node_spacing=60, layer_spacing=90,
             thoroughness=10)              # tune for cleaner layouts
    p.edge_style("smoothstep")              # fallback when ELK can't route
    p.edge_options(border_radius=8, offset=20, step_position=0.5)
    p.background("dots")
    p.palette("dark")          # node body colours; "light" default
    p.color_mode("auto")       # ReactFlow internal chrome; follows palette

    # steps
    p.trigger(...)
    p.activity(...)
    p.decision(...)
    p.activity(...)
    p.end(...)

    # connections (skip those auto-emitted by decisions)
    p.connect(from_id, to_id)

    return p   # FlowBuilder; the runtime calls .to_dict() for you
```

`FlowBuilder` is in scope automatically — no import. `ctx.get_theme()`
and `ctx.log()` are available; nothing else from `ctx`.

---

## 2. Step types — pick by semantics

| Type | When |
|---|---|
| `trigger`  | The flow's starting point. Always exactly one trigger per flow (mostly). |
| `activity` | An action / task / piece of work done by someone or something. |
| `decision` | A branching point — "if/else", "switch by priority", "pick handler". Requires `branches=[...]`. |
| `artifact` | A document, deliverable, or named data object that flows between activities. |
| `end`      | Terminal — the process is complete. Multiple `end`s are fine (one per outcome). |

If you're unsure: `activity`. The mistake people make is using
`activity` for things that are really `artifact`s. "Request Form" is
an artifact; "Validate Request Form" is an activity.

---

## 3. Tones — the semantic colour system

Five tones drive the title-bar colour and outgoing-edge accent:

| Tone | Use for | Title-bar colour |
|---|---|---|
| `default`  | Plain steps, neutral activities | slate |
| `info`     | Triggers, informational gates, validations | sky-cyan |
| `success`  | Successful outcomes, "happy path" terminal states | emerald |
| `warning`  | Steps with caveats, manual review, SLA risk | amber |
| `critical` | Error handling, escalations, rejection paths | red |

Conventions worth following:

- **The trigger step**: `tone="info"`. Sets the visual entry point.
- **A success end**: `tone="success"`. The "Complete" / "Approved" / "Delivered" outcome.
- **Critical / rejection ends**: `tone="critical"`. "Failed" / "Rejected" / "Escalated".
- **Decision nodes**: `tone="warning"` if the decision has stakes ("Approved?", "Priority?"). `tone="default"` for neutral routing.
- **Edges**: re-tone an edge with `p.connect(..., tone="success")` to color the success path through a flow — that's a powerful storytelling tool. Use sparingly.

```python
p.trigger("start", "Submit Request", tone="info")
p.activity("review", "Manual Review", tone="warning",
           note="Reviewer SLA: 48 hours")
p.activity("approve", "Auto-Approve", tone="default")
p.end("done", "Complete", tone="success")
p.end("rejected", "Rejected", tone="critical")
p.connect("review", "approve", tone="success")
p.connect("review", "rejected", tone="critical")
```

---

## 4. Icons — the lucide vocabulary

The default icon per step type is OK but bland. Picking the right
icon adds enormous specificity. Use kebab-case lucide names:

```python
icon="play"            # triggers, starts
icon="check"           # validations, approvals
icon="check-circle"    # successful completion
icon="x"               # rejections, terminations
icon="x-circle"        # failures
icon="alert-triangle"  # warnings, escalations
icon="alert-octagon"   # critical alerts
icon="git-branch"      # decisions, branching, routing
icon="git-merge"       # joining flows
icon="search"          # reviews, lookups, queries
icon="filter"          # filtering, sieving
icon="user" "users"    # human-driven steps
icon="user-check"      # approval by a person
icon="server"          # backend processing
icon="database"        # data storage / retrieval
icon="cloud"           # cloud / external service
icon="mail" "send"     # notification, dispatch
icon="inbox"           # receiving, queuing
icon="clock"           # delays, SLAs, scheduled
icon="calendar"        # scheduled / dated events
icon="dollar-sign"     # money, billing, pricing
icon="shield" "lock"   # security, auth
icon="key"             # credentials, access
icon="terminal"        # CLI, scripts, automation
icon="settings" "gear" # configuration
icon="refresh-cw"      # retries, loops
icon="repeat"          # repeated operations
icon="upload" "save"   # storing results
icon="file" "file-text"  # documents / artifacts
icon="folder" "archive"  # archival, storage
icon="package" "box"   # bundles, deliverables
icon="zap"             # fast operations, performance-critical
icon="trending-up"     # growth, escalation
icon="layers"          # multi-stage processing
icon="hash" "tag"      # identifiers, labels
```

Full curated list lives in
`frontend/src/components/flow-nodes/icons.tsx`. Unknown names fall
back to the step-type default silently — no error.

Inline SVG escape hatch (rare):

```python
icon={"svg": '<svg viewBox="0 0 24 24" stroke="currentColor" fill="none" '
             'stroke-width="2"><path d="M3 12l4 4 14-14"/></svg>'}
```

---

## 5. Badges, meta, and note — node body content

These three fields turn a one-line node into a rich engineering card.

### Badges — short status pills

```python
badges=[
    "Engineering",                                # plain string → tone="default"
    {"label": "P0", "tone": "critical"},
    {"label": "Manual", "tone": "warning"},
    {"label": "SLA 48h", "tone": "warning"},
]
```

Use for: priority levels (P0/P1/P2), team names, SLAs, status flags.
Keep labels short (≤ 8 chars ideal, ≤ 16 char hard cap before they
start looking wrong).

### Meta — key/value rows

```python
meta=[
    ("Input",  "Request Form"),
    ("Output", "Decision Object"),
    ("Owner",  "platform-team"),
    ("Action", "Schema check"),
    ("SLA",    "< 100 ms"),
]
```

Use for: structured attributes of a step. Anything you'd write as a
column in a process spec.

### Note — italic annotation

```python
note="Skips review if amount < $1,000. Reviewer SLA: 48 hours."
```

Use for: caveats, exceptions, important conditions. ONE per node.
Italicized, muted color — visually subordinate to the rest.

### Roles / artifacts_in / artifacts_out — inferred meta

These render automatically as meta rows with appropriate icons:

```python
p.activity("review", "Review Request",
           roles=["Manager"],
           artifacts_in=["Request Form"],
           artifacts_out=["Decision Object"])
```

Renders as:
```
ROLES  : Manager
IN     : Request Form
OUT    : Decision Object
```

Use this BEFORE `meta=[...]` — same visual real estate, but with
process semantics the host can introspect. Reserve `meta=[...]` for
data that doesn't fit `roles` / `artifacts_*`.

---

## 6. Decision nodes — picking the right shape

This is the highest-leverage authoring decision. Decisions have FOUR
shapes; the wrong one looks like crap. Use this table:

### `shape="rect"` (default)

```
┌─ DECIDE      DECISION ─┐
│ ◆ Approved?            │
└──┬───────────────────┬─┘
   │ Yes               │ No
   ▼                   ▼
[approve]          [reject]
```

**Use when**: 2–3 branches, short labels (≤ 6 chars).
**Avoid when**: 4+ branches (labels collide), long branch labels.

### `shape="port"` (schematic multi-port) — **best for fan-out**

```
┌─ ROUTE          DECISION ─┐
│ Route by Priority         │
├───────────────────────────┤
│   Critical            ●───┼──► [critical_handler]
│   High                ●───┼──► [high_handler]
│   Medium              ●───┼──► [medium_handler]
│   Low                 ●───┼──► [low_handler]
│   Deferred            ●───┼──► [defer_handler]
└───────────────────────────┘
```

**Use when**: 4+ branches, OR branches with descriptive labels (>6 chars).
**Sweet spot**: priority routing, switch-case dispatchers, "pick the handler" flows.
**Visual reading**: IC datasheet pinout. Most engineering of all four.

### `shape="diamond"` (BPMN rhombus)

```
       ╱╲
      ╱  ╲
   ◆ Approved? ◆
      ╲  ╱
       ╲╱
   Yes     No
   ↓       ↓
```

**Use when**: 2-branch yes/no, and the *shape* itself should signal "question".
**Avoid when**: 3+ branches (gets cramped), branches with non-yes/no semantics.
**Visual reading**: classic flowchart.

### `shape="junction"` (tiny routing dot)

```
        │
        ●  Route by Priority
       ╱│╲╲
   Crit│Hi Med Low Def
       ▼ ▼  ▼  ▼  ▼
```

**Use when**: many branches (5+) AND the branch labels carry the entire meaning AND the decision itself has no other content (no roles, no meta, no note — these are REJECTED by the validator for junction shape).
**Avoid when**: you want to attach context (roles, meta, etc.) to the decision itself.
**Visual reading**: electrical-junction symbol. Pure routing.

### Decision rule (just follow this)

```
                          ┌─────────────────────┐
   How many branches?     │   Branch labels     │
                          │   > 6 chars long?   │
                          │  ┌──────────┬─────┐ │
                          │  │   Yes    │ No  │ │
                          │  ├──────────┼─────┤ │
   2     → diamond OR     │  │   port   │rect │ │
   3     → rect OR port   │  │   port   │rect │ │
   4–6   → port           │  │   port   │port │ │
   7+    → port OR junction  port-if-context,
                              junction-if-pure-routing
```

When in doubt: **port**. It scales, it looks the most engineering-like,
it never produces label collisions.

---

## 6.5. Edge customization — markers, animation, per-edge tuning

Beyond `style="solid|dashed"` and `tone=`, each connection now
accepts:

```python
p.connect("review", "approve",
          tone="success",
          marker="arrow-closed",  # default — filled triangle
          animated=True,          # marching-ants animation
          border_radius=12)       # per-edge corner softness
```

**`marker`** — arrowhead style:

| Value | Look | Use for |
|---|---|---|
| `"arrow-closed"` (default) | ▶ filled triangle | most edges; clear direction |
| `"arrow"` | > open V | lighter, less assertive — good when many edges converge on one target |
| `"none"` | no arrowhead | edges where direction is obvious from layout |

**`animated`** — when `True`, ReactFlow renders a marching-ants
animation along the path. Use sparingly to highlight a SINGLE
active path through the flow (e.g. "the happy success path" or "a
currently-executing branch"). Animating every edge is noise.

**`border_radius`** — overrides the diagram-level
`edge_options(border_radius=…)` for this one edge. Useful when one
particular connection needs sharper or softer corners than the rest.

### Diagram-level edge tuning — `edge_options()`

```python
p.edge_options(
    border_radius=8,    # corner softness on smoothstep edges (px)
    offset=20,          # distance before first turn (px)
    step_position=0.5,  # 0..1 — where the trunk bend happens (0.5 = midpoint)
)
```

Lower `step_position` (e.g. 0.2) makes the trunk turn close to the
source — useful for fan-out from a single hub. Higher (e.g. 0.8) puts
the turn near the target — useful when many edges converge on one node.

## 6.6. `palette` — node body colours (THE main control)

```python
p.palette("light")   # default — light cards
p.palette("dark")    # dark cards
```

Picks the **node-body palette** for this diagram: background, text,
muted text, faint text, border. The selected preset ships verbatim
on the wire under `config.palette` — five keys, fully visible:

```python
ctx.log(p.to_dict()["process"]["config"]["palette"])
# {"node_bg": "#1e293b", "node_fg": "#e2e8f0",
#  "node_fg_muted": "#94a3b8", "node_fg_faint": "#64748b",
#  "node_border": "#334155"}
```

Applied as inline CSS variables on the ReactFlow wrapper. **No
class-name automation, no media queries, no luminance probe**.
What you set is what renders.

### Override hierarchy (most-specific wins)

```
per-node style={"background": "#7f1d1d"}    ← LLM, wins everything
        │
diagram p.palette("dark")                   ← LLM, diagram default
        │
plugin theme.css :host {                    ← plugin author, host default
  --voitta-flow-node-bg: ...; }
        │
bare CSS-token default                      ← built-in light card
```

### Per-node override pattern

```python
p.activity("normal", "Standard step")  # uses palette default
p.activity("hot", "Critical Path",
           style={"background": "#7f1d1d", "color": "#fee2e2"})
p.activity("good", "Success",
           style={"background": "#064e3b"})
```

Tones (info / success / warning / critical) still control the title
bar — those are independent of the palette. So a node with
`tone="critical"` + `style={"background": "#7f1d1d"}` paints the
title bar amber-on-red and the body red — distinct but coordinated.

## 6.7. `color_mode` — ReactFlow internal chrome only

```python
p.color_mode("auto")     # follows palette name (default)
p.color_mode("light")    # force light
p.color_mode("dark")     # force dark
p.color_mode("system")   # follow OS prefers-color-scheme at runtime
```

Drives ReactFlow's INTERNAL `--xy-*` variable bundle — Controls
panel chrome, attribution badge, default edge stroke fallback,
minimap fill. **Does NOT** affect node bodies (that's `p.palette()`).

`"auto"` (default) picks `"dark"` when `p.palette("dark")` was
called, otherwise `"light"`. Usually you don't need to think about
this; setting `p.palette()` is enough.

## 7. Title block — the polish touch

Drop this in for an engineering-drawing flair:

```python
p.title_block(drawing_id="APR-001", rev="B", author="voitta")
```

Renders as a small monospaced corner block. Treat the rev as the
semantic version of the *process* (B = second iteration). Treat
the drawing ID as a stable handle the user can reference.

Use a title block when:
- The user is reviewing the flow as a deliverable
- The flow is one of several related diagrams
- You want it to feel like a real engineering artifact (which is
  often — that's the whole brief)

Skip it when:
- The flow is a quick illustration mid-conversation
- It would compete with the chart for visual attention

---

## 8. Layout direction

```python
p.layout(direction="TB")   # top-down — default; reads like a recipe
p.layout(direction="LR")   # left-right — reads like a timeline
```

- **TB**: process docs, approval flows, anything with "steps".
- **LR**: timelines, pipelines, data flows, port-shape decisions with many outputs (the right-side handles look cleanest with LR).

Avoid `BT` / `RL` unless you have a specific reason — they're
unfamiliar.

---

## 9. Layout engine

```python
p.layout(engine="elk")     # default — better edge routing, ~600 KB
p.layout(engine="dagre")   # smaller, faster, cruder routing
```

Switch to `dagre` when:
- The graph has 30+ nodes and ELK feels slow
- The user explicitly asks for "lighter / faster"

Otherwise: ELK.

---

## 9.5. Layout tuning — edge routing & spacing

ELK (the default engine) exposes several knobs that change layout
quality dramatically. All are kwargs on `p.layout()`:

```python
p.layout(
    engine="elk",
    edge_routing="orthogonal",            # true rectilinear (default)
    node_spacing=60,                       # px between sibling nodes
    layer_spacing=90,                      # px between layers
    crossing_minimization="LAYER_SWEEP",   # LAYER_SWEEP | INTERACTIVE | NONE
    node_placement="NETWORK_SIMPLEX",      # NETWORK_SIMPLEX | BRANDES_KOEPF
                                           # | LINEAR_SEGMENTS | SIMPLE
    thoroughness=7,                        # 1..100; higher = better, slower
    elk_options={"elk.layered.cycleBreaking.strategy": "DEPTH_FIRST"},
)
```

### `edge_routing` — the single biggest quality knob

| Value | What it does |
|---|---|
| `"orthogonal"` (default) | True rectilinear routing. ELK computes N-bend polylines that avoid crossings. Renders as a `<path>` with hard 90° corners — the schematic look. **Use this for engineering diagrams.** |
| `"polyline"` | Polyline with no orthogonal constraint — diagonal segments allowed. Looks like a sketch. |
| `"splines"` | Smooth curves through nodes. Looks like a designer's flowchart, not engineering. |

When `edge_routing="orthogonal"`, each edge is rendered by the
`OrthogonalEdge` component using ELK's computed bend points — NOT by
ReactFlow's bundled `getSmoothStepPath` (which is a 2-bend
approximation, not a real router). With the other options ELK still
runs, but we fall back to ReactFlow's smoothstep for the visual.

### `node_spacing` / `layer_spacing`

Bigger numbers = more space for the router to bend without crossings.
Defaults (60 / 90) are tight; bump to ~80 / 110 if your diagram has
many edges and looks cramped. Going below the defaults often
produces overlapping edges — ELK can't route inside a wall of nodes.

### `crossing_minimization` / `node_placement` / `thoroughness`

The ELK algorithm tuning trio. Worth touching when:

- **Lots of edge crossings**: increase `thoroughness` to 15–25.
- **Layout feels "loose"**: switch `node_placement` to `BRANDES_KOEPF`
  (tighter).
- **User dragged nodes manually and you want to keep their order**:
  switch `crossing_minimization` to `INTERACTIVE`.

### `elk_options` — the escape hatch

Any ELK config not exposed as a typed kwarg goes here:

```python
p.layout(elk_options={
    "elk.layered.cycleBreaking.strategy": "DEPTH_FIRST",
    "elk.layered.feedbackEdges": "true",
    "elk.layered.mergeEdges": "true",
})
```

Full reference: https://eclipse.dev/elk/reference/options.html

### Per-edge handle hints

When ELK's auto-routing picks an awkward side for a particular edge
(common with back-edges / retry loops), pin it manually:

```python
p.connect("retry", "decision",
          source_side="bottom",   # retry exits its bottom
          target_side="left")     # enters decision's left

p.connect("order", "decision",
          source_side="bottom",
          target_side="top")      # canonical top-to-bottom flow
```

Most diagrams never need this — the auto-routing is good. Reach for
side hints when you can see a specific edge taking a bad route.

---

## 10. Composing — a worked example

A complete CI/CD release flow, demonstrating every element:

```python
def build(ctx):
    p = FlowBuilder("Release Pipeline", "Code → production deploy")
    p.layout(direction="LR", engine="elk")
    p.title_block(drawing_id="REL-002", rev="C", author="platform-team")

    # ── entry ─────────────────────────────────────────────────────
    p.trigger("merge", "PR Merged to main",
              icon="git-merge", tone="info",
              badges=["main branch"],
              roles=["GitHub"])

    # ── build & test stage ────────────────────────────────────────
    p.activity("build", "Build Artifacts",
               icon="package", tone="default",
               meta=[("Output", "Docker images"), ("SLA", "< 5 min")])

    p.activity("test", "Run Test Suite",
               icon="check-circle", tone="default",
               badges=[{"label": "unit", "tone": "info"},
                       {"label": "integration", "tone": "warning"}],
               note="Flaky tests retry up to 3×")

    # ── decision: gate on test outcome ────────────────────────────
    p.decision("test_gate", "Tests passed?",
               shape="diamond", icon="git-branch", tone="warning",
               branches=[("Yes", "stage_deploy"),
                         ("No",  "notify_fail")])

    p.end("notify_fail", "Build Failed",
          icon="x-circle", tone="critical",
          artifacts_out=["Slack notification", "GitHub status"])

    # ── staging deploy ────────────────────────────────────────────
    p.activity("stage_deploy", "Deploy to Staging",
               icon="upload", tone="default",
               roles=["ArgoCD"],
               meta=[("Target", "staging cluster")])

    p.activity("smoke", "Smoke Tests",
               icon="zap", tone="default",
               badges=["< 2 min"])

    # ── decision: route by deploy strategy ───────────────────────
    p.decision("strategy", "Deploy Strategy",
               shape="port", icon="git-branch", tone="warning",
               branches=[
                   ("Canary",      "canary"),
                   ("Blue/Green",  "blue_green"),
                   ("Rolling",     "rolling"),
                   ("Big Bang",    "big_bang"),
               ])

    p.activity("canary",     "Canary 10% → 50% → 100%", icon="trending-up", tone="info")
    p.activity("blue_green", "Blue/Green switch",       icon="repeat",      tone="info")
    p.activity("rolling",    "Rolling restart",         icon="refresh-cw",  tone="info")
    p.activity("big_bang",   "Big Bang deploy",         icon="zap",         tone="warning",
               note="Only for emergency hotfixes")

    p.end("live", "Live in Production",
          icon="check-circle", tone="success",
          artifacts_out=["Production traffic"])

    # ── edges ─────────────────────────────────────────────────────
    p.connect("merge",        "build")
    p.connect("build",        "test")
    p.connect("test",         "test_gate")
    p.connect("stage_deploy", "smoke")
    p.connect("smoke",        "strategy")
    p.connect("canary",     "live")
    p.connect("blue_green", "live")
    p.connect("rolling",    "live")
    p.connect("big_bang",   "live")
    # success-path coloring + marching-ants animation to draw the eye
    p.connect("test_gate", "stage_deploy", tone="success", animated=True)
    # the failure path stays static, no animation

    return p
```

Patterns demonstrated:

- LR layout for a pipeline
- Title block for deliverable-feel
- `info` tone for entry, `success` for the happy end, `critical` for the failure end
- `diamond` shape for the binary test gate
- `port` shape for the 4-way deploy-strategy decision
- Mixed badge tones to distinguish test types
- Notes for caveats ("Flaky tests retry…", "Only for emergency hotfixes")
- `roles=` for "who/what does this" (GitHub, ArgoCD)
- `meta=` for structured attributes (Output, SLA, Target)
- `artifacts_out=` for things the step produces

---

## 11. Common mistakes

| Mistake | Fix |
|---|---|
| Using `shape="rect"` for a 5+ branch decision | Switch to `shape="port"`. |
| Hex colors in `style=` instead of tones | Use `tone=`. Stays themed; respects plugin re-skins. |
| Badge label > 12 chars | Truncate or move to `meta=` as a key/value row. |
| `note` longer than one sentence | Split into `meta=[("Note", "…")]` if it has a key. Notes that span lines look like buried paragraphs. |
| Same `icon` on every node | Each step type has a clear default. Override only when there's a specific signal to send. |
| Decision shape and content mismatch — `shape="junction"` with `roles=` | Validator rejects it. Switch to `rect` or `port`. |
| All `tone="default"` | The chart looks correct but boring. Pick out the happy path and the failure path with tones; even one tone-`success` and one tone-`critical` transforms readability. |
| Missing `p.connect(from, to)` between non-decision steps | Steps that aren't auto-connected by a decision MUST be explicitly connected. Forgetting this → orphaned nodes. |
| Calling `p.connect(decision, target)` when the decision already has `branches=[…]` for that target | Validator deduplicates, but the auto-emitted styling wins. Just don't. |
| Returning `p.to_dict()` AND `return p` — only one | Either works. Pick `return p` (shorter). |

---

## 12. Validation behavior

Errors are surfaced at build time with a complete report — fix all of
them at once, not one-by-one:

- **Duplicate step ID** → rename.
- **`connect()` source or target doesn't exist** → typo in step name.
- **Decision branch target doesn't exist** → typo in branch tuple.
- **Decision with < 2 branches** → add another, or make it an activity.
- **`shape="junction"` with roles/meta/note** → switch shape or strip those fields.
- **CSS in `style=` rejected** → you used `position` / `transform` / something layout-affecting. Use `tone=` or pick another visual property.
- **CSS value contains `url(…)`** → rejected for security; inline-encode or use a token.

Everything else is allowed. Be bold.
