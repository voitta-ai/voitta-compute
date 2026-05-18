# Panel report — minimum skeleton

The whole authoring contract in one page. Read this **first** when you're about to call `define_report`.

## The contract

A report script is one Python file with one top-level function:

```python
def build(ctx) -> "pn.viewable.Viewable":
    ...
```

- `build(ctx)` runs once per browser session of the report iframe.
- Return any `pn.viewable.Viewable` — a content layout (`pn.Column`, `pn.Row`, `pn.GridSpec`, `pn.Card`), an individual pane (`pn.pane.Markdown`, `pn.pane.PNG`, `pn.pane.Plotly`), or a `pn.template.*` if you have a strong reason. The host wraps content layouts in `EditableTemplate` automatically; if you return a template, the host uses it as-is.
- `panel` is **not** auto-imported. `import panel as pn` at the top.
- The full venv is available: pandas, numpy, matplotlib, plotly, bokeh, holoviews, h5py, scipy, three.js (via `ctx.three_scene`), etc.

## Minimum working example

```python
import panel as pn


def build(ctx):
    return pn.Column(
        pn.pane.Markdown("# Hello"),
        pn.pane.Markdown("This is a minimal report."),
    )
```

That's it. Define it with `define_report("hello", code)`, show it with `show_holoviz_report("hello")`.

## The five things you almost always need

```python
import matplotlib
matplotlib.use("agg")   # MUST be before any pyplot import — Bokeh server runs build() off the main thread

import matplotlib.pyplot as plt
import panel as pn

pn.extension("tabulator")   # required for pn.widgets.Tabulator; add 'plotly', 'vega', etc. as needed


def build(ctx):
    return pn.Column(
        pn.pane.Markdown("# Title"),
        # ... rest of the report ...
    )
```

- `matplotlib.use("agg")` — pin **before** any pyplot import. The Bokeh worker thread can't initialize GUI backends; the failure is intermittent and confusing.
- `pn.extension("tabulator")` — required for non-default widget families. Other tokens: `"plotly"`, `"vega"`, `"deckgl"`, `"echarts"`, `"ipywidgets"`. Call at module top, not inside `build`.

## Editing existing reports — speed matters

- **Create or full rewrite** → `define_report(name, code)` (submits full source).
- **Targeted edit** → `edit_report_script(name, [{find, replace}, ...])` — search/replace, much faster wall-clock than re-emitting the whole file.

Both run the same smoke test after writing: `build(ctx)` is called once in-process. Exceptions come back in the tool result as `smoke_error` with a raw traceback. The smoke test does not run JavaScript — render-time errors (Bokeh hydration, missing extension token, late-loaded CSS) only surface via `show_holoviz_report` or `get_report_render_errors`.

## What `ctx` gives you

Small, focused surface — only what you can't easily express in vanilla Panel:

- `ctx.snapshot(handle)` / `ctx.dataframe(handle)` / `ctx.raw(handle)` — load data from `python_storage`. See [panel-snapshots.md](panel-snapshots.md).
- `ctx.three_scene(scene_js, height=, bg=)` — sandboxed WebGL pane. See [panel-three-scene.md](panel-three-scene.md).
- `ctx.log(...)` — debug log line; surfaces in the tool result.
- `ctx.get_theme(host=)` / `ctx.theme_css(host=)` / `ctx.apply_theme(layout, host=)` — host-aware theming. See [panel-theming.md](panel-theming.md).

Everything else is vanilla Panel. No magic.

## When something breaks

- Smoke test failed (`smoke_error` non-null in the tool result) → read the raw traceback, fix, resubmit.
- `show_holoviz_report` returned `status: "errored"` → read `errors[0].message`. Cross-reference with [panel-common-errors.md](panel-common-errors.md).
- User says "the report is broken" but the tool returned `ready` → render-time error fired after the wait window. Call `get_report_render_errors(report_id)`.
- The screenshot looks wrong → screenshots are lossy. See [panel-screenshot-limits.md](panel-screenshot-limits.md).
