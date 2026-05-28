# Recipe: matplotlib → base64 `<img>`

matplotlib runs server-side. The `Agg` backend is pre-set by the sandbox before user code runs, so you never need to call `matplotlib.use("agg")` yourself.

## Basic pattern

```python
import io, base64
import matplotlib.pyplot as plt

def build(ctx):
    fig, ax = plt.subplots(figsize=(8, 4))
    ax.plot([1, 2, 3, 4], [1, 4, 2, 3])
    ax.set_title("My chart")

    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", dpi=150)
    plt.close(fig)
    b64 = base64.b64encode(buf.getvalue()).decode()

    return f"""<!DOCTYPE html>
<html><body style="margin:0;padding:16px;background:#fff;">
  <img src="data:image/png;base64,{b64}" style="max-width:100%;height:auto;">
</body></html>"""
```

## Using ctx.image instead

If you want the chart in the chat rather than the report pane:

```python
def build(ctx):
    import io, matplotlib.pyplot as plt
    fig, ax = plt.subplots()
    ax.bar(["A", "B", "C"], [3, 7, 5])
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    plt.close(fig)
    ctx.image(buf.getvalue(), "image/png")
    # return None — no report pane, chart lands inline in chat
```

## Multiple charts

```python
def build(ctx):
    import io, base64
    import matplotlib.pyplot as plt
    import matplotlib.gridspec as gridspec

    fig = plt.figure(figsize=(12, 5))
    gs = gridspec.GridSpec(1, 2)
    ax1 = fig.add_subplot(gs[0])
    ax2 = fig.add_subplot(gs[1])

    ax1.plot([1, 2, 3], [1, 4, 2])
    ax2.scatter([1, 2, 3], [3, 1, 4])

    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", dpi=150)
    plt.close(fig)
    b64 = base64.b64encode(buf.getvalue()).decode()

    return f'<html><body style="margin:0"><img src="data:image/png;base64,{b64}" style="width:100%"></body></html>'
```

## Theming

```python
def build(ctx):
    import io, base64
    import matplotlib.pyplot as plt

    t = ctx.theme()
    bg = t.get("--voitta-bg", "#ffffff")
    fg = t.get("--voitta-text", "#000000")
    accent = t.get("--voitta-accent", "#5b5fc7")

    plt.rcParams.update({
        "figure.facecolor": bg,
        "axes.facecolor": bg,
        "axes.edgecolor": fg,
        "text.color": fg,
        "xtick.color": fg,
        "ytick.color": fg,
    })

    fig, ax = plt.subplots(figsize=(8, 4))
    ax.plot([1, 2, 3], [1, 4, 2], color=accent, linewidth=2)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    plt.close(fig)
    b64 = base64.b64encode(buf.getvalue()).decode()

    return f'<html><body style="margin:0;background:{bg}"><img src="data:image/png;base64,{b64}" style="width:100%"></body></html>'
```

## Notes

- Always call `plt.close(fig)` to avoid memory leaks between runs.
- Use `dpi=150` for crisp images in the iframe.
- `bbox_inches="tight"` prevents whitespace cropping issues.
- numpy is available for data generation; don't use `random` module for reproducible charts.
