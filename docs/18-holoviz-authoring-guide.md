# HoloViz report authoring guide (for the LLM)

This is the field guide for writing `build(ctx)` HoloViz Panel report
scripts that look like real engineering deliverables. Written for the
model to consult as a cheat sheet — short, prescriptive, full of small
patterns to copy.

For architectural reference (lifecycle, render-event plumbing, smoke
testing) see [07-report-scripts.md](07-report-scripts.md). For theming
internals (token cascade, shadow-DOM widgets) see
[15-theming-architecture.md](15-theming-architecture.md). For
flow-chart reports (the other report kind) see
[17-flow-authoring-guide.md](17-flow-authoring-guide.md).

This file is **how to author** — not how it works.

---

## 1. The script skeleton

Every HoloViz report is one Python file with one top-level function:

```python
def build(ctx):
    import panel as pn

    # 1. Theme / design (optional but recommended)
    ctx.set_design("material")              # widget chrome
    ctx.set_template_theme("dark")          # light/dark template scheme
    css = ctx.theme_css(host="enterprise.voitta.ai")  # our tokens

    # 2. Build content
    grid = pn.GridSpec(sizing_mode="stretch_both")
    grid[0, :2] = pn.pane.Markdown("# Title")
    grid[1, 0]  = pn.widgets.Tabulator(df, sizing_mode="stretch_width")
    grid[1, 1]  = pn.pane.Plotly(fig)

    # 3. Apply theme everywhere
    ctx.apply_theme(grid, host="enterprise.voitta.ai")

    # 4. Make resize work in editable mode (optional)
    ctx.fill_cards(grid)

    return grid    # NOT pn.template.* — the host wraps for you
```

`panel` is **not** auto-imported — `import panel as pn` at the top of
`build`. The full venv is available (pandas, numpy, matplotlib,
plotly, bokeh, holoviews, h5py, scipy, …).

---

## 2. `ctx` surface — every method, indexed

| Method | Purpose | When to use |
|---|---|---|
| `ctx.snapshot(handle)` | Get a python_storage snapshot record | Read snapshot metadata |
| `ctx.dataframe(handle)` | Load `curves.pkl` as DataFrame | Curve / table data |
| `ctx.raw(handle)` | Parse `raw.json` | Anything else snapshot-related |
| `ctx.add_css(css)` | Inject CSS into iframe `<head>` | Outer-doc widgets (Markdown, Card, Plotly) |
| `ctx.add_widget_stylesheets(widget, css)` | Per-widget stylesheets | Shadow-DOM widgets (single target) |
| `ctx.add_js(name, url)` | Inject `<script src="…">` into `<head>` | Three.js, D3, custom JS libs |
| `ctx.get_theme(host=)` | Active palette dict | Read colours from Python — `palette["surfaces"]["bg"]` |
| `ctx.theme_css(host=, overrides=)` | Theme as plain CSS string | Pass to `add_css` and/or `stylesheets=` |
| `ctx.apply_theme(layout, host=)` | One-call sugar: add_css + walk layout + attach to every shadow-DOM widget | **Default choice for theming** |
| `ctx.set_design(name)` | Panel widget chrome family | `material` / `bootstrap` / `fast` / `native` |
| `ctx.set_template_theme(name)` | Panel template scheme | `default` / `dark` |
| `ctx.fill_cards(layout)` | Promote `stretch_both` deep | Editable reports with resizable cards |
| `ctx.three_scene(scene_js, bg=)` | Interactive 3D pane | Three.js content in a sandboxed iframe |
| `ctx.log(*args)` | Debug log line | Surfaced as `log_lines` in tool result |

Methods that have **no effect** in report scripts (compute-only):
`ctx.text` / `ctx.image`. Reports emit a layout, not chat-bound items.

---

## 3. Three orthogonal theming axes

These layer cleanly. Use any combination:

```
┌─────────────────────┬─────────────────────────────────────────────┐
│ AXIS                │ WHAT IT CONTROLS                            │
├─────────────────────┼─────────────────────────────────────────────┤
│ ctx.set_design()    │ Panel widget chrome (Tabulator theme,       │
│                     │ slider styling, button look, Card padding)  │
├─────────────────────┼─────────────────────────────────────────────┤
│ ctx.set_template_   │ Template scheme — header / sidebar / Bokeh  │
│   theme()           │ figure backgrounds. light or dark.          │
├─────────────────────┼─────────────────────────────────────────────┤
│ ctx.apply_theme()   │ --voitta-* tokens overlaid on top — your    │
│                     │ palette, your typography, your accents.     │
└─────────────────────┴─────────────────────────────────────────────┘
```

### When to set each

| Situation | Design | Template theme | apply_theme |
|---|---|---|---|
| Quick chart, host doesn't care about polish | — | — | ✓ |
| Polished report on enterprise dark host | `material` | `dark` | ✓ |
| Mostly forms, sliders, buttons | `material` or `bootstrap` | — | ✓ |
| Heavy table report | `bootstrap` (Tabulator inherits bootstrap5 theme) | — | ✓ |
| Mostly Plotly / matplotlib (panes own their colours) | — | match host | partial |

Most reports want **all three set**. The minimum that's not embarrassing on a dark host: `set_template_theme("dark")` + `apply_theme(layout, host=…)`.

---

## 4. Theme CSS — what `apply_theme` actually does

```python
ctx.apply_theme(layout, host="enterprise.voitta.ai")
```

Equivalent to:

```python
css = ctx.theme_css(host="enterprise.voitta.ai")
ctx.add_css(css)                                          # outer doc
for w in walk(layout):
    if isinstance(w, (Tabulator, DataFrame, DatePicker,   # shadow root
                      DatetimePicker, DateRangePicker,
                      DatetimeRangePicker, Markdown,
                      HTML, Str)):
        w.stylesheets = [*w.stylesheets, css]
```

The CSS is a single string with a `:root, :host { … }` variable
block (so it works in both the outer document AND inside each
shadow root) plus surface-targeting rules. See
[15-theming-architecture.md](15-theming-architecture.md) for the
selector list and edge cases.

**Print what's injected** when debugging:

```python
ctx.log(ctx.theme_css(host="enterprise.voitta.ai"))
```

The log surfaces in the `show_holoviz_report` tool result.

---

## 5. Common patterns

### Tabulator with full theming

```python
import panel as pn
pn.extension("tabulator")   # required when using Tabulator

def build(ctx):
    df = ctx.dataframe(handle)
    ctx.set_design("bootstrap")            # gives Tabulator the bootstrap5 theme
    ctx.set_template_theme("dark")
    table = pn.widgets.Tabulator(
        df,
        sizing_mode="stretch_width",
        layout="fit_columns",
        pagination="local",
        page_size=20,
    )
    return ctx.apply_theme(
        pn.Column(pn.pane.Markdown("# Results"), table),
        host="enterprise.voitta.ai",
    )
```

### matplotlib (palette-aware)

```python
import matplotlib.pyplot as plt
import panel as pn

def build(ctx):
    theme = ctx.get_theme(host="enterprise.voitta.ai")
    p = theme["palette"]
    plt.rcParams.update({
        "figure.facecolor":  p["surfaces"]["bg"],
        "axes.facecolor":    p["surfaces"]["surface"],
        "axes.edgecolor":    p["surfaces"]["border"],
        "axes.labelcolor":   p["text"]["text"],
        "text.color":        p["text"]["text"],
        "xtick.color":       p["text"]["text-muted"],
        "ytick.color":       p["text"]["text-muted"],
    })
    fig, ax = plt.subplots(figsize=(8, 4))
    ax.plot(df["x"], df["y"], color=p["accent"]["accent"])
    plt.close(fig)
    return ctx.apply_theme(
        pn.Column(pn.pane.Markdown("# Plot"), pn.pane.Matplotlib(fig, tight=True)),
        host="enterprise.voitta.ai",
    )
```

### Plotly (palette-aware)

```python
import plotly.graph_objects as go
import panel as pn
pn.extension("plotly")

def build(ctx):
    theme = ctx.get_theme(host="enterprise.voitta.ai")
    fig = go.Figure(...)
    fig.update_layout(
        template="plotly_dark" if theme["is_dark"] else "plotly_white",
        paper_bgcolor=theme["palette"]["surfaces"]["bg"],
        plot_bgcolor=theme["palette"]["surfaces"]["surface"],
        font_color=theme["palette"]["text"]["text"],
    )
    return ctx.apply_theme(
        pn.Column(pn.pane.Plotly(fig, sizing_mode="stretch_width")),
        host="enterprise.voitta.ai",
    )
```

### GridSpec with resizable cards

```python
def build(ctx):
    grid = pn.GridSpec(sizing_mode="stretch_both", ncols=2, nrows=3)
    grid[0, :]  = pn.pane.Markdown("# Title", margin=(0, 10))
    grid[1, 0]  = pn.widgets.Tabulator(df1, sizing_mode="stretch_both")
    grid[1, 1]  = pn.pane.Plotly(fig1, sizing_mode="stretch_both")
    grid[2, :]  = pn.pane.Matplotlib(fig2, sizing_mode="stretch_both")
    # fill_cards walks the grid and flips inner panes to stretch_both
    # so editable-mode resize actually reflows the charts.
    ctx.fill_cards(grid)
    return ctx.apply_theme(grid, host="enterprise.voitta.ai")
```

### Single widget themed differently from the rest

```python
def build(ctx):
    main_table = pn.widgets.Tabulator(df_normal)
    warning_table = pn.widgets.Tabulator(df_anomalies)

    # Apply default theme to main_table via apply_theme, then a
    # different palette to warning_table only.
    layout = pn.Column(main_table, warning_table)
    ctx.apply_theme(layout, host="enterprise.voitta.ai")

    # Override just the warning_table with a critical-tone palette.
    warning_css = ctx.theme_css(
        host="enterprise.voitta.ai",
        overrides={
            "--voitta-surface": "#7f1d1d",  # red-900
            "--voitta-text":    "#fee2e2",
        },
    )
    ctx.add_widget_stylesheets(warning_table, warning_css)

    return layout
```

---

## 6. Return value rules

Return a **content layout**, never a `pn.template.*`:

| ✓ OK | ✗ NOT |
|---|---|
| `pn.Column(...)` | `pn.template.VanillaTemplate(main=[...])` |
| `pn.Row(...)` | `pn.template.MaterialTemplate(...)` |
| `pn.GridSpec(...)` | `pn.template.EditableTemplate(...)` |
| `pn.Card(...)` | any `pn.template.*` |
| `pn.Tabs(...)` | |
| any `pn.pane.*` | |

Returning a template throws `ListLike` validation errors at template-wrap time — the host already wraps your layout in EditableTemplate.

---

## 7. Editable mode — what changes

When the user toggles the edit (⇲) button, the report iframe reloads
with `?editable=true`. Panel's `EditableTemplate` activates Muuri
drag-resize on each top-level card.

For resize to reach the inner figure (instead of just stretching the
outer Column), you must call `ctx.fill_cards(layout)` before returning.
Without it, Panel's own `document_ready` only flips the top-level root —
inner figures keep their declared height and leave whitespace.

**Default**: skip `fill_cards` unless you expect the user to actually
resize cards. Most reports don't.

---

## 8. Designs reference

```python
ctx.set_design("material")   # Google Material — clean, modern, blue-ish
ctx.set_design("bootstrap")  # Bootstrap 5 — Tabulator gets bootstrap5 theme
ctx.set_design("fast")       # Microsoft Fluent UI Web Components
ctx.set_design("native")     # Browser defaults — least opinionated
```

Material is the safe default for "looks like a real product."
Bootstrap is the safe default if your report is table-heavy.
Native is for "I'll style every widget myself."

---

## 9. Common mistakes

| Mistake | Fix |
|---|---|
| Returning `pn.template.VanillaTemplate(...)` from build(ctx) | Return the content layout; the host wraps it. |
| `pn.pane.HTML('<style>…</style>')` to inject CSS | Doesn't work (Panel entity-encodes). Use `ctx.add_css`. |
| Tabulator looks unthemed after `ctx.add_css` | Tabulator is shadow-DOM. Use `ctx.apply_theme` or `ctx.add_widget_stylesheets`. |
| `pn.widgets.DataFrame` instead of `pn.widgets.Tabulator` | DataFrame has SlickGrid stylesheet races inside EditableTemplate. Use Tabulator. |
| matplotlib figure has white edges on dark theme | Set `plt.rcParams` from `ctx.get_theme()["palette"]` BEFORE plotting. |
| Card sizes are tiny | Set `sizing_mode="stretch_both"` on inner panes — or call `ctx.fill_cards(layout)`. |
| Mixing inline `style="…"` on `pn.pane.HTML` with theme overrides | Inline styles beat stylesheet rules in the cascade. Template via Python f-strings from `ctx.get_theme()`. |
| `pn.extension(...)` called inside build(ctx) | Not strictly wrong but expensive. Only call extension('tabulator', 'plotly', …) for the panes you actually use. |
| Theming a 3D scene via `ctx.add_css` | `ctx.three_scene` is a sandboxed iframe; CSS doesn't cross it. Pass `bg=` and template materials from Python. See `09-panel-threejs-reports.md`. |

---

## 10. Worked example — multi-pane dark-themed report

```python
def build(ctx):
    import panel as pn
    import matplotlib.pyplot as plt
    import plotly.graph_objects as go

    pn.extension("tabulator", "plotly")

    # 1. Theme stack
    ctx.set_design("material")
    ctx.set_template_theme("dark")
    theme = ctx.get_theme(host="enterprise.voitta.ai")
    p = theme["palette"]

    # 2. matplotlib (palette-aware before any plot)
    plt.rcParams.update({
        "figure.facecolor": p["surfaces"]["bg"],
        "axes.facecolor":   p["surfaces"]["surface"],
        "axes.edgecolor":   p["surfaces"]["border"],
        "axes.labelcolor":  p["text"]["text"],
        "text.color":       p["text"]["text"],
        "xtick.color":      p["text"]["text-muted"],
        "ytick.color":      p["text"]["text-muted"],
    })

    # 3. Load data
    df = ctx.dataframe(handle)

    # 4. Compose three views
    fig_mpl, ax = plt.subplots(figsize=(7, 3))
    ax.plot(df["x"], df["y1"], color=p["accent"]["accent"], label="series 1")
    ax.plot(df["x"], df["y2"], color=p["surfaces"]["text-muted"], label="series 2")
    ax.legend()
    plt.close(fig_mpl)

    fig_ply = go.Figure(go.Scatter(x=df["x"], y=df["y3"], mode="markers"))
    fig_ply.update_layout(
        template="plotly_dark",
        paper_bgcolor=p["surfaces"]["bg"],
        plot_bgcolor=p["surfaces"]["surface"],
        font_color=p["text"]["text"],
    )

    table = pn.widgets.Tabulator(
        df.head(50),
        sizing_mode="stretch_width",
        layout="fit_columns",
        pagination="local",
        page_size=10,
    )

    # 5. Layout
    layout = pn.Column(
        pn.pane.Markdown("# Sensor Run Analysis"),
        pn.Row(
            pn.pane.Matplotlib(fig_mpl, sizing_mode="stretch_width", tight=True),
            pn.pane.Plotly(fig_ply, sizing_mode="stretch_width"),
        ),
        pn.pane.Markdown("## Top 50 readings"),
        table,
        sizing_mode="stretch_width",
    )

    # 6. Apply theme to outer doc + every shadow-DOM widget
    return ctx.apply_theme(layout, host="enterprise.voitta.ai")
```

Demonstrates:

- `pn.extension(...)` for just the panes used
- Three theming axes set
- Palette-aware matplotlib and Plotly
- Tabulator with pagination
- Single `apply_theme` covers Markdown panes + Tabulator's shadow root
- No `pn.template.*` in the return (host wraps it)
- No magic — every step visible in the script

---

## 11. Validation behaviour

After persisting, `define_report` re-runs `build(ctx)` once as a
smoke test. Failures surface in the tool result as:

```json
{ "smoke_error": "<truncated traceback>",
  "hint": "<doc-section pointer when the pattern is recognised>" }
```

Common patterns the tool recognises and hints for:

- `ListLike` error → returned a `pn.template.*` (fix per §6)
- `stylesheets` error → shadow-DOM widget needs `apply_theme` or `add_widget_stylesheets`
- `SlickGrid` race → DataFrame in EditableTemplate; switch to Tabulator
- `RecursionError` → self-reference / circular include
- `no module named` → import failure

Other failures get the generic "rag_query 18-holoviz-authoring-guide and fix via edit_report_script" hint.

**Always read `smoke_error` and act on the hint BEFORE calling
`show_holoviz_report`** — otherwise the user sees a red error page
in the iframe.
