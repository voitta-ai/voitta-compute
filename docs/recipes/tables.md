# Recipe: HTML tables and KPI cards

Pure HTML/CSS — no JavaScript library needed.

## Basic themed table

```python
def build(ctx):
    t = ctx.theme()
    bg     = t.get("--voitta-bg",      "#ffffff")
    text   = t.get("--voitta-text",    "#111111")
    border = t.get("--voitta-border",  "#e0e0e0")
    accent = t.get("--voitta-accent",  "#5b5fc7")

    rows = [
        ("Alice", "Engineering", "$120k"),
        ("Bob",   "Design",      "$110k"),
        ("Carol", "Product",     "$130k"),
    ]
    trs = "".join(
        f"<tr><td>{name}</td><td>{dept}</td><td>{sal}</td></tr>"
        for name, dept, sal in rows
    )

    return f"""<!DOCTYPE html>
<html>
<head>
<style>
  body {{ margin: 0; padding: 16px; background: {bg}; color: {text}; font-family: sans-serif; }}
  table {{ width: 100%; border-collapse: collapse; }}
  th {{ background: {accent}; color: #fff; padding: 10px 12px; text-align: left; }}
  td {{ padding: 8px 12px; border-bottom: 1px solid {border}; }}
  tr:hover td {{ background: {border}; }}
</style>
</head>
<body>
<table>
  <thead><tr><th>Name</th><th>Department</th><th>Salary</th></tr></thead>
  <tbody>{trs}</tbody>
</table>
</body></html>"""
```

## From a DataFrame

```python
def build(ctx):
    df = ctx.dataframe("my-snapshot")
    t = ctx.theme()
    bg     = t.get("--voitta-bg",     "#fff")
    text   = t.get("--voitta-text",   "#111")
    border = t.get("--voitta-border", "#e0e0e0")
    accent = t.get("--voitta-accent", "#5b5fc7")

    headers = "".join(f"<th>{col}</th>" for col in df.columns)
    rows = "".join(
        "<tr>" + "".join(f"<td>{v}</td>" for v in row) + "</tr>"
        for row in df.head(50).itertuples(index=False)
    )

    return f"""<!DOCTYPE html>
<html><head>
<style>
  body{{margin:0;padding:16px;background:{bg};color:{text};font-family:sans-serif}}
  table{{width:100%;border-collapse:collapse}}
  th{{background:{accent};color:#fff;padding:8px 10px;text-align:left}}
  td{{padding:6px 10px;border-bottom:1px solid {border}}}
</style>
</head><body>
<table><thead><tr>{headers}</tr></thead><tbody>{rows}</tbody></table>
</body></html>"""
```

## KPI cards

```python
def build(ctx):
    t = ctx.theme()
    bg     = t.get("--voitta-bg",     "#ffffff")
    text   = t.get("--voitta-text",   "#111111")
    card   = t.get("--voitta-card",   "#f5f5f5")
    accent = t.get("--voitta-accent", "#5b5fc7")

    kpis = [
        ("Revenue",   "$1.2M",  "+12%"),
        ("Users",     "84,200", "+5%"),
        ("Churn",     "2.1%",   "-0.3%"),
        ("NPS",       "62",     "+4"),
    ]
    cards = "".join(f"""
      <div class="card">
        <div class="label">{label}</div>
        <div class="value">{value}</div>
        <div class="delta">{delta}</div>
      </div>""" for label, value, delta in kpis)

    return f"""<!DOCTYPE html>
<html><head>
<style>
  body {{ margin: 0; padding: 16px; background: {bg}; font-family: sans-serif; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; }}
  .card {{ background: {card}; border-radius: 8px; padding: 16px; }}
  .label {{ font-size: 12px; color: {text}; opacity: 0.6; margin-bottom: 4px; }}
  .value {{ font-size: 28px; font-weight: 700; color: {accent}; }}
  .delta {{ font-size: 12px; color: {text}; margin-top: 4px; }}
</style>
</head><body>
<div class="grid">{cards}</div>
</body></html>"""
```

## Notes

- Limit DataFrame tables to ~50 rows — large tables make the iframe very tall.
- For sortable/filterable tables consider Alpine.js (see `interactivity.md`).
- KPI card grids use CSS Grid with `auto-fit` — they reflow cleanly at any iframe width.
