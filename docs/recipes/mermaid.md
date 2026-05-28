# Recipe: Mermaid diagrams

Mermaid renders flowcharts, sequence diagrams, ER diagrams, Gantt charts, and more from a text DSL.

## Basic pattern

```python
def build(ctx):
    diagram = """
flowchart TD
    A[Start] --> B{Decision}
    B -- Yes --> C[Do thing]
    B -- No --> D[Skip]
    C --> E[End]
    D --> E
"""
    return f"""<!DOCTYPE html>
<html>
<head>
<style>
  body {{ margin: 0; padding: 16px; background: #fff; }}
  .mermaid {{ max-width: 100%; }}
</style>
</head>
<body>
<div class="mermaid">{diagram}</div>
<script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>
<script>
  mermaid.initialize({{ startOnLoad: true, theme: 'default' }});
</script>
</body>
</html>"""
```

## Diagram types

```
flowchart TD    — top-down flowchart
flowchart LR    — left-right flowchart
sequenceDiagram — sequence diagram
erDiagram       — entity-relationship
gantt           — Gantt chart
classDiagram    — class diagram
stateDiagram-v2 — state machine
pie             — pie chart
```

## Theming

```python
def build(ctx):
    t = ctx.theme()
    bg = t.get("--voitta-bg", "#ffffff")
    # Mermaid themes: default, dark, forest, neutral, base
    mermaid_theme = "dark" if bg.startswith("#1") or bg.startswith("#0") else "default"

    diagram = """
sequenceDiagram
    User->>Agent: Send message
    Agent->>LLM: Stream request
    LLM-->>Agent: Token stream
    Agent-->>User: Chat response
"""
    return f"""<!DOCTYPE html>
<html>
<head><style>body{{margin:0;padding:16px;background:{bg}}}.mermaid{{max-width:100%}}</style></head>
<body>
<div class="mermaid">{diagram}</div>
<script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>
<script>mermaid.initialize({{startOnLoad:true,theme:'{mermaid_theme}'}})</script>
</body></html>"""
```

## Multiple diagrams

```python
def build(ctx):
    diagrams = [
        ("Architecture", "flowchart LR\n  A --> B --> C"),
        ("Sequence",     "sequenceDiagram\n  A->>B: hello\n  B-->>A: hi"),
    ]
    blocks = "".join(
        f"<h3>{title}</h3><div class='mermaid'>{src}</div>"
        for title, src in diagrams
    )
    return f"""<!DOCTYPE html>
<html><head><style>body{{margin:0;padding:16px;font-family:sans-serif}}</style></head>
<body>
{blocks}
<script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>
<script>mermaid.initialize({{startOnLoad:true}})</script>
</body></html>"""
```

## Notes

- Mermaid renders client-side in the iframe; there's a brief flash of the raw text before it renders.
- For screenshots, the render completes before capture because the shim waits for `window.load` before signalling ready.
- Avoid `&` in diagram labels — escape as `&amp;` in HTML context.
