"""Mock report layout used as a fallback when no script matches.

Returns a Panel ``Column`` with a header, indicator row, an interactive
Bokeh chart, and a small Tabulator. Lives behind a lazy-import — Panel
pulls in pandas/bokeh/jinja2/etc., and we don't want any of that on the
FastAPI startup path.

Used by ``app.services.panel_app.panel_factory`` when ``id`` doesn't
resolve to a stored report script.
"""

from __future__ import annotations

from threading import Lock


_extension_lock = Lock()
_extension_initialised = False


def _ensure_extension() -> None:
    """Call ``pn.extension()`` exactly once per process."""

    global _extension_initialised
    with _extension_lock:
        if _extension_initialised:
            return
        import panel as pn

        pn.extension()
        _extension_initialised = True


def mock_layout(report_id: str):
    """Build a Panel ``Column`` for ``report_id``. Per-session — never
    cache the returned layout: Bokeh widgets are tied to the session
    document and break if reused across sessions."""

    _ensure_extension()
    import math

    import pandas as pd
    import panel as pn
    from bokeh.plotting import figure

    header = pn.pane.Markdown(
        f"# 📊 Mock Report — `{report_id}`\n\n"
        "This panel is rendered server-side by **FastAPI + HoloViz "
        "Panel** and embedded in the chat UI via an `<iframe>`. Real "
        "report payloads will replace this mock once a real "
        "`define_report` script is registered.",
        sizing_mode="stretch_width",
    )

    indicators = pn.Row(
        pn.indicators.Number(name="Curves", value=42, default_color="#cf2a2e", font_size="28pt"),
        pn.indicators.Number(name="Workitems", value=7, default_color="#1c8a4a", font_size="28pt"),
        pn.indicators.Number(name="Datasets", value=3, default_color="#1766c4", font_size="28pt"),
        sizing_mode="stretch_width",
    )

    chart = figure(
        title="Mock signal — sin(x) + 0.3 sin(3x)",
        height=240,
        sizing_mode="stretch_width",
        tools="pan,wheel_zoom,box_zoom,reset,save",
    )
    xs = [i / 10 for i in range(0, 101)]
    ys = [math.sin(x) + 0.3 * math.sin(3 * x) for x in xs]
    chart.line(xs, ys, line_width=2, color="#cf2a2e")
    chart.xaxis.axis_label = "x"
    chart.yaxis.axis_label = "amplitude"

    df = pd.DataFrame(
        {
            "name": ["FR speaker A", "FR speaker B", "Imp speaker A"],
            "s/n": ["A001", "B007", "A001"],
            "score": [0.92, 0.87, 0.74],
        }
    )
    table = pn.widgets.Tabulator(
        df, height=180, sizing_mode="stretch_width", disabled=True, show_index=False
    )

    return pn.Column(
        header,
        indicators,
        pn.pane.Markdown("## Trace", sizing_mode="stretch_width"),
        chart,
        pn.pane.Markdown("## Sample data", sizing_mode="stretch_width"),
        table,
        sizing_mode="stretch_width",
        margin=(20, 24),
    )
