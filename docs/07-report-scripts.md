# Report scripts — Panel + matplotlib best practices

Report scripts are stored Python files of the form `def build(ctx) -> pn.viewable.Viewable`. The Panel-served route `/panel/reports?id=<slug>` calls `build(ctx)` once per browser session, returns the layout, and Bokeh serialises that document to the iframe. See [01-architecture.md](01-architecture.md) for the bigger picture; this doc is just the matplotlib-embedding rules.

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
| `pn.pane.Matplotlib(fig)` left in the tree | Bokeh tries to serialise a live Figure at render time; mismatched backends or closed/mutated figures raise inside the Bokeh session | ❌ no |
| `plt.show()` inside `build(ctx)` | No-op in headless backend, or pops a window in the host's display, or hangs depending on backend | ❌ no |
| `fig.savefig("/tmp/foo.png")` then referencing the file by URL | Path leaks across sessions, breaks on cleanup, race conditions, no served route | ❌ no |
| Returning a `Figure` directly from `build(ctx)` | Panel doesn't know what to do with it; Bokeh serializer chokes | ❌ no — `build` returned successfully |
| Building the figure once at module scope and reusing it across `build(ctx)` calls | Each session mutates the same Figure; first session's render closes the canvas the second uses | ❌ no |

All of these *can* run cleanly the first time and fail later, after `build(ctx)` returns — past the smoke test's reach.

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

### Quick anti-pattern table

| Anti-pattern | Failure mode | What to do |
| ------------ | ------------ | ---------- |
| `pn.widgets.DataFrame(df)` | SlickGrid stylesheet race in iframe → blank table + `console.error` | Use `pn.widgets.Tabulator` instead |
| `pn.widgets.Tabulator(df)` without `pn.extension('tabulator')` | Widget renders as inert placeholder; JS errors on interaction | Add `pn.extension('tabulator')` at module top |
| `bokeh.models.DataTable` (raw Bokeh) | Same SlickGrid issue as `pn.widgets.DataFrame` | Use `pn.widgets.Tabulator` |
| `pn.extension(...)` inside `build(ctx)` | Race with Bokeh document setup; tokens may not register in time | Always at module top, before `def build(ctx):` |

## Other image sources

`pn.pane.PNG(bytes, sizing_mode="stretch_width")` works for *any* PNG source — PIL, scikit-image, plotly's `to_image()`, a downloaded asset. The "render eagerly to bytes" rule generalises: if a library has a "render now" entry point, use it inside `build(ctx)`; if it only has a deferred / lazy renderer, wrap it the same way matplotlib is wrapped above.

## TL;DR for the LLM

**Plots:**

1. Build the figure (matplotlib OO API or pyplot — both fine).
2. Convert it to PNG bytes via `BytesIO` + `fig.savefig(buf, format="png", ...)` **inside `build(ctx)`**.
3. Wrap with `pn.pane.PNG(buf.read(), sizing_mode="stretch_width")`.
4. `plt.close(fig)` (only needed if you used `plt.subplots()` / `plt.figure()`).
5. Never return / embed a live `Figure`.

**Tables:**

1. Default to `pn.widgets.Tabulator(df, ...)`. Never `pn.widgets.DataFrame` and never raw `bokeh.models.DataTable` — both lose a SlickGrid stylesheet race inside our iframe.
2. Add `pn.extension('tabulator')` at the **top of the script**, before `def build(ctx):`. Without it the widget is inert.
3. Same rule for `'plotly'`, `'vega'`, `'deckgl'`, `'gridstack'`, `'mathjax'`, `'echarts'`, `'ipywidgets'` — every non-default widget family needs its extension token.

**When the iframe breaks but the smoke test passed**, check in this order:

1. Is there a deferred matplotlib pane (`pn.pane.Matplotlib(fig)`) anywhere? → switch to the BytesIO pattern.
2. Is there a `pn.widgets.DataFrame` or `bokeh.models.DataTable`? → switch to `pn.widgets.Tabulator` (and add the extension token).
3. Is a non-default widget family in use without its `pn.extension(...)` token? → add it.
4. Otherwise, call `get_report_render_errors(report_id)` to pull the captured error stack — it'll usually name the offending class/file.
