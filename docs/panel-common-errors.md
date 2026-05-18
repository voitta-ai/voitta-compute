# Panel report — common errors keyed by symptom

Search this file with the exact error text the smoke test or `get_report_render_errors` returned.

## `TypeError: Children parameter 'ListLike.objects' items must be instances of Panel, not str`

**Cause:** `build(ctx)` returned a `pn.template.*` (EditableTemplate, VanillaTemplate, MaterialTemplate, FastListTemplate, etc.) — the host then nested its own `EditableTemplate` around it and Bokeh's `ListLike` validation rejects the structure.

**Fix:** return a content layout (`pn.Column`, `pn.Row`, `pn.GridSpec`, `pn.Card`), not a template. The host wraps it.

```python
# BAD
return pn.template.EditableTemplate(main=[layout])

# GOOD
return layout
```

## `SlickGrid Cannot find stylesheet`

**Cause:** You used `pn.widgets.DataFrame(df)` (or `bokeh.models.DataTable`). SlickGrid's constructor synchronously walks `document.styleSheets` for a `<style>` element that isn't attached yet inside the iframe. Table renders blank.

**Fix:** switch to `pn.widgets.Tabulator(df, layout="fit_columns", pagination="local", page_size=25)`. Add `pn.extension("tabulator")` at module top.

## Tabulator renders as inert placeholder; clicking does nothing

**Cause:** Missing `pn.extension("tabulator")` at module top.

**Fix:** add it before `def build`. Other widget families that need their token: `"plotly"`, `"vega"`, `"deckgl"`, `"echarts"`, `"ipywidgets"`. The call is idempotent.

## Matplotlib raises `Cannot start Qt event loop from non-main thread` (or similar GUI-backend error)

**Cause:** `import matplotlib.pyplot as plt` happened before `matplotlib.use("agg")`. The Bokeh worker thread can't init GUI backends.

**Fix:** pin Agg at the **very top** of the script, before any pyplot import (including transitive ones from seaborn / pandas plotting / holoviews):

```python
import matplotlib
matplotlib.use("agg")
import matplotlib.pyplot as plt
```

## Report shows but matplotlib pane is blank / charts disappear after a rerun

**Cause:** You returned a live `Figure` to Panel via `pn.pane.Matplotlib(fig)` and the figure was closed, mutated, or shared across sessions before Bokeh serialised it.

**Fix:** render eagerly to PNG bytes inside `build(ctx)`:

```python
from io import BytesIO
buf = BytesIO()
fig.savefig(buf, format="png", dpi=130, bbox_inches="tight")
buf.seek(0)
img = pn.pane.PNG(buf.read(), sizing_mode="stretch_width")
plt.close(fig)
```

Any matplotlib failure now fires inside `build` (caught by the smoke test) instead of inside the Bokeh render path (silent).

## `<style>` rules I put in `pn.pane.HTML` aren't applied

**Cause:** Panel entity-encodes HTML pane content. The `<style>` becomes literal text inside a `<div>`.

**Fix:** use `ctx.add_css("…")` instead. Lands in the iframe `<head>` as a real stylesheet.

## Tabulator chrome stays default-coloured even after `ctx.apply_theme`

**Cause:** Most likely fine — `apply_theme` walks the layout and attaches the theme CSS to each shadow-DOM widget's `stylesheets=` list. If this is failing, check:
1. Did you wrap with `apply_theme` *after* assembling the layout (not before)?
2. Is the Tabulator instance inside the layout you passed to `apply_theme`?

If both yes and it still doesn't theme, file an issue with a minimal repro.

## Three.js scene shows in bottom-left corner of an empty viewport

**Cause:** In a custom iframe (not `ctx.three_scene`), `camera.position.set(x, y, z)` places the camera but doesn't aim it. The camera's forward vector is still the default `(0, 0, -1)`.

**Fix:** add `camera.lookAt(0, 0, 0)` after `position.set`. Inside `ctx.three_scene` the helper does this for you; the symptom there usually means you're using a custom iframe, not `three_scene`.

## `show_holoviz_report` returns `status: "timeout"` with empty errors[]

**Cause:** The iframe never reported ready or errored within the wait window (default 8s). Either:
- The user has no active chat pane open.
- The iframe failed to load (backend unreachable from the host page).
- A `source="server:template"` error fired in the Panel session *before* the iframe document loaded.

**Fix:** check the chat pane is open. If it is, look at backend logs for Panel session-init exceptions, or check `get_report_render_errors(report_id)` for `source: "server:template"` entries.

## User says the report broke, but `show_holoviz_report` returned `ready`

**Cause:** A render-time error fired *after* the wait window closed (e.g. user toggled edit mode, a deferred widget mounted, a WebSocket reconnected).

**Fix:** `get_report_render_errors(report_id)` — pulls every error logged for this report, including post-`ready` ones. Each entry has a `source` field:
- `window.error` / `console.error` / `unhandledrejection` / `bokeh` — iframe-side JS
- `server:script` — Panel session re-ran `build(ctx)` and it raised
- `server:template` — template instantiation failed (rare)

## Layer cake of errors — fix in order

A `server:template` failure short-circuits the iframe before any JS runs, so a missing `pn.extension("tabulator")` won't show up until you fix the template return. Fix one, re-run `show_holoviz_report`, repeat until `status == "ready"`.
