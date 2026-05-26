# Recipe: HTML tables and KPI cards

Pure HTML/CSS — no library needed.

## Themed table

```python
def build(ctx):
    rows = [
        {"region": "EMEA", "revenue": 1.2, "growth": "+8%"},
        {"region": "Americas", "revenue": 2.4, "growth": "+12%"},
        {"region": "APAC", "revenue": 0.9, "growth": "+5%"},
    ]
    t = ctx.theme()

    body = "".join(
        f'<tr><td>{r["region"]}</td>'
        f'<td>${r["revenue"]}B</td>'
        f'<td>{r["growth"]}</td></tr>'
        for r in rows
    )

    return f"""<!doctype html>
<html>
<head><style>
  body {{ background: {t.get("--voitta-bg", "#fff")};
          color: {t.get("--voitta-text", "#000")};
          font-family: system-ui; margin: 0; padding: 24px; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th, td {{ padding: 12px 16px; text-align: left;
            border-bottom: 1px solid {t.get("--voitta-border", "#ddd")}; }}
  th {{ background: {t.get("--voitta-surface", "#f5f5f7")};
        font-weight: 600; letter-spacing: 0.4px;
        text-transform: uppercase; font-size: 11px; }}
  tr:hover td {{ background: {t.get("--voitta-surface", "#f5f5f7")}; }}
</style></head>
<body>
  <table>
    <thead>
      <tr><th>Region</th><th>Revenue</th><th>YoY Growth</th></tr>
    </thead>
    <tbody>{body}</tbody>
  </table>
</body>
</html>"""
```

## KPI cards (CSS grid)

```python
def build(ctx):
    t = ctx.theme()
    accent = t.get("--voitta-accent", "#0a84ff")
    ok = t.get("--voitta-ok-fg", "#10b981")
    warn = t.get("--voitta-warn-fg", "#f59e0b")

    return f"""<!doctype html>
<html>
<head><style>
  body {{ background: {t.get("--voitta-bg", "#fff")};
          color: {t.get("--voitta-text", "#000")};
          font-family: system-ui; margin: 0; padding: 24px; }}
  .grid {{ display: grid;
           grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
           gap: 16px; }}
  .card {{ background: {t.get("--voitta-surface", "#f5f5f7")};
           border: 1px solid {t.get("--voitta-border", "#ddd")};
           border-radius: 8px; padding: 20px; }}
  .label {{ font-size: 11px; font-weight: 600;
            letter-spacing: 0.8px; text-transform: uppercase;
            color: {t.get("--voitta-text-muted", "#666")};
            margin-bottom: 8px; }}
  .value {{ font-size: 28px; font-weight: 700; margin-bottom: 4px; }}
  .delta {{ font-size: 13px; font-weight: 500; }}
  .delta-up {{ color: {ok}; }}
  .delta-down {{ color: {warn}; }}
</style></head>
<body>
  <div class="grid">
    <div class="card">
      <div class="label">Total Revenue</div>
      <div class="value" style="color: {accent}">$4.5B</div>
      <div class="delta delta-up">↑ 8.5% YoY</div>
    </div>
    <div class="card">
      <div class="label">Active Customers</div>
      <div class="value">12,430</div>
      <div class="delta delta-up">↑ 240 this month</div>
    </div>
    <div class="card">
      <div class="label">Churn Rate</div>
      <div class="value">2.1%</div>
      <div class="delta delta-down">↑ 0.3% (worse)</div>
    </div>
  </div>
</body>
</html>"""
```

## Mixing KPI cards + chart + table in one report

```python
return f"""<!doctype html>
<html>
<head>
  <script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
  <style>... (theme + grid styles)</style>
</head>
<body>
  <div class="grid">
    <div class="card">...</div>
    <div class="card">...</div>
  </div>
  <div id="chart" style="height: 320px; margin-top: 24px"></div>
  <table>...</table>
  <script>
    Plotly.newPlot("chart", chart_spec.data, chart_spec.layout);
  </script>
</body>
</html>"""
```

Composition is yours. One HTML doc, anything inside.
