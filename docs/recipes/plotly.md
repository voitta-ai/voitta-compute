# Recipe: Plotly via CDN

Embed an interactive Plotly chart inside an HTML report.

## Basic pattern

```python
import json

def build(ctx):
    data = [
        {"x": [1, 2, 3, 4], "y": [10, 15, 13, 17], "type": "scatter", "name": "Series A"},
        {"x": [1, 2, 3, 4], "y": [16, 5, 11, 9],  "type": "scatter", "name": "Series B"},
    ]
    layout = {
        "title": "My Chart",
        "xaxis": {"title": "X"},
        "yaxis": {"title": "Y"},
        "autosize": True,
    }
    data_json = json.dumps(data)
    layout_json = json.dumps(layout)

    return f"""<!DOCTYPE html>
<html>
<head>
<style>
  body {{ margin: 0; padding: 8px; }}
  #chart {{ width: 100%; height: 480px; }}
</style>
</head>
<body>
<div id="chart"></div>
<script src="https://cdn.plot.ly/plotly-2.27.0.min.js"></script>
<script>
  Plotly.newPlot('chart', {data_json}, {layout_json}, {{responsive: true}});
</script>
</body>
</html>"""
```

## Responsive sizing

Pass `{{responsive: true}}` in the config argument to `Plotly.newPlot`. This makes the chart resize with the iframe container.

Do not set `height: 100vh` on the container — use an explicit pixel height.

## Theming

```python
import json

def build(ctx):
    t = ctx.theme()
    bg = t.get("--voitta-bg", "#ffffff")
    text = t.get("--voitta-text", "#111111")
    accent = t.get("--voitta-accent", "#5b5fc7")

    data = [{"x": [1,2,3], "y": [1,4,2], "type": "bar", "marker": {"color": accent}}]
    layout = {
        "paper_bgcolor": bg,
        "plot_bgcolor": bg,
        "font": {"color": text},
        "autosize": True,
    }

    return f"""<!DOCTYPE html>
<html>
<head><style>body{{margin:0;background:{bg}}}#c{{width:100%;height:480px}}</style></head>
<body>
<div id="c"></div>
<script src="https://cdn.plot.ly/plotly-2.27.0.min.js"></script>
<script>Plotly.newPlot('c',{json.dumps(data)},{json.dumps(layout)},{{responsive:true}})</script>
</body></html>"""
```

## Building data server-side

```python
import json
import pandas as pd

def build(ctx):
    df = ctx.dataframe("my-snapshot")
    # Build Plotly traces from the DataFrame
    traces = []
    for col in df.columns:
        if col == "date":
            continue
        traces.append({
            "x": df["date"].astype(str).tolist(),
            "y": df[col].tolist(),
            "name": col,
            "type": "scatter",
        })
    layout = {"title": "Time Series", "autosize": True}
    return f"""<!DOCTYPE html>
<html><head><style>body{{margin:0}}#c{{width:100%;height:500px}}</style></head>
<body><div id="c"></div>
<script src="https://cdn.plot.ly/plotly-2.27.0.min.js"></script>
<script>Plotly.newPlot('c',{json.dumps(traces)},{json.dumps(layout)},{{responsive:true}})</script>
</body></html>"""
```

## Notes

- Plotly is ~3 MB from CDN; first load can take a second.
- For reports that only need a static image, matplotlib is faster (server-side, no CDN).
- `Plotly.react()` can update data in-place for interactive reports with controls.
