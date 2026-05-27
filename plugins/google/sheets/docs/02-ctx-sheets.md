# ctx.sheets — Sheets API access from scripts

`ctx.sheets` is a `SheetsClient` injected into every script's context when
Google OAuth has the `spreadsheets` scope active. It gives scripts full
read/write/format access to Google Sheets without any auth plumbing.

## Prerequisites

- Google OAuth connected with the `spreadsheets` scope (reconnect via the
  Drive settings panel if tools return `insufficient_scope`)
- Script must be run on a `docs.google.com` page, or `spreadsheet_id` passed
  via `ctx.args`

## Getting spreadsheet_id

The LLM supplies `spreadsheet_id` through `run_script`'s `args` field.
Inside the script, read it as:

```python
sid = ctx.args["spreadsheet_id"]
```

To discover it at runtime from the page URL, call `sheets_get_page_context`
as a tool turn before `run_script`.

## All methods (sync — no await needed)

### `ctx.sheets.get_metadata(spreadsheet_id)`

Returns spreadsheet title and list of sheets:

```python
meta = ctx.sheets.get_metadata(sid)
# meta = {
#   "spreadsheet_id": "...",
#   "title": "My Sheet",
#   "sheets": [
#     {"sheet_id": 0, "title": "Sheet1", "index": 0, "row_count": 1000, "col_count": 26},
#     ...
#   ]
# }
sheet_id = meta["sheets"][0]["sheet_id"]
```

### `ctx.sheets.read_range(spreadsheet_id, range, value_render="FORMATTED_VALUE")`

Returns **a list of lists directly** — NOT a dict envelope. Do not call `.get("values")` on the result.

```python
rows = ctx.sheets.read_range(sid, "Sheet1!A1:D20")
# rows = [["Name", "Score", ...], ["Alice", "95", ...], ...]
# ✓ correct:  for row in rows: ...
# ✗ wrong:    rows.get("values", [])   ← AttributeError

# Read raw numbers (not formatted strings):
rows = ctx.sheets.read_range(sid, "Sheet1!A1:D20", value_render="UNFORMATTED_VALUE")

# Read formulas:
rows = ctx.sheets.read_range(sid, "Sheet1!A1:D20", value_render="FORMULA")
```

`value_render` options: `"FORMATTED_VALUE"` (default) | `"UNFORMATTED_VALUE"` | `"FORMULA"`

### `ctx.sheets.write_range(spreadsheet_id, range, values, value_input_mode="USER_ENTERED")`

Overwrites a range with a list of lists:

```python
ctx.sheets.write_range(sid, "Sheet1!A1", [["Name", "Score"], ["Alice", 95]])

# Write a formula:
ctx.sheets.write_range(sid, "Sheet1!E2", [["=SUM(B2:D2)"]])
```

`value_input_mode`: `"USER_ENTERED"` (default, interprets formulas) | `"RAW"`

### `ctx.sheets.append_rows(spreadsheet_id, range, values, value_input_mode="USER_ENTERED")`

Appends rows after the last non-empty row in the table:

```python
ctx.sheets.append_rows(sid, "Sheet1!A1", [["Bob", 87], ["Carol", 92]])
```

### `ctx.sheets.format_range(spreadsheet_id, sheet_id, range, **kwargs)`

Applies cell formatting. `range` is A1 notation **without** sheet prefix.
`sheet_id` is the numeric GID from `get_metadata()`.

```python
sheet_id = meta["sheets"][0]["sheet_id"]

# Bold header row, blue background, white text:
ctx.sheets.format_range(sid, sheet_id, "A1:Z1",
    bold=True,
    background_color="#1a73e8",
    font_color="#ffffff",
)

# Borders around a data range:
ctx.sheets.format_range(sid, sheet_id, "A1:D10",
    borders={
        "top":    {"style": "SOLID", "color": "#000000"},
        "bottom": {"style": "SOLID", "color": "#000000"},
        "left":   {"style": "SOLID", "color": "#000000"},
        "right":  {"style": "SOLID", "color": "#000000"},
    }
)
```

All kwargs are optional — only supplied ones are written:

| kwarg | type | example |
|---|---|---|
| `background_color` | `'#RRGGBB'` | `'#fce8b2'` |
| `font_color` | `'#RRGGBB'` | `'#ff0000'` |
| `bold` | `bool` | `True` |
| `italic` | `bool` | `True` |
| `font_size` | `int` | `12` |
| `font_family` | `str` | `'Arial'` |
| `horizontal_alignment` | `'LEFT'\|'CENTER'\|'RIGHT'` | `'CENTER'` |
| `vertical_alignment` | `'TOP'\|'MIDDLE'\|'BOTTOM'` | `'MIDDLE'` |
| `wrap_strategy` | `'OVERFLOW_CELL'\|'WRAP'\|'CLIP'` | `'WRAP'` |
| `borders` | `dict` | see above |

### `ctx.sheets.conditional_format(spreadsheet_id, sheet_id, range, **kwargs)`

Adds a conditional formatting rule.

```python
# Highlight cells > 90 in green:
ctx.sheets.conditional_format(sid, sheet_id, "B2:B100",
    rule_type="single_color",
    condition_type="NUMBER_GREATER",
    condition_values=["90"],
    background_color="#b7e1cd",
)

# Color scale: white → blue across a range:
ctx.sheets.conditional_format(sid, sheet_id, "C2:C100",
    rule_type="color_scale",
    min_color="#ffffff",
    max_color="#1a73e8",
)

# Custom formula (highlight entire row if column D is "DONE"):
ctx.sheets.conditional_format(sid, sheet_id, "A2:Z100",
    condition_type="CUSTOM_FORMULA",
    condition_values=["=$D2=\"DONE\""],
    background_color="#e6f4ea",
)
```

### `ctx.sheets.merge_cells(spreadsheet_id, sheet_id, range, *, merge_type="MERGE_ALL", unmerge=False)`

```python
ctx.sheets.merge_cells(sid, sheet_id, "A1:D1")                        # merge header
ctx.sheets.merge_cells(sid, sheet_id, "A1:D1", unmerge=True)          # unmerge
ctx.sheets.merge_cells(sid, sheet_id, "A1:D4", merge_type="MERGE_ROWS")  # each row
```

### `ctx.sheets.resize_columns(spreadsheet_id, sheet_id, columns)`

```python
# Column A = 200px, columns B–D = 100px each:
ctx.sheets.resize_columns(sid, sheet_id, [
    {"index": 0, "width_px": 200},      # A (0-based)
    {"start": 1, "end": 3, "width_px": 100},  # B–D
])
```

### `ctx.sheets.resize_rows(spreadsheet_id, sheet_id, rows)`

```python
# Row 1 (header) = 40px, rows 2–50 = 24px:
ctx.sheets.resize_rows(sid, sheet_id, [
    {"index": 0, "height_px": 40},
    {"start": 1, "end": 49, "height_px": 24},
])
```

### `ctx.sheets.batch_update(spreadsheet_id, requests)`

Raw escape hatch for any Sheets API batchUpdate request not covered above:

```python
ctx.sheets.batch_update(sid, [{
    "updateSheetProperties": {
        "properties": {"sheetId": 0, "title": "Summary"},
        "fields": "title",
    }
}])
```

## When ctx.sheets is unavailable

If OAuth is not connected or the `spreadsheets` scope is missing, every
method raises:

```
RuntimeError: ctx.sheets is not available: Google OAuth 'spreadsheets' scope
is not active. Connect Google OAuth via the Drive settings panel, then run
the script on a docs.google.com page.
```

The stub is injected during `smoke_test` too, so scripts that call
`ctx.sheets` without being on a Sheets page will fail smoke_test with this
message rather than a cryptic `AttributeError`.
