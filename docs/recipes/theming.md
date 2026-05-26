# Recipe: Theming via `ctx.theme()`

`ctx.theme()` returns a dict of raw CSS-variable names → values
for the active plugin. Keys are real CSS variables like
`--voitta-bg`, `--voitta-accent`, `--voitta-flow-edge-success`.

## Embed all theme vars in CSS

```python
def build(ctx):
    t = ctx.theme()
    vars_block = "".join(f"  {k}: {v};\n" for k, v in t.items())
    return f"""<!doctype html>
<html>
<head><style>
  :root {{
{vars_block}  }}
  body {{ background: var(--voitta-bg); color: var(--voitta-text);
          font-family: system-ui; padding: 24px; margin: 0; }}
  h1 {{ color: var(--voitta-accent); }}
  .muted {{ color: var(--voitta-text-muted); }}
  .border {{ border: 1px solid var(--voitta-border); }}
</style></head>
<body>
  <h1>Themed report</h1>
  <p class="muted">Picks up host's plugin palette automatically.</p>
</body>
</html>"""
```

## Read individual values for matplotlib / plotly colors

```python
t = ctx.theme()
fg = t.get("--voitta-text", "#000")
bg = t.get("--voitta-bg", "#fff")
accent = t.get("--voitta-accent", "#0a84ff")
ok = t.get("--voitta-ok-fg", "#10b981")
warn = t.get("--voitta-warn-fg", "#f59e0b")

# Pass into matplotlib:
fig.patch.set_facecolor(bg)
ax.tick_params(colors=fg)
ax.plot(x, y, color=accent)
```

## Common variable names

Plugin themes vary, but most expose at least:

- `--voitta-bg` — page background
- `--voitta-surface` — card / panel background
- `--voitta-text` — primary text
- `--voitta-text-muted` — secondary text
- `--voitta-border` — neutral border
- `--voitta-accent` — primary highlight color
- `--voitta-link-fg` — link color
- `--voitta-ok-fg` — success / positive
- `--voitta-warn-fg` — warning / caution
- `--voitta-error-fg` — error / danger

Flow-chart specific (used by the design-template skins, see
`../elk-design-templates.md`):

- `--voitta-flow-edge-info`
- `--voitta-flow-edge-success`
- `--voitta-flow-edge-warning`
- `--voitta-flow-edge-critical`
- `--voitta-flow-node-bg`
- `--voitta-flow-node-fg`
- `--voitta-flow-node-border`

**Don't assume any specific variable exists.** Use `.get(key,
fallback)`. Different plugins ship different keys.

## Finding the full variable list for the host plugin

```python
def build(ctx):
    t = ctx.theme()
    rows = "".join(
        f'<tr><td><code>{k}</code></td>'
        f'<td><span class="swatch" style="background:{v}"></span></td>'
        f'<td><code>{v}</code></td></tr>'
        for k, v in sorted(t.items())
    )
    return f"""<!doctype html>
<html>
<head><style>
  body {{ font-family: system-ui; padding: 24px; }}
  table {{ border-collapse: collapse; }}
  td {{ padding: 6px 12px; border-bottom: 1px solid #eee; }}
  .swatch {{ display: inline-block; width: 24px; height: 24px;
            border: 1px solid #ccc; vertical-align: middle; }}
  code {{ font-family: ui-monospace, monospace; font-size: 12px; }}
</style></head>
<body>
  <h1>Theme tokens</h1>
  <table>{rows}</table>
</body>
</html>"""
```

Run this against your target host to see exactly what's
available.

## Falling back gracefully

`ctx.theme()` can return an empty dict if no plugin resolves
(no host, no fallback) — always `.get()` with a fallback color.
Or check upfront:

```python
t = ctx.theme()
if not t:
    # No theme available; use defaults.
    return "<!doctype html><html>...</html>"
```
