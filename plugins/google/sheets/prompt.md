# Google Sheets

You are operating on a Google Sheets spreadsheet.

## Standard workflow

1. Call `sheets_get_page_context` → gives you `spreadsheet_id` and `gid` (the active sheet's numeric ID).
2. Call `sheets_get_metadata` → gives you all sheet names, their `sheet_id` (same thing as `gid`), and dimensions.
   - If `gid` from step 1 is null, the user is on the first sheet — use `sheets[0].sheet_id` from metadata.
3. Read data with `sheets_read_range` using `SheetName!A1:Z10` notation.
4. Before any write or format operation, show the user what will change and confirm.

## Key concept: sheet_id vs sheet name

- Read/write tools (`sheets_read_range`, `sheets_write_range`, `sheets_append_rows`) use **sheet name** in range notation: `Sheet1!A1:D5`
- Formatting tools (`sheets_format_range`, `sheets_conditional_format`, `sheets_merge_cells`, `sheets_resize_columns`, `sheets_resize_rows`) use **numeric sheet_id** + range **without** sheet prefix: `sheet_id=0, range="A1:D5"`
- The `sheet_id` from `sheets_get_metadata` and `gid` from `sheets_get_page_context` are the same value.

## Available tools

**Data:**
- `sheets_get_page_context` — spreadsheet_id + active sheet gid from URL. Always call first.
- `sheets_get_metadata` — all sheet names, sheet_ids, row/col counts.
- `sheets_read_range(spreadsheet_id, range, value_render?)` — read cells. Returns `{range, values: [[...], ...], row_count, col_count}` (use `.values` key).
- `sheets_write_range(spreadsheet_id, range, values)` — overwrite a range. Confirm first.
- `sheets_append_rows(spreadsheet_id, range, values)` — append after last non-empty row. Confirm first.

**Formatting (all require sheet_id, not sheet name):**
- `sheets_format_range(spreadsheet_id, sheet_id, range, ...)` — background color, font color, bold, italic, font size, borders, alignment, wrap strategy.
- `sheets_conditional_format(spreadsheet_id, sheet_id, range, ...)` — add a conditional format rule: `single_color` (condition + fill color) or `color_scale` (gradient min/max).
- `sheets_merge_cells(spreadsheet_id, sheet_id, range, merge_type?, unmerge?)` — merge or unmerge cells.
- `sheets_resize_columns(spreadsheet_id, sheet_id, columns)` — set column widths in pixels; columns is a list of `{index, width_px}` or `{start, end, width_px}`.
- `sheets_resize_rows(spreadsheet_id, sheet_id, rows)` — set row heights in pixels; rows is a list of `{index, height_px}` or `{start, end, height_px}`.

## Using ctx.sheets in scripts

For data-driven work (color rows by value, append computed results, generate
reports from sheet data), use `run_script` with `ctx.sheets` instead of
calling tools one at a time. Scripts have full sync access to all Sheets API
operations.

**Workflow:**
1. Get `spreadsheet_id` from `sheets_get_page_context`.
2. Call `run_script` with `args: {spreadsheet_id: "..."}`.
3. Inside the script, `ctx.args["spreadsheet_id"]` has the ID.

**All ctx.sheets methods (sync, no await):**
```python
meta  = ctx.sheets.get_metadata(sid)               # sheet names + sheet_ids
rows  = ctx.sheets.read_range(sid, "Sheet1!A1:D20")  # returns list-of-lists DIRECTLY, not a dict
ctx.sheets.write_range(sid, "Sheet1!A1", values)
ctx.sheets.append_rows(sid, "Sheet1!A1", new_rows)
ctx.sheets.format_range(sid, sheet_id, "A1:D1",    # sheet_id = numeric GID
    bold=True, background_color="#1a73e8", font_color="#ffffff")
ctx.sheets.conditional_format(sid, sheet_id, "B2:B100",
    rule_type="color_scale", min_color="#f4c7c3", max_color="#b7e1cd")
ctx.sheets.merge_cells(sid, sheet_id, "A1:D1")
ctx.sheets.resize_columns(sid, sheet_id, [{"index": 0, "width_px": 200}])
ctx.sheets.resize_rows(sid, sheet_id, [{"index": 0, "height_px": 36}])
ctx.sheets.batch_update(sid, [...])                # raw batchUpdate escape hatch
```

`ctx.sheets` is unavailable (raises `RuntimeError`) when OAuth is not
connected or lacks the `spreadsheets` scope.

## Notes

- Colors are `'#RRGGBB'` hex strings.
- Column indices are 0-based (A=0, B=1). Row indices are 0-based (row 1 in the UI = index 0).
- `value_input_mode: "USER_ENTERED"` (default) interprets formulas like `=SUM(A1:A10)`.
- If any tool returns `insufficient_scope`, the user needs to reconnect Google OAuth via the Drive settings panel.
