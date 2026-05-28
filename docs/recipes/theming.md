# Recipe: Theming via `ctx.theme()`

`ctx.theme()` returns a dict of CSS variable names → values from the active plugin's theme. Use it to style reports consistently with the surrounding UI.

## Getting tokens

```python
def build(ctx):
    t = ctx.theme()
    # t is a dict like:
    # {
    #   "--voitta-bg":      "#1a1a2e",
    #   "--voitta-text":    "#e2e8f0",
    #   "--voitta-accent":  "#7c3aed",
    #   "--voitta-border":  "#2d2d44",
    #   "--voitta-card":    "#16213e",
    #   ...
    # }
```

Keys depend on the active plugin. Always use `.get(key, fallback)` to handle missing tokens.

## Common tokens

| Token | Typical use |
|---|---|
| `--voitta-bg` | Page / report background |
| `--voitta-text` | Primary text color |
| `--voitta-accent` | Highlights, buttons, chart primary color |
| `--voitta-border` | Table borders, dividers |
| `--voitta-card` | Card / panel background |

## Inject into `<style>` as CSS variables

```python
def build(ctx):
    t = ctx.theme()
    css_vars = "".join(f"  {k}: {v};\n" for k, v in t.items())

    return f"""<!DOCTYPE html>
<html>
<head>
<style>
  :root {{
{css_vars}  }}
  body {{
    margin: 0;
    padding: 16px;
    background: var(--voitta-bg, #fff);
    color: var(--voitta-text, #111);
    font-family: sans-serif;
  }}
  h1 {{ color: var(--voitta-accent, #5b5fc7); }}
  .card {{
    background: var(--voitta-card, #f5f5f5);
    border: 1px solid var(--voitta-border, #e0e0e0);
    border-radius: 8px;
    padding: 16px;
  }}
</style>
</head>
<body>
  <h1>Themed Report</h1>
  <div class="card">Content here</div>
</body>
</html>"""
```

## Use tokens directly in Python (no CSS variables)

Some libraries (matplotlib, Plotly) need concrete color values, not CSS variable references:

```python
def build(ctx):
    t = ctx.theme()
    bg     = t.get("--voitta-bg",     "#ffffff")
    text   = t.get("--voitta-text",   "#111111")
    accent = t.get("--voitta-accent", "#5b5fc7")

    # Pass directly to matplotlib / Plotly / ELK SVG attributes
```

## Dark/light detection

```python
def build(ctx):
    t = ctx.theme()
    bg = t.get("--voitta-bg", "#ffffff")

    # Rough detection: if background is dark, use dark variant
    # (parse the hex and check luminance, or just heuristic)
    is_dark = bg.startswith("#1") or bg.startswith("#0") or bg in ("#000", "#111", "#222")
    mermaid_theme = "dark" if is_dark else "default"
    plotly_template = "plotly_dark" if is_dark else "plotly_white"
```

## When ctx.theme() returns empty

`ctx.theme()` returns `{}` when:
- No plugin matches the current host.
- The default plugin has no `static/theme.css`.

Always provide fallback values via `.get(key, fallback)`.
