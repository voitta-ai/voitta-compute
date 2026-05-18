# Panel report — theming to match the host page

The report iframe has no inherited CSS from the host page. To make a report look at home next to the chat drawer, you apply the active theme inside `build(ctx)`.

## The one-line default

```python
def build(ctx):
    layout = pn.Column(
        pn.pane.Markdown("# Title"),
        # ... rest of content ...
    )
    return ctx.apply_theme(layout, host="enterprise.voitta.ai")
```

`host` is the page hostname. Extract it from the `(current url: …)` prefix the orchestrator injects into the user's message. If no plugin matches that host, the call quietly falls back to the bare Voitta defaults — never raises.

That single call themes every Panel surface in the layout: body background, Markdown headings + text + links + code, Card containers, Tabulator chrome, inputs, buttons, tabs. **For a polished report, this is usually all you need.**

## What `apply_theme` does NOT cover (you still own these)

- **Matplotlib pixel content** (`figure.facecolor`, `axes.edgecolor`, line colours). Pull from `ctx.get_theme(host=…)["palette"]` and set `plt.rcParams` before plotting.
- **Plotly figure colours** (`paper_bgcolor`, `plot_bgcolor`, `font_color`). Same source; pass to `fig.update_layout(...)`.
- **Three.js scene background and material colours.** See [panel-three-scene.md](panel-three-scene.md); pass `bg=` to `ctx.three_scene`.
- **Inline `style="…"` attributes** on HTML you wrote. Inline styles beat stylesheets in the cascade.

## Pattern: palette-aware matplotlib

```python
def build(ctx):
    theme = ctx.get_theme(host="enterprise.voitta.ai")
    p = theme["palette"]

    plt.rcParams.update({
        "figure.facecolor":  p["surfaces"]["bg"],
        "axes.facecolor":    p["surfaces"]["surface"],
        "axes.edgecolor":    p["text"]["text"],
        "axes.labelcolor":   p["text"]["text"],
        "text.color":        p["text"]["text"],
        "xtick.color":       p["text"]["text-muted"],
        "ytick.color":       p["text"]["text-muted"],
        "grid.color":        p["surfaces"]["divider"],
    })

    fig, ax = plt.subplots(figsize=(8, 4))
    ax.plot(df["x"], df["y"], color=p["accent"]["accent"])
    img = fig_to_png(fig); plt.close(fig)

    return ctx.apply_theme(pn.Column(img), theme=theme)   # reuse the dict — no second filesystem read
```

## Pattern: palette-aware Plotly

```python
def build(ctx):
    theme = ctx.get_theme(host="enterprise.voitta.ai")
    p = theme["palette"]

    fig = go.Figure(...)
    fig.update_layout(
        template=("plotly_dark" if theme["is_dark"] else "plotly_white"),
        paper_bgcolor=p["surfaces"]["bg"],
        plot_bgcolor=p["surfaces"]["surface"],
        font_color=p["text"]["text"],
    )
    return ctx.apply_theme(pn.Column(pn.pane.Plotly(fig)), theme=theme)
```

## Pattern: one-off CSS overrides

For rules `apply_theme` doesn't cover (highlight a Tabulator column, italicize one heading), inject into the iframe `<head>`:

```python
def build(ctx):
    ctx.add_css("""
        .bk-Markdown h1 { font-style: italic; }
        .tabulator-col[tabulator-field="price"] { background: #ffd; }
    """)
    layout = pn.Column(...)
    return ctx.apply_theme(layout, host="enterprise.voitta.ai")
```

**Don't** use `pn.pane.HTML("<style>…</style>")` — Panel entity-encodes the text and the rules become literal characters in a `<div>`, styling nothing. Always `ctx.add_css(...)`.

## Pattern: override individual tokens

When the active theme is mostly right but a few colours need tweaking for one report:

```python
return ctx.apply_theme(
    layout,
    host="enterprise.voitta.ai",
    overrides={
        "--voitta-accent":    "#ff8800",
        "--voitta-link-fg":   "#ff8800",
        "--voitta-font-sans": '"Inter", system-ui, sans-serif',
    },
)
```

Keys must start with `--`. Values must be non-empty strings.

## Theme dict shape — what `ctx.get_theme` returns

```python
{
    "ok": True,
    "plugin": "voitta-enterprise",
    "host": "enterprise.voitta.ai",
    "is_dark": True,
    "palette": {
        "surfaces":  {"bg": "#1d1d1f", "surface": "#2c2c2e", "border": "#3a3a3c", "divider": "#48484a"},
        "text":      {"text": "#f5f5f7", "text-muted": "#a1a1a6", "text-faint": "#636366"},
        "accent":    {"accent": "#0a84ff", "accent-hover": "#409cff", "accent-fg": "#ffffff"},
        "fonts":     {"font-sans": "-apple-system, ..."},
        # ... more categories: header, code, status, ...
    },
    "raw_tokens": { ... },        # flat name→value map
    "css_snippet": ":host { ... }",  # ready-to-paste block
}
```

The same dict is returned by the `get_active_theme` MCP tool when the LLM wants to inspect outside a script.
