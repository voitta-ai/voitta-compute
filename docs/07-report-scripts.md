# Report scripts — Panel + matplotlib best practices

Report scripts are stored Python files of the form `def build(ctx) -> pn.viewable.Viewable`. The Panel-served route `/panel/reports?id=<slug>` calls `build(ctx)` once per browser session, returns the layout, and Bokeh serialises that document to the iframe.

> **For authoring guidance** — what to put inside `build(ctx)`, when to use which ctx helper, design / theme axes, common patterns, and the worked example — see [18-holoviz-authoring-guide.md](18-holoviz-authoring-guide.md). This file is the **infrastructure reference**: lifecycle, embedding rules, server-side error plumbing. See [01-architecture.md](01-architecture.md) for the bigger picture; this doc is just the matplotlib-embedding rules.

## Return a plain layout — NEVER a template

`build(ctx)` must return a **content layout** — typically `pn.Column`, `pn.Row`, `pn.GridBox`, or a single `pn.Card` / pane. The Panel host (`backend/app/services/panel_app.py::_wrap_template`) takes whatever you return and wraps it in an `EditableTemplate` / `VanillaTemplate` itself, attaches the iframe shim, hides the nav header, and applies your theme. Returning a template yourself nests a template inside a template — and Bokeh rejects the resulting `ListLike` at instantiation time with an error that looks like:

```
TypeError: Children parameter 'ListLike.objects' items must be instances of Panel, not str.
```

This is a **`source="server:template"` error**: it fires inside the Panel session handler *before* the iframe document ever loads, so the in-iframe `window.error` shim never sees it. The whole iframe shows Panel's bare 500 page. Until recently this also meant `show_holoviz_report` would time out with an empty `errors[]` — the LLM was flying blind. The current fix records server-side template failures into the same store the iframe shim writes to (`source="server:template"`), so `show_holoviz_report` wakes immediately with the traceback in hand. Even so, **the fix for the script** is to stop returning a template:

```python
# BAD — nests EditableTemplate(EditableTemplate(...)). Server:template error.
def build(ctx):
    layout = pn.Column(pn.pane.Markdown("# Title"), chart)
    return pn.template.EditableTemplate(main=[layout])   # ← don't

# BAD — same problem with VanillaTemplate, Bootstrap, FastList, Material, any pn.template.*
def build(ctx):
    return pn.template.VanillaTemplate(main=[chart])     # ← don't

# GOOD — return the content. The host wraps it.
def build(ctx):
    return ctx.apply_theme(
        pn.Column(
            pn.Card(chart,           title="Cost vs deck length"),
            pn.Card(summary_table,   title="Per-deck breakdown"),
            sizing_mode="stretch_width",
        ),
        host="enterprise.voitta.ai",
    )
```

Use `pn.Card(child, title="…")` to give a section a header — the host's `EditableTemplate` knows how to lay these out, drag/resize them in edit mode, and theme them. Don't try to recreate the chrome yourself.

## Picking the right rendering path

| The user asks for… | Use… | Why |
| --- | --- | --- |
| static chart, 2D plot, bar chart, scatter, line | `ctx.image(fig)` or eager `pn.pane.PNG(buf.getvalue())` | Fastest, smallest, deterministic. |
| table / DataFrame | `pn.widgets.Tabulator(df)` | See §Tables below. |
| **rotating 3D / interactive 3D / spinning 3D / 3D scatter / 3D surface / 3D anything** | **`ctx.three_scene(scene_js, height=…)`** — see [09-panel-threejs-reports.md](09-panel-threejs-reports.md) | Drag-to-rotate + wheel-zoom out of the box. **Don't render 3D as matplotlib GIFs / animations.** |
| WebGL / three.js / model viewer | `ctx.three_scene(...)` | Same helper, more complex `scene_js`. |
| geographic 3D map | `pn.pane.DeckGL` | Built into Panel. |
| named Plotly 3D (user said "Plotly") | `pn.pane.Plotly(fig)` | Only when the user explicitly asks. |

For anything that says "3D" + "rotating" / "interactive" / "spinning" / "draggable", default to `ctx.three_scene` — see [09-panel-threejs-reports.md](09-panel-threejs-reports.md). Matplotlib animations / `view_init` sweeps / GIF loops are the wrong answer.

## Theme the report to match the host page

Reports render in a separate iframe with no access to the widget's CSS custom properties. Without explicit theming, a white-background matplotlib plot or a default Markdown title looks broken against a dark enterprise portal. There are **two distinct layers** to colour, and you usually want both.

### Layer 1: the report chrome — `ctx.apply_theme(layout, host=…)`

One line at the end of `build(ctx)` themes EVERY surface around your content: the iframe body, the main content area, Markdown panes (headings + body text + links + code blocks), card containers, Tabulator tables, inputs, dividers, tabs, even the Plotly modebar. You don't have to chase Panel/Bokeh selectors yourself.

```python
def build(ctx):
    grid = pn.GridSpec(sizing_mode="stretch_width", height=600)
    grid[0, 0] = pn.pane.Markdown("## My report")
    grid[1, 0] = my_chart
    return ctx.apply_theme(grid, host="enterprise.voitta.ai")
```

`host` is the page hostname. The orchestrator injects the current URL as ambient context (see `(current url: …)` prefix on user messages); extract the hostname from it. If no plugin matches that host, the call quietly falls back to the bare Voitta defaults — never raises.

**What `apply_theme` actually does** (no hidden state — just a 4-line convenience):

```python
# Equivalent unsugared form:
css = ctx.theme_css(host="enterprise.voitta.ai")  # → CSS string
ctx.add_css(css)                                  # outer doc <head>
# walks the layout and for each Tabulator / DataTable / DatePicker / etc.:
#     widget.stylesheets = [*widget.stylesheets, css]
```

The unsugared form is useful when you want explicit control — e.g. one widget unthemed, or different overrides per widget. See [15-theming-architecture.md](15-theming-architecture.md) for why the two channels (outer doc vs per-widget `stylesheets=`) exist: outer-document CSS cannot pierce Bokeh's per-component shadow roots, so widgets like Tabulator need the CSS attached directly to their `stylesheets=[…]` list.

What `apply_theme` covers automatically:
- Document `<body>` background + text colour
- `#container` / `#main` / `#content` (Vanilla & Editable templates)
- `.bk-Markdown` / `.markdown-body` — headings, body, links, code, blockquote, table borders, hr
- `.bk-Card` / `.pn-card` containers and headers
- `.tabulator` (Tabulator widget) — header, rows, even-row stripe, hover, footer
- `<input>` / `<textarea>` / `<select>` chrome and focus rings
- `button.bk-btn` (default + `.bk-btn-primary`)
- `.bk-Tabs` active/inactive tab styling
- The Plotly modebar (transparent bg, themed icons)

What `apply_theme` does NOT cover (you still own these):
- Pixel content of matplotlib figures (rcParams — see Layer 2 below)
- Plotly layout config (`paper_bgcolor`, `plot_bgcolor`, `font_color`)
- Three.js scene `bg` and material colours (see `ctx.three_scene(bg=…)`)
- Any `pn.pane.HTML` you wrote with explicit inline `style="background:…"` attributes

### Layer 2: per-chart colours — `ctx.get_theme(host=…)`

`apply_theme` themes the chrome; the *contents* of your charts still need their own palette. `ctx.get_theme(host=…)` returns the same dict the `get_active_theme` LLM tool returns, but inside Python — no tool round-trip.

```python
def build(ctx):
    theme = ctx.get_theme(host="enterprise.voitta.ai")
    p = theme["palette"]

    # matplotlib — apply globally before plotting
    import matplotlib.pyplot as plt
    plt.rcParams.update({
        "figure.facecolor":  p["surfaces"]["bg"],
        "axes.facecolor":    p["surfaces"]["surface"],
        "axes.edgecolor":    p["text"]["text"],
        "axes.labelcolor":   p["text"]["text"],
        "text.color":        p["text"]["text"],
        "xtick.color":       p["text"]["text-muted"],
        "ytick.color":       p["text"]["text-muted"],
        "grid.color":        p["surfaces"]["divider"],
        "savefig.facecolor": p["surfaces"]["bg"],
    })
    # Plotly — pass colours into update_layout
    fig.update_layout(
        paper_bgcolor=p["surfaces"]["bg"],
        plot_bgcolor=p["surfaces"]["surface"],
        font_color=p["text"]["text"],
        font_family=p["fonts"]["font-sans"],
        template=("plotly_dark" if theme["is_dark"] else "plotly_white"),
    )
    # Three.js — pass surfaces.bg as the iframe bg
    scene = ctx.three_scene(scene_js, bg=p["surfaces"]["bg"])

    # ... assemble layout ...
    return ctx.apply_theme(layout, theme=theme)   # reuse the dict; no second filesystem read
```

`apply_theme(layout, theme=theme)` accepts a pre-fetched dict — pass the one you got from `get_theme` and avoid re-reading the CSS files. If you only need wrapper theming and don't touch chart colours, skip `get_theme` and use `apply_theme(layout, host=…)` directly.

### Override individual tokens — `apply_theme(layout, host=…, overrides={…})`

Sometimes the active theme is mostly right but one or two colours need tweaking — a brighter accent for a specific report, a custom link colour for a workflow, a different font for a presentation. Pass an `overrides` dict:

```python
def build(ctx):
    return ctx.apply_theme(
        layout,
        host="enterprise.voitta.ai",
        overrides={
            "--voitta-accent":    "#ff8800",   # orange CTAs / focused borders
            "--voitta-link-fg":   "#ff8800",   # matching link colour
            "--voitta-font-sans": '"Inter", system-ui, sans-serif',
        },
    )
```

Rules:
- Keys MUST start with `--` (CSS custom-property convention). Token names come from `frontend/src/theme.css` — any `--voitta-*` declaration there is valid. Bare names like `"accent"` raise `ValueError`.
- Values must be non-empty strings. Anything else raises.
- If you override `--voitta-bg`, `is_dark` is recomputed from the new value — so the wrapper's `color-scheme` and your `is_dark`-conditional logic stay consistent.

Common tokens worth overriding:

| Token | What it controls |
|---|---|
| `--voitta-bg` | Outer document background |
| `--voitta-surface` | Card / table / elevated-surface background |
| `--voitta-text` | Primary body text colour |
| `--voitta-text-muted` | Captions, axis labels, secondary text |
| `--voitta-accent` | CTAs, focused inputs, blockquote borders, active tabs |
| `--voitta-link-fg` | Anchor colour in Markdown |
| `--voitta-divider` | Separator lines, hr, table inner borders |
| `--voitta-border` | Outer table / card borders |
| `--voitta-font-sans` | Body font stack |
| `--voitta-font-display` | Headings / title font |
| `--voitta-radius` | Card / button corner radius |

For overrides that target ONE specific selector (e.g. "make just `<h1>` italic", "highlight one column in a Tabulator"), inject raw CSS into the iframe's `<head>` via `ctx.add_css(...)`:

```python
def build(ctx):
    ctx.add_css("""
        .bk-Markdown h1 { font-style: italic; }
        .tabulator-col[tabulator-field="price"] { background: #ffd; }
    """)
    return pn.Column(...)
```

⚠ **Don't use `pn.pane.HTML('<style>…')` for this.** Panel sanitises HTML panes — it entity-encodes the `<style>` text, so the browser sees the rules as literal text inside a `<div>`, not as a stylesheet. The rules never reach Tabulator's chrome, Bokeh's DataTable, or anything else owning a stylesheet in the outer document.

Empirically, an HTML-pane `<style>` shows up in the rendered DOM as:

```
<div ...>&lt;style&gt;.tabulator { … }&lt;/style&gt;</div>      <!-- ❌ literal text in a div -->
```

while `ctx.add_css(...)` lands the rules in the document head as a sibling of Panel's own stylesheets:

```
<head>
  …Panel's bundled CSS…
  <style type="text/css">.tabulator { … }</style>            <!-- ✅ real stylesheet -->
</head>
```

`ctx.add_css` is per-session (it appends to `template.config.raw_css`, not the process-global `pn.config.raw_css`), so concurrent sessions don't see each other's overrides.

If you find yourself writing the same `<style>` override across multiple reports, the wrapper has a gap — see [15-theming-architecture.md](15-theming-architecture.md) for the architecture, known limits, and how to report it so we can widen the wrapper's selector coverage. The goal is that **every surface in a report follows the active theme by default** — when one doesn't, that's our bug.

### Quick reference

| Need to do… | Call |
|---|---|
| Theme everything around the content (chrome, Markdown, tables) | `ctx.apply_theme(layout, host=…)` at end of `build` |
| Get the theme CSS as a string (e.g. to attach to one specific widget) | `ctx.theme_css(host=…)` → `str`. Then `widget = pn.widgets.Tabulator(df, stylesheets=[css])` for explicit per-widget theming |
| One-off selector override (Tabulator column, Markdown heading, etc.) | `ctx.add_css("…raw CSS…")` — lands in `<head>`, reaches outer-document widgets |
| Colour a matplotlib plot | Pull from `ctx.get_theme(host=…)["palette"]`, set `plt.rcParams` |
| Colour a Plotly figure | Same — pass into `fig.update_layout(...)` |
| Theme a Three.js scene's background | `ctx.three_scene(scene_js, bg=p["surfaces"]["bg"])` |
| Know if the active theme is dark for `plotly_dark` / `plotly_white` | `theme["is_dark"]` |
| Get a copy-paste `:host { … }` block to inject into a custom iframe | `theme["css_snippet"]` |
| Find the active plugin name / agent_name | `theme["plugin"]`, `theme["agent_name"]` |
| LLM-side inspection (without writing a script) | `get_active_theme` tool, same shape |

## Lock matplotlib to the Agg backend (precondition for everything below)

`build(ctx)` runs on a Bokeh server worker **thread**, not the main thread. `import matplotlib.pyplot as plt` without an explicit backend defaults to whatever matplotlib detects (on macOS that's the GUI `macosx` backend; on Linux desktops it's often Qt). Those backends refuse to initialise off the main thread and raise during the first figure creation — either inside `build(ctx)` (which the smoke test will catch) or, worse, later during a re-render in a different worker (which it won't).

Pin the backend **before** any pyplot import in the script, including transitive ones:

```python
# At the very top of the report script, before any other matplotlib touch:
import matplotlib
matplotlib.use("agg")     # headless raster — safe on any thread, no display required

import matplotlib.pyplot as plt
import panel as pn

pn.extension("tabulator")   # plus any other widget families you use
```

Why this is a separate rule, not just "use the OO API": even when you only touch `matplotlib.figure.Figure`, libraries you call (seaborn, pandas plotting, holoviews' matplotlib backend) re-enter pyplot internally. The backend is process-global, so the first `import matplotlib.pyplot` wins — set it explicitly first or you're at the mercy of whichever transitive import got there first.

This shows up as a `source="server:script"` error in `get_report_render_errors` when it triggers inside `build(ctx)`. If a downstream import flips the backend later (rare but possible with some seaborn versions), you can get a `source="window.error"` from the iframe instead — same root cause, different reporting path.

## The pyplot pitfall (read this first)

**Don't embed a live `matplotlib.figure.Figure` into a Panel layout via `pn.pane.Matplotlib(fig)` (or any wrapper that defers rendering).** It looks fine in dev, then fails in subtle ways the LLM never sees.

### Why it bites

The execution path has two distinct error stages:

1. **Build time** — `build(ctx)` runs, returns a Panel layout object. `define_report` / `edit_report_script` smoke-test this stage (`smoke_test_report` in [backend/app/services/scripts.py](../backend/app/services/scripts.py)). Any exception raised inside `build(ctx)` is captured, truncated, and surfaced back to the LLM via the tool result so it can fix the script.
2. **Render time** — when the user opens the iframe, Bokeh serialises the layout into the session document. If a `pn.pane.Matplotlib(fig)` is in the tree, this is when matplotlib actually rasterises. Any failure here (closed figure, agg-backend mismatch, non-picklable artist, mutated state from a prior run) raises **after** `build(ctx)` has already returned — past the smoke test, into the Bokeh server log. **The LLM never receives this error.** The user sees a broken iframe; the chat looks healthy.

So: a naive matplotlib pattern lets the LLM ship a "successful" report that breaks at view time, with no feedback loop.

### The fix — render eagerly inside `build(ctx)`

Convert each figure to PNG **bytes** while `build(ctx)` is still on the stack, then hand the bytes to `pn.pane.PNG`. Any matplotlib failure now raises during build, gets caught by the smoke test, and round-trips to the LLM as an editable error.

```python
from io import BytesIO
import matplotlib.pyplot as plt
import panel as pn


def fig_to_png_pane(fig):
    """Render a matplotlib Figure to a self-contained PNG Panel pane.

    Eager render: any matplotlib error raises here, inside build(ctx),
    where the smoke test will surface it back to the LLM.
    """
    buf = BytesIO()
    fig.savefig(buf, format="png", dpi=130, bbox_inches="tight")
    buf.seek(0)
    return pn.pane.PNG(buf.read(), sizing_mode="stretch_width")


def build(ctx):
    df = ctx.dataframe("...")  # snapshot handle

    fig_fr, ax = plt.subplots()
    ax.plot(df["t"], df["fr"])
    fr_img = fig_to_png_pane(fig_fr)
    plt.close(fig_fr)  # release the Figure — see "Memory" below

    return pn.Column(
        pn.pane.Markdown("## Frequency response"),
        fr_img,
        sizing_mode="stretch_width",
    )
```

Why each step matters:

| Step | What it gives you |
| ---- | ----------------- |
| `fig.savefig(buf, format="png")` | Forces matplotlib to rasterise NOW, in `build(ctx)`. No deferred work. |
| `buf.seek(0); buf.read()` | Yields raw PNG bytes. |
| `pn.pane.PNG(bytes, ...)` | Panel accepts raw bytes natively — no temp file, no URL, no `ctx`. |
| `plt.close(fig)` | Drops the Figure from `pyplot`'s registry so a long report doesn't grow memory linearly with the number of figures. |

This pattern is the **canonical way** to embed matplotlib output in a report script. Use it by default, even for one-off plots.

## What NOT to do (and why)

| Anti-pattern | Failure mode | Caught by smoke test? |
| ------------ | ------------ | --------------------- |
| Returning `pn.template.EditableTemplate(...)` / `VanillaTemplate(...)` / any `pn.template.*` from `build(ctx)` | Host nests it inside its own template → Bokeh `ListLike` validation throws at session init → `source="server:template"` | ❌ no — error fires in the Panel session, after `build` returned |
| Skipping `matplotlib.use("agg")` before `import matplotlib.pyplot as plt` | GUI backend (`macosx` / Qt) refuses off-main-thread init → first figure creation raises in the worker | ⚠️ sometimes — depends on which worker the smoke test ran on |
| `pn.pane.HTML("<style>…</style>")` for custom CSS | Panel entity-encodes the text — rules become literal characters in a `<div>`, never a real stylesheet → Tabulator / Bokeh widgets in the outer document keep their default theme | ✅ "no error" — silently wrong; use `ctx.add_css(...)` |
| `pn.pane.Matplotlib(fig)` left in the tree | Bokeh tries to serialise a live Figure at render time; mismatched backends or closed/mutated figures raise inside the Bokeh session | ❌ no |
| `plt.show()` inside `build(ctx)` | No-op in headless backend, or pops a window in the host's display, or hangs depending on backend | ❌ no |
| `fig.savefig("/tmp/foo.png")` then referencing the file by URL | Path leaks across sessions, breaks on cleanup, race conditions, no served route | ❌ no |
| Returning a `Figure` directly from `build(ctx)` | Panel doesn't know what to do with it; Bokeh serializer chokes | ❌ no — `build` returned successfully |
| Building the figure once at module scope and reusing it across `build(ctx)` calls | Each session mutates the same Figure; first session's render closes the canvas the second uses | ❌ no |

All of these *can* run cleanly the first time and fail later, after `build(ctx)` returns — past the smoke test's reach. `get_report_render_errors(report_id)` is the catch-all: it sees everything that fires post-build, regardless of layer.

## Memory hygiene

`pyplot` keeps every Figure alive in its global registry until you close it. In a long-running session that re-runs `build(ctx)` per page load, this accumulates. Two equivalent fixes:

```python
# Option A: explicit close after each conversion
img = fig_to_png_pane(fig)
plt.close(fig)

# Option B: use the OO API and skip pyplot's registry entirely
from matplotlib.figure import Figure
fig = Figure(figsize=(8, 4))
ax = fig.subplots()
ax.plot(...)
img = fig_to_png_pane(fig)  # no plt.close needed — fig isn't in pyplot's registry
```

Option B is cleaner for report scripts since you don't need `plt.show()` semantics anyway.

## Tables — use `pn.widgets.Tabulator`, never `pn.widgets.DataFrame`

`pn.widgets.DataFrame` is backed by Bokeh's SlickGrid `DataTable`. Inside an iframe-embedded Panel report it loses a race condition that doesn't reproduce in a notebook or standalone server:

```
Error rendering Bokeh items: Error: SlickGrid Cannot find stylesheet.
  at getColumnCssRules (bokeh-tables.min.js …)
  at applyColumnWidths
  at autosizeColumns
  at new SlickGrid
```

**What's actually happening.** SlickGrid's `getColumnCssRules()` walks `document.styleSheets` looking for the `<style>` element it injected at init time and scrapes per-column width rules from it. In an iframe child of `EditableTemplate` (or even `VanillaTemplate`), that `<style>` isn't yet attached to `document.styleSheets` when the constructor's `init()` synchronously calls `getColumnCssRules()`. The lookup throws; the table never renders. Bokeh catches the error and `console.error`s it — so it doesn't crash the whole document, but the table area stays blank and the LLM doesn't see it unless the render-error pump is wired (it is — see [01-architecture.md](01-architecture.md), `show_holoviz_report` returns `status="errored"` plus the message).

**The fix.** Use `pn.widgets.Tabulator`. It's the canonical Panel choice now (Tabulator.js, not SlickGrid), and it's built to handle the iframe / late-mount cases. `pn.widgets.DataFrame` exists mostly for back-compat.

```python
# BAD — SlickGrid race condition under iframe + EditableTemplate / VanillaTemplate
table = pn.widgets.DataFrame(df, sizing_mode="stretch_width")

# GOOD
table = pn.widgets.Tabulator(
    df,
    sizing_mode="stretch_width",
    pagination="local",  # or "remote" for big frames
    page_size=25,
)
```

### `pn.extension('tabulator')` is required

Tabulator's JS/CSS bundle is **not loaded by default**. Panel only ships the front-end assets for widget families you explicitly enable via `pn.extension(...)`. Forget this and the widget renders as a static placeholder with no interactivity — and on first interaction you get a JS error because the Tabulator constructor is `undefined`.

```python
import panel as pn

pn.extension('tabulator')  # ← MUST be at module top, before build(ctx)


def build(ctx):
    df = ctx.dataframe(...)
    return pn.Column(
        pn.pane.Markdown("## Results"),
        pn.widgets.Tabulator(df, sizing_mode="stretch_width", pagination="local", page_size=25),
        sizing_mode="stretch_width",
    )
```

Other widget families that need their own extension token: `'plotly'`, `'vega'`, `'deckgl'`, `'gridstack'`, `'mathjax'`, `'echarts'`, `'ipywidgets'`. If you're using one and getting a "blank widget" or a `Cannot read properties of undefined` JS error, the missing `pn.extension(...)` arg is the first thing to check.

`pn.extension(...)` is idempotent — calling it again with the same args is a no-op. Adding tokens to an existing call is fine: `pn.extension('tabulator', 'plotly')`.

### Tabulator `layout=` — match the table to the surrounding cards

Tabulator has four column-sizing modes. The default `'fit_data'` and the seductively-named `'fit_data_stretch'` both size columns to content first, then stretch one column to fill the container. Inside a `sizing_mode='stretch_width'` card alongside fixed-pixel chart PNGs (`pn.pane.PNG(bytes, sizing_mode='stretch_width')` with an 8×4″ figure), the chart cards constrain their own width — but the Tabulator card spans the full pane. Visually the table looks like it belongs to a different report.

```python
# BAD — table stretches wider than the chart cards beside it
pn.widgets.Tabulator(df, sizing_mode="stretch_width", layout="fit_data_stretch")

# GOOD — table respects a fixed column layout matched to the card width
pn.widgets.Tabulator(df, sizing_mode="stretch_width", layout="fit_columns")
```

`layout="fit_columns"` distributes available width across all columns proportionally — the table stays the width of its card and never extends past the neighbouring chart cards. Use `"fit_data_table"` if you want columns sized to content AND the *table* (not just one column) to stop at the content's natural width.

If you need the table at a specific pixel width to match a chart, cap the card itself: `pn.Card(table, title="…", width=720)` (and lose `sizing_mode="stretch_width"` on the card — width and stretch are mutually exclusive).

### Quick anti-pattern table

| Anti-pattern | Failure mode | What to do |
| ------------ | ------------ | ---------- |
| `pn.widgets.DataFrame(df)` | SlickGrid stylesheet race in iframe → blank table + `console.error` | Use `pn.widgets.Tabulator` instead |
| `pn.widgets.Tabulator(df)` without `pn.extension('tabulator')` | Widget renders as inert placeholder; JS errors on interaction | Add `pn.extension('tabulator')` at module top |
| `bokeh.models.DataTable` (raw Bokeh) | Same SlickGrid issue as `pn.widgets.DataFrame` | Use `pn.widgets.Tabulator` |
| `pn.extension(...)` inside `build(ctx)` | Race with Bokeh document setup; tokens may not register in time | Always at module top, before `def build(ctx):` |
| `pn.widgets.Tabulator(df, layout="fit_data_stretch", sizing_mode="stretch_width")` | Last column expands; table looks wider than neighbouring chart cards | Use `layout="fit_columns"` (or cap card width to match charts) |
| `pn.pane.HTML("<style>…</style>")` to style Tabulator / Bokeh widgets | Panel entity-encodes the text; rules end up as literal characters in a `<div>`, never reach the outer-document stylesheet table | Use `ctx.add_css("…")` |

## Other image sources

`pn.pane.PNG(bytes, sizing_mode="stretch_width")` works for *any* PNG source — PIL, scikit-image, plotly's `to_image()`, a downloaded asset. The "render eagerly to bytes" rule generalises: if a library has a "render now" entry point, use it inside `build(ctx)`; if it only has a deferred / lazy renderer, wrap it the same way matplotlib is wrapped above.

## TL;DR for the LLM

**Layout:**

1. Return a content layout from `build(ctx)` — `pn.Column` / `pn.Row` / `pn.GridBox`, with `pn.Card(child, title="…")` for section headers. The host wraps it in a template.
2. **Never** return `pn.template.EditableTemplate(...)` / `VanillaTemplate(...)` / any other `pn.template.*`. That nests a template inside the host's template and produces a `source="server:template"` `ListLike` validation error before the iframe even loads.

**Imports & extensions (must be at the top of the script, before `def build`):**

```python
import matplotlib
matplotlib.use("agg")     # MUST come before any pyplot import — pin for off-main-thread workers
import matplotlib.pyplot as plt
import panel as pn
pn.extension("tabulator")  # plus 'plotly' / 'vega' / 'deckgl' / 'echarts' / etc. for any non-default widget family you use
```

**Plots:**

1. Build the figure (matplotlib OO API or pyplot — both fine).
2. Convert it to PNG bytes via `BytesIO` + `fig.savefig(buf, format="png", ...)` **inside `build(ctx)`**.
3. Wrap with `pn.pane.PNG(buf.read(), sizing_mode="stretch_width")`.
4. `plt.close(fig)` (only needed if you used `plt.subplots()` / `plt.figure()`).
5. Never return / embed a live `Figure`.

**Tables:**

1. Default to `pn.widgets.Tabulator(df, layout="fit_columns", ...)`. Never `pn.widgets.DataFrame` and never raw `bokeh.models.DataTable` — both lose a SlickGrid stylesheet race inside our iframe.
2. Use `layout="fit_columns"` (not `"fit_data_stretch"`) when the table sits alongside fixed-pixel chart cards — keeps widths consistent.
3. Add `pn.extension('tabulator')` at the **top of the script**, before `def build(ctx):`. Without it the widget is inert.
4. Same rule for `'plotly'`, `'vega'`, `'deckgl'`, `'gridstack'`, `'mathjax'`, `'echarts'`, `'ipywidgets'` — every non-default widget family needs its extension token.

**Custom CSS overrides:**

1. Prefer `ctx.apply_theme(layout, host=…)` first — it covers every standard Panel surface (chrome, Markdown, Tabulator chrome, inputs, Plotly modebar) automatically.
2. For one-off rules **outside the shadow DOM** (Markdown classes, Card headers): `ctx.add_css("…")` — injects into the iframe's `<head>`.
3. For rules **inside a widget's shadow root** (Tabulator column highlight, DataTable cell): pass via the widget's `stylesheets=[css_string]` kwarg. Outer-doc CSS can't pierce shadow roots.
4. **Never** `pn.pane.HTML("<style>…")` — Panel entity-encodes HTML panes, so the `<style>` becomes literal text inside a `<div>` and styles nothing.
5. The whole CSS surface is just text: `ctx.theme_css(host=…)` returns the string `apply_theme` would inject. Print it (`ctx.log(...)`) to see exactly what's being applied; concatenate with your own rules for custom surfaces.

**When the iframe breaks but the smoke test passed**, errors come from one of four layers; `get_report_render_errors(report_id)` tags each with its `source` so you know which:

| `source`                       | When it fires                                                                                 | What to check first                                                                                                                                       |
| ------------------------------ | --------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `server:script`                | Inside `build(ctx)` when called by the Panel session (after smoke test passed)                | Matplotlib backend (top of doc); session-thread-only failures the smoke test didn't reach; mutable module-scope state shared across sessions.            |
| `server:template`              | Panel/Bokeh template instantiation — *before* the iframe loads                                | Are you returning a `pn.template.*`? **Don't.** Return a `pn.Column` / `pn.Card` and let the host wrap it.                                                |
| `window.error` / `console.error` / `bokeh` | Inside the iframe at JS init time                                              | Missing `pn.extension('<token>')` for a non-default widget family; `pn.widgets.DataFrame` (SlickGrid race); third-party JS that needs `ctx.add_js(...)`. |
| `unhandledrejection`           | Async work inside the iframe (network fetch, deferred widget mount)                           | Network failures (CDN-loaded JS unreachable); promises returned from `CustomJS` callbacks.                                                                 |

**Errors mask each other — fix them in order.** A `server:template` failure short-circuits the iframe before any JS runs, so a missing `pn.extension('tabulator')` won't show up until you've fixed the template return. After every fix, re-run `show_holoviz_report` to surface the next failure. Symptom of the masking: `show_holoviz_report` returns `errored` with one item in `errors[]`, you fix exactly that, re-run, and `errored` becomes `ready` or surfaces a completely different problem. That's normal — the layers are independent.

Quick triage flow:

1. Run `show_holoviz_report` and read `status` + `errors[0].source`.
2. If `source == "server:template"` → stop returning a template, retry.
3. If `source == "server:script"` → check matplotlib backend pin and pyplot/PNG conversion pattern.
4. If `source` looks JS-ish → check `pn.extension(...)` tokens and `pn.widgets.DataFrame` → Tabulator.
5. After each fix, re-run. Don't assume you're done until `status == "ready"`.
