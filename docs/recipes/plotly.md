# Recipe: Plotly via CDN

Embed an interactive Plotly chart inside an HTML report.

## The full pattern

```python
import plotly.graph_objects as go
import plotly.io as pio
import json

def build(ctx):
    fig = go.Figure(go.Scatter(
        x=[1, 2, 3, 4, 5],
        y=[1, 4, 9, 16, 25],
        mode="lines+markers",
        name="squares",
    ))
    fig.update_layout(
        title="Squares",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
    )
    # Build a JSON spec the FE Plotly can consume.
    spec = pio.to_json(fig)

    t = ctx.theme()
    bg = t.get("--voitta-bg", "#fff")
    fg = t.get("--voitta-text", "#000")

    return f"""<!doctype html>
<html>
<head>
  <script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
  <style>
    body {{ background: {bg}; color: {fg}; font-family: system-ui;
            margin: 0; padding: 16px; }}
    #chart {{ width: 100%; height: 480px; }}
  </style>
</head>
<body>
  <div id="chart"></div>
  <script>
    const spec = {spec};
    Plotly.newPlot("chart", spec.data, spec.layout, {{
      displayModeBar: false, responsive: true,
    }});
  </script>
</body>
</html>"""
```

## Notes

- `plotly.io.to_json(fig)` produces a JSON string with `data` +
  `layout` keys. Drop it into the iframe verbatim.
- The CDN URL above pins to 2.35.2; bump if you need a feature
  from a newer version.
- `displayModeBar: false` hides Plotly's toolbar — common for
  embedded charts. Set `true` (the default) if you want pan/zoom
  controls in the screenshot.
- For Plotly to be screenshot-friendly: charts settle within the
  default 1500ms wait. Big datasets may need `expand_settle_ms`
  raised on the `screenshot_report` call.

## Multiple charts

```python
return f"""<!doctype html>
<html>
<head>
  <script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
  <style>
    body {{ margin: 0; padding: 16px; font-family: system-ui; }}
    .row {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }}
    .chart {{ height: 320px; }}
  </style>
</head>
<body>
  <div class="row">
    <div class="chart" id="c1"></div>
    <div class="chart" id="c2"></div>
  </div>
  <script>
    Plotly.newPlot("c1", {spec_1}.data, {spec_1}.layout);
    Plotly.newPlot("c2", {spec_2}.data, {spec_2}.layout);
  </script>
</body>
</html>"""
```
