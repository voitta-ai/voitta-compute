# Recipe: Mermaid diagrams

Mermaid renders flow/sequence/state/Gantt/ER/class diagrams from
text. Good when you want a quick diagram without writing layout code.

## The full pattern

```python
def build(ctx):
    diagram = """
graph TD
    A[Source] --> B[Process]
    B --> C{Valid?}
    C -->|Yes| D[Save]
    C -->|No| E[Reject]
    D --> F[End]
    E --> F
"""
    return f"""<!doctype html>
<html>
<head>
  <script type="module">
    import mermaid from "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs";
    mermaid.initialize({{ startOnLoad: true, theme: "default" }});
  </script>
  <style>
    body {{ margin: 0; padding: 16px; font-family: system-ui; }}
    .mermaid {{ background: #fff; }}
  </style>
</head>
<body>
  <pre class="mermaid">{diagram}</pre>
</body>
</html>"""
```

## Themes

Mermaid ships `default`, `dark`, `forest`, `neutral`. Pick based
on the host palette:

```python
t = ctx.theme()
# Crude "is the bg dark?" check
is_dark = "0" in t.get("--voitta-bg", "#fff")[:3]
mermaid_theme = "dark" if is_dark else "default"
```

## Diagram types

```
graph TD       — flowchart top-down
graph LR       — flowchart left-right
sequenceDiagram
stateDiagram-v2
gantt
classDiagram
erDiagram
journey
pie
mindmap
timeline
```

## Multiple diagrams

```python
return f"""<!doctype html>
<html>
<head>
  <script type="module">
    import mermaid from "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs";
    mermaid.initialize({{ startOnLoad: true }});
  </script>
</head>
<body>
  <h2>Flow</h2>
  <pre class="mermaid">graph TD; A-->B</pre>
  <h2>Sequence</h2>
  <pre class="mermaid">sequenceDiagram; A->>B: hi; B->>A: hello</pre>
</body>
</html>"""
```

## When to use Mermaid vs ELK

- **Mermaid**: quick diagrams, you don't care about exact layout
  positioning, you want one of the diagram types Mermaid
  natively supports (sequence/gantt/state).
- **ELK** (see `elk.md`): you want orthogonal routing with
  port-side hints, you want custom-painted nodes (gradients,
  shadows, custom shapes), you want pixel control over layout.

Mermaid is faster to write. ELK is more customisable.
