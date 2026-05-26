# Recipe: Matplotlib → base64 `<img>`

Render the chart server-side, base64-encode, embed in HTML.

For any sampling / synthetic-data generation, use **numpy**, not
stdlib `random`:

```python
import numpy as np
rng = np.random.default_rng(42)
y = rng.normal(size=200).cumsum()      # random walk
ax.plot(y)
```

`numpy` is always available alongside `matplotlib` and `pandas`.
See `06-reports.md` for why numpy beats stdlib `random` for data
work (bounds-safe, reproducible, vectorised).

## The full pattern

```python
import base64
import io
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

def _fig_to_b64(fig, *, dpi=120) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", dpi=dpi)
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def build(ctx):
    fig, ax = plt.subplots(figsize=(8, 5))
    ax.plot([1, 2, 3, 4, 5], [1, 4, 9, 16, 25], marker="o")
    ax.set_title("Squares")
    ax.set_xlabel("x")
    ax.set_ylabel("x²")

    img_b64 = _fig_to_b64(fig)
    t = ctx.theme()
    bg = t.get("--voitta-bg", "#fff")

    return f"""<!doctype html>
<html>
<head><style>
  body {{ background: {bg}; margin: 0; padding: 16px;
          font-family: system-ui; }}
  img {{ max-width: 100%; height: auto; }}
</style></head>
<body>
  <img src="data:image/png;base64,{img_b64}" alt="Squares chart">
</body>
</html>"""
```

## Themed plots

Use `ctx.theme()` to pick matplotlib colors that match the host:

```python
t = ctx.theme()
fig, ax = plt.subplots()
fig.patch.set_facecolor(t.get("--voitta-bg", "#fff"))
ax.set_facecolor(t.get("--voitta-surface", "#fff"))
ax.spines["top"].set_color(t.get("--voitta-border", "#1a2230"))
ax.tick_params(colors=t.get("--voitta-text", "#000"))
ax.plot(x, y, color=t.get("--voitta-accent", "#0a84ff"))
```

## Multiple charts

```python
fig1_b64 = _fig_to_b64(make_chart_1())
fig2_b64 = _fig_to_b64(make_chart_2())

return f"""<!doctype html>
<html>
<head><style>
  body {{ margin: 0; padding: 16px; font-family: system-ui; }}
  .row {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }}
  .row img {{ max-width: 100%; }}
</style></head>
<body>
  <div class="row">
    <img src="data:image/png;base64,{fig1_b64}">
    <img src="data:image/png;base64,{fig2_b64}">
  </div>
</body>
</html>"""
```

## Notes

- Always `matplotlib.use("Agg")` at import — avoids GUI backend
  issues in a script context
- Always `plt.close(fig)` after `savefig` — leaks Agg memory
  otherwise
- `bbox_inches="tight"` trims whitespace around the plot
- `dpi=120` is a sensible default; raise to 200 for retina
  quality (4× the bytes)
- Base64 PNGs are large — a 1920x1200 dpi=200 chart can hit
  500KB. The screenshot path is fine with this; just keep the
  TOTAL HTML size reasonable (< 5MB)
