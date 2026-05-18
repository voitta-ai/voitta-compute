# Panel report — loading data from `python_storage`

When the LLM has previously called `fetch_to_python_storage` or `run_compute` and gotten back a `handle`, that handle points at a directory under `python_storage/cache/snapshot_<handle>/` containing the data. Report scripts load it via `ctx`.

## The three accessors

| Method | Returns | Use when |
|---|---|---|
| `ctx.snapshot(handle)` | dict `{path, meta}` | You want the raw snapshot directory + metadata; everything else is built on top of this. |
| `ctx.dataframe(handle)` | `pandas.DataFrame` | The snapshot has a `curves.pkl` and you want it as a DataFrame. |
| `ctx.raw(handle)` | dict (parsed `raw.json`) | The snapshot has a `raw.json` and you want it as a Python dict. |

## Pattern: load a DataFrame, plot it

```python
import matplotlib
matplotlib.use("agg")
import matplotlib.pyplot as plt
import panel as pn
from io import BytesIO


def fig_to_png(fig):
    buf = BytesIO()
    fig.savefig(buf, format="png", dpi=130, bbox_inches="tight")
    buf.seek(0)
    return pn.pane.PNG(buf.read(), sizing_mode="stretch_width")


def build(ctx):
    df = ctx.dataframe("snap_abc123")

    fig, ax = plt.subplots(figsize=(8, 4))
    ax.plot(df["time"], df["value"])
    img = fig_to_png(fig)
    plt.close(fig)

    return pn.Column(
        pn.pane.Markdown("# Time series"),
        img,
    )
```

## Pattern: load raw JSON, render a table

```python
import panel as pn
import pandas as pd
pn.extension("tabulator")


def build(ctx):
    rec = ctx.raw("snap_xyz789")
    df = pd.DataFrame(rec["measurements"])
    return pn.Column(
        pn.pane.Markdown(f"# {rec['title']}"),
        pn.widgets.Tabulator(df, sizing_mode="stretch_width", layout="fit_columns", pagination="local", page_size=25),
    )
```

## Pattern: read arbitrary files from the snapshot

When the snapshot contains files other than `curves.pkl` / `raw.json` (a CAD `.glb`, a custom binary, an image):

```python
import base64
import pathlib


def build(ctx):
    rec = ctx.snapshot("snap_glb_001")
    snap_dir = pathlib.Path(rec["path"])
    glb_path = snap_dir / rec["meta"]["stored_name"]
    # ... use glb_path however you need ...
```

`rec["path"]` is the snapshot's directory; `rec["meta"]` is the metadata dict (includes `stored_name`, MIME type, size, etc.).

## What NOT to do

- **Don't pass the handle to `run_compute` and then to `build(ctx)` separately if they're in the same conversation** — the snapshot is server-local, both calls see the same files. One `ctx.dataframe(handle)` in `build` is enough.
- **Don't try to load a snapshot with `pd.read_pickle("python_storage/...")` directly.** Path layout is internal and may change. Always go through `ctx`.
- **Don't store the DataFrame at module scope and reuse it across `build()` calls.** Each session calls `build(ctx)` afresh — mutating module state leaks across users.

## Tables — use `pn.widgets.Tabulator`, never `pn.widgets.DataFrame`

`pn.widgets.DataFrame` is backed by Bokeh's SlickGrid `DataTable`. Inside an iframe-embedded Panel report it loses a stylesheet race and renders blank.

```python
# BAD — SlickGrid stylesheet race inside iframe
pn.widgets.DataFrame(df)

# GOOD
pn.widgets.Tabulator(df, sizing_mode="stretch_width", layout="fit_columns", pagination="local", page_size=25)
```

`pn.extension("tabulator")` must be at module top, before `def build`.
