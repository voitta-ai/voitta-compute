"""Interactive viewer for parsed SoundCheck `.dat` snapshots.

Reads ``dat_curves.json`` + ``dat_summary.json`` from the most-recent
``python_storage`` snapshot that contains them (i.e. one for which the
``dat_parse`` compute script has already run). Renders one Bokeh figure
per curve kind (PFR, HD2, THD, …), overlaying every serial number's curve
as a translucent line plus a median line on top. A selector switches
between kinds; the X axis is log by default for Hz units.

Per-iframe budget: 7344 curves × 105 points × 8 bytes/value ≈ 6 MB raw.
We pre-thin to keep payloads under the iframe wall and downsample to
≤ 64 visible curves per kind via deterministic stride so overlay plots
stay responsive.
"""

from __future__ import annotations

import json
import os

import numpy as np
import panel as pn
from bokeh.models import ColumnDataSource, HoverTool
from bokeh.palettes import Category10
from bokeh.plotting import figure


# Cap visible overlay lines per kind. With 612 serials the overlay is too
# noisy and slow; deterministic stride keeps shape-indicative coverage.
MAX_OVERLAY_PER_KIND = 64

# Default plot canvas — sized so the chat-side iframe stays compact.
PLOT_W = 720
PLOT_H = 380


def _find_dat_snapshot(ctx):
    """Most recent python_storage snapshot with both ``dat_curves.json``
    and ``dat_summary.json``. Raises with a clear message if none exists."""
    from app.services import python_storage as ps

    best: tuple[float, str] | None = None
    for d in ps.STORAGE_ROOT.iterdir():
        if not d.is_dir():
            continue
        if not all((d / n).exists() for n in ("dat_curves.json", "dat_summary.json")):
            continue
        mtime = (d / "dat_curves.json").stat().st_mtime
        if best is None or mtime > best[0]:
            best = (mtime, str(d))
    if best is None:
        raise RuntimeError(
            "No snapshot with dat_curves.json + dat_summary.json. "
            "Run the `dat_parse` compute script against your .dat snapshot first."
        )
    return best[1]


def _bin_curves_by_kind(curves: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for c in curves:
        kind = next(
            (m["value"] for m in (c.get("metadata") or []) if m.get("key") == "kind"),
            None,
        ) or "?"
        out.setdefault(kind, []).append(c)
    return out


def _serial_of(curve: dict) -> str:
    for m in curve.get("metadata") or []:
        if m.get("key") == "s/n":
            return str(m.get("value"))
    return ""


def _median_curve(curves: list[dict]) -> tuple[list[float], list[float]] | None:
    """Take a per-X median across all serials of one kind, when their X
    grids match. SoundCheck typically uses identical sweeps per kind, so
    the simple equality check holds in the common case."""
    if not curves:
        return None
    first = curves[0]
    x_series = next((s for s in first["series"] if s["name"] == "X"), None)
    y_series = next((s for s in first["series"] if s["name"] == "Y"), None)
    if not x_series or not y_series:
        return None
    x = x_series["values"]
    n = len(x)
    ys: list[list[float]] = []
    for c in curves:
        xs = next((s["values"] for s in c["series"] if s["name"] == "X"), None)
        ysv = next((s["values"] for s in c["series"] if s["name"] == "Y"), None)
        if xs is None or ysv is None or len(ysv) != n or xs != x:
            continue
        ys.append(ysv)
    if not ys:
        return None
    arr = np.asarray(ys, dtype=np.float64)
    med = np.median(arr, axis=0).tolist()
    return list(x), med


def _build_kind_figure(kind: str, curves: list[dict]) -> figure:
    """Bokeh figure overlaying every (sub-sampled) curve of one kind."""
    if not curves:
        return figure(title=kind, width=PLOT_W, height=PLOT_H)

    # Pull units from the first curve; SoundCheck holds them constant per kind.
    first = curves[0]
    series_by_name = {s["name"]: s for s in first["series"]}
    x_unit = series_by_name.get("X", {}).get("unit", "")
    y_unit = series_by_name.get("Y", {}).get("unit", "")

    # Log-X for Hz sweeps (the SoundCheck default), linear otherwise.
    x_axis_type = "log" if x_unit == "Hz" else "linear"

    p = figure(
        title=f"{kind}  ({len(curves)} serials)",
        x_axis_label=f"X ({x_unit})" if x_unit else "X",
        y_axis_label=f"Y ({y_unit})" if y_unit else "Y",
        x_axis_type=x_axis_type,
        width=PLOT_W,
        height=PLOT_H,
        tools="pan,wheel_zoom,box_zoom,reset,save",
        toolbar_location="above",
    )

    stride = max(1, len(curves) // MAX_OVERLAY_PER_KIND)
    sampled = curves[::stride]

    overlay_color = Category10[10][0]  # consistent muted blue
    for c in sampled:
        x = next((s["values"] for s in c["series"] if s["name"] == "X"), [])
        y = next((s["values"] for s in c["series"] if s["name"] == "Y"), [])
        if not x or not y or len(x) != len(y):
            continue
        sn = _serial_of(c)
        # Filter zeros / negatives on a log axis — Bokeh will warn otherwise.
        if x_axis_type == "log":
            xy = [(xi, yi) for xi, yi in zip(x, y) if xi > 0]
            if not xy:
                continue
            x = [a for a, _ in xy]
            y = [b for _, b in xy]
        src = ColumnDataSource(data={"x": x, "y": y, "sn": [sn] * len(x)})
        p.line(
            "x", "y", source=src,
            line_alpha=0.25, line_width=1.0, color=overlay_color,
        )

    median = _median_curve(curves)
    if median:
        mx, my = median
        if x_axis_type == "log":
            mxy = [(xi, yi) for xi, yi in zip(mx, my) if xi > 0]
            if mxy:
                mx = [a for a, _ in mxy]
                my = [b for _, b in mxy]
        p.line(mx, my, line_width=2.5, color="#e6550d", legend_label="median")

    if p.legend:
        p.legend.location = "top_right"
        p.legend.click_policy = "hide"
        p.legend.background_fill_alpha = 0.7

    p.add_tools(HoverTool(
        tooltips=[("s/n", "@sn"), ("x", "@x"), ("y", "@y")],
        mode="vline",
    ))
    return p


def build(ctx):
    pn.extension()
    snap_dir = _find_dat_snapshot(ctx)

    with open(os.path.join(snap_dir, "dat_summary.json")) as f:
        summary = json.load(f)
    with open(os.path.join(snap_dir, "dat_curves.json")) as f:
        body = json.load(f)

    curves = body.get("curves", [])
    binned = _bin_curves_by_kind(curves)
    kind_order = sorted(binned.keys(), key=lambda k: -len(binned[k]))

    # ---- header summary ----
    kinds_str = ", ".join(f"**{k}** ({len(binned[k])})" for k in kind_order)
    head_md = (
        f"### SoundCheck `{body.get('filename', '?')}`\n\n"
        f"**{summary.get('n_items', '?')}** curves · "
        f"DAT v{summary.get('cluster_versions', [])} · "
        f"**{summary.get('n_unique_serials', '?')}** distinct serials  \n"
        f"Curve kinds (count of serials): {kinds_str}  \n"
        f"_Up to {MAX_OVERLAY_PER_KIND} curves shown per kind; the orange "
        f"line is the per-X median across all serials._"
    )

    # ---- per-kind figures into Tabs ----
    tabs = []
    for kind in kind_order:
        fig = _build_kind_figure(kind, binned[kind])
        tabs.append((kind, pn.pane.Bokeh(fig, sizing_mode="fixed")))
    if not tabs:
        tabs.append(("(no curves)", pn.pane.Markdown("No curves in this snapshot.")))

    return pn.Column(
        pn.pane.Markdown(head_md, sizing_mode="stretch_width"),
        pn.Tabs(*tabs, sizing_mode="stretch_width"),
        sizing_mode="stretch_width",
    )
