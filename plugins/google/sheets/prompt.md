# Google Sheets

You are operating on a Google Sheets spreadsheet.

## Standard workflow

1. Call `sheets_get_page_context` → gives you `spreadsheet_id` and `gid` (active sheet numeric GID).
2. Call `sheets_get_metadata` → all sheet names and their `sheet_id` (same as `gid`).
3. Read data with `sheets_read_range` using `SheetName!A1:Z10` notation.
4. Before any write, show the user what will change and confirm.

## Available LLM tools (quick in-chat operations)

- `sheets_get_page_context` — spreadsheet_id + active sheet gid from URL.
- `sheets_get_metadata` — all sheet names, sheet_ids, row/col counts.
- `sheets_read_range(spreadsheet_id, range, value_render?)` — returns `{range, values, row_count, col_count}`.
- `sheets_write_range(spreadsheet_id, range, values)` — overwrite a range. Confirm first.
- `sheets_append_rows(spreadsheet_id, range, values)` — append after last non-empty row. Confirm first.

## ctx.sheets in scripts — full API access

For anything beyond simple reads/writes — formatting, conditional formatting,
freeze rows, data validation, hide columns, charts, etc. — write a script
using `ctx.sheets`. Three raw HTTP methods, auth injected automatically:

```python
ctx.sheets.get(path, **params)        # GET  spreadsheets/{path}
ctx.sheets.post(path, body, **params) # POST spreadsheets/{path}
ctx.sheets.put(path, body, **params)  # PUT  spreadsheets/{path}
ctx.sheets.get_metadata(sid)          # parse sheet structure
```

### Before writing any script that uses ctx.sheets

1. `rag_query corpus="docs" query="ctx sheets api"` → `03-api-ctx-sheets.md`
   Full usage examples for all values operations and common batchUpdate patterns.

2. `rag_query corpus="docs" query="sheets batchUpdate requests"` → `04-api-batch-requests.md`
   Every batchUpdate request type with field names and examples.

### Quick examples

**Read cells:**
```python
data = ctx.sheets.get(f"{sid}/values/Sheet1!A1:D20",
                      valueRenderOption="UNFORMATTED_VALUE")
rows = data.get("values", [])
```

**Write cells:**
```python
ctx.sheets.put(f"{sid}/values/Sheet1!A1",
               {"range": "Sheet1!A1", "values": [["Name", "Score"]]},
               valueInputOption="USER_ENTERED")
```

**Any batchUpdate:**
```python
ctx.sheets.post(f"{sid}:batchUpdate", {"requests": [
    { ... }   # see 04-api-batch-requests.md for all request types
]})
```

**Clear a range:**
```python
ctx.sheets.post(f"{sid}/values/Sheet1!A1:D20:clear", {})
```

### Script smoke-test guard

```python
def build(ctx):
    sid = ctx.args.get("spreadsheet_id")
    if not sid:
        return None
    ...
```

## Key concept: sheet_id vs sheet name

- Read/write tools and `values.*` calls use **sheet name** in range notation: `Sheet1!A1:D5`
- batchUpdate `GridRange` uses **numeric sheet_id** + 0-based row/column indices
- Get the numeric sheet_id from `get_metadata()["sheets"][n]["sheet_id"]`
