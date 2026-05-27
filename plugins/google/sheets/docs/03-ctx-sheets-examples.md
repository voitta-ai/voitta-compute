# ctx.sheets — worked examples

## Example A: Color rows by value

Read a score column, apply green/yellow/red background per row based on
the value, resize the header row, and bold the header.

```python
def build(ctx):
    sid = ctx.args["spreadsheet_id"]

    meta = ctx.sheets.get_metadata(sid)
    sheet_id = meta["sheets"][0]["sheet_id"]
    sheet_name = meta["sheets"][0]["title"]

    # Read data (rows include header)
    rows = ctx.sheets.read_range(sid, f"{sheet_name}!A1:D50",
                                 value_render="UNFORMATTED_VALUE")
    if not rows:
        return None

    # Bold + resize header
    ctx.sheets.format_range(sid, sheet_id, f"A1:D1",
                            bold=True, background_color="#1a73e8",
                            font_color="#ffffff", horizontal_alignment="CENTER")
    ctx.sheets.resize_rows(sid, sheet_id, [{"index": 0, "height_px": 36}])

    # Color data rows by score in column B (index 1)
    for i, row in enumerate(rows[1:], start=2):   # 1-based, skip header
        try:
            score = float(row[1]) if len(row) > 1 else None
        except (TypeError, ValueError):
            score = None
        if score is None:
            continue
        color = "#b7e1cd" if score >= 90 else "#fce8b2" if score >= 70 else "#f4c7c3"
        ctx.sheets.format_range(sid, sheet_id, f"A{i}:D{i}",
                                background_color=color)

    ctx.text(f"Colored {len(rows)-1} rows in {sheet_name}.")
    return None   # no HTML report needed
```

---

## Example B: Append computed results and write a summary

Run a computation on sheet data, append the results, then write a summary
cell.

```python
def build(ctx):
    sid = ctx.args["spreadsheet_id"]

    meta = ctx.sheets.get_metadata(sid)
    sheet_name = meta["sheets"][0]["title"]

    rows = ctx.sheets.read_range(sid, f"{sheet_name}!A1:C100",
                                 value_render="UNFORMATTED_VALUE")

    # Skip header, compute totals per row
    new_rows = []
    for row in rows[1:]:
        try:
            vals = [float(v) for v in row]
            new_rows.append([sum(vals), sum(vals) / len(vals)])
        except (TypeError, ValueError):
            new_rows.append(["", ""])

    # Append computed columns to a Results sheet (must exist)
    ctx.sheets.append_rows(sid, "Results!A1", new_rows)

    # Write a summary formula in a fixed cell
    n = len(new_rows)
    ctx.sheets.write_range(sid, "Results!D1",
                           [[f"=AVERAGE(A1:A{n})"]])

    ctx.text(f"Appended {n} result rows and wrote AVERAGE formula.")
    return None
```

---

## Example C: Build an HTML report from sheet data

Read data, render it as an HTML table in the report pane, and also apply
a color-scale conditional format to a numeric column.

```python
def build(ctx):
    sid = ctx.args["spreadsheet_id"]

    meta = ctx.sheets.get_metadata(sid)
    sheet_id = meta["sheets"][0]["sheet_id"]
    sheet_name = meta["sheets"][0]["title"]

    rows = ctx.sheets.read_range(sid, f"{sheet_name}!A1:E30")

    if not rows:
        return "<p>No data found.</p>"

    header, data = rows[0], rows[1:]

    # Apply color scale to column B (index 1) — numeric scores
    ctx.sheets.conditional_format(sid, sheet_id, "B2:B30",
        rule_type="color_scale",
        min_color="#f4c7c3",
        mid_color="#fce8b2",
        max_color="#b7e1cd",
    )

    # Build HTML table
    t = ctx.theme()
    bg  = t.get("--voitta-bg",   "#ffffff")
    txt = t.get("--voitta-text", "#000000")
    acc = t.get("--voitta-accent","#1a73e8")

    header_cells = "".join(f"<th>{h}</th>" for h in header)
    body_rows = ""
    for row in data:
        cells = "".join(f"<td>{c}</td>" for c in row)
        body_rows += f"<tr>{cells}</tr>"

    return f"""<!DOCTYPE html>
<html>
<head>
<style>
  body {{ background:{bg}; color:{txt}; font-family:sans-serif; padding:16px; }}
  table {{ border-collapse:collapse; width:100%; }}
  th {{ background:{acc}; color:#fff; padding:8px 12px; text-align:left; }}
  td {{ padding:6px 12px; border-bottom:1px solid #ddd; }}
  tr:hover td {{ background:#f5f5f5; }}
</style>
</head>
<body>
<h2>{meta["title"]}</h2>
<table>
  <thead><tr>{header_cells}</tr></thead>
  <tbody>{body_rows}</tbody>
</table>
</body>
</html>"""
```
