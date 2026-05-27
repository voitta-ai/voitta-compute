# ctx.sheets — raw Sheets API v4 access from scripts

`ctx.sheets` exposes three HTTP methods that map directly to the Google
Sheets API v4. Auth is injected automatically from the stored OAuth token.

## Methods

```python
ctx.sheets.get(path, **params)         # GET  spreadsheets/{path}?params
ctx.sheets.post(path, body, **params)  # POST spreadsheets/{path}?params
ctx.sheets.put(path, body, **params)   # PUT  spreadsheets/{path}?params
ctx.sheets.get_metadata(spreadsheet_id)  # convenience: parse sheet structure
```

All methods are **synchronous** inside `build(ctx)`. They raise
`httpx.HTTPStatusError` on 4xx/5xx responses.

---

## get_metadata — start here

Always call this first to get sheet names and numeric IDs.

```python
meta = ctx.sheets.get_metadata(sid)
# {
#   "spreadsheet_id": "...",
#   "title": "My Spreadsheet",
#   "sheets": [
#     {"sheet_id": 0, "title": "Sheet1", "index": 0,
#      "row_count": 1000, "col_count": 26},
#     ...
#   ]
# }
sheet_id = meta["sheets"][0]["sheet_id"]   # numeric GID for batchUpdate
sheet_name = meta["sheets"][0]["title"]    # name for A1 range notation
```

---

## Reading cells — values.get

```python
data = ctx.sheets.get(f"{sid}/values/Sheet1!A1:D20")
rows = data.get("values", [])   # list of lists; short rows not padded

# Options:
data = ctx.sheets.get(
    f"{sid}/values/Sheet1!A1:D20",
    valueRenderOption="UNFORMATTED_VALUE",  # raw numbers, not display strings
    # valueRenderOption="FORMULA"           # return formulas
    # majorDimension="COLUMNS"             # column-major instead of row-major
)
```

## Reading multiple ranges — values.batchGet

```python
data = ctx.sheets.get(
    f"{sid}/values:batchGet",
    ranges=["Sheet1!A1:D5", "Sheet2!B2:E10"],
    valueRenderOption="UNFORMATTED_VALUE",
)
for vr in data.get("valueRanges", []):
    print(vr["range"], vr.get("values", []))
```

---

## Writing cells — values.update

```python
ctx.sheets.put(
    f"{sid}/values/Sheet1!A1",
    {"range": "Sheet1!A1", "values": [["Name", "Score"], ["Alice", 95]]},
    valueInputOption="USER_ENTERED",  # formulas interpreted; or "RAW"
)
```

## Writing multiple ranges — values.batchUpdate

```python
ctx.sheets.post(
    f"{sid}/values:batchUpdate",
    {
        "valueInputOption": "USER_ENTERED",
        "data": [
            {"range": "Sheet1!A1", "values": [["Header"]]},
            {"range": "Sheet2!B2", "values": [[1, 2, 3]]},
        ],
    },
)
```

## Appending rows — values.append

```python
ctx.sheets.post(
    f"{sid}/values/Sheet1!A1:append",
    {"values": [["Bob", 87], ["Carol", 92]]},
    valueInputOption="USER_ENTERED",
    insertDataOption="INSERT_ROWS",  # or "OVERWRITE"
)
```

## Clearing cells — values.clear / values.batchClear

```python
ctx.sheets.post(f"{sid}/values/Sheet1!A1:D20:clear", {})

ctx.sheets.post(f"{sid}/values:batchClear",
                {"ranges": ["Sheet1!A1:D20", "Sheet2!B2:E10"]})
```

---

## Formatting and mutations — spreadsheets.batchUpdate

All structural and formatting changes go through one endpoint:

```python
ctx.sheets.post(f"{sid}:batchUpdate", {"requests": [
    { ... },  # one or more Request objects (see 04-api-batch-requests.md)
]})
```

### Bold header row

```python
ctx.sheets.post(f"{sid}:batchUpdate", {"requests": [{
    "repeatCell": {
        "range": {
            "sheetId": sheet_id,
            "startRowIndex": 0, "endRowIndex": 1,
            "startColumnIndex": 0, "endColumnIndex": 5,
        },
        "cell": {
            "userEnteredFormat": {
                "textFormat": {"bold": True},
                "backgroundColor": {"red": 0.2, "green": 0.4, "blue": 0.8},
            }
        },
        "fields": "userEnteredFormat.textFormat.bold,userEnteredFormat.backgroundColor",
    }
}]})
```

### Freeze top row

```python
ctx.sheets.post(f"{sid}:batchUpdate", {"requests": [{
    "updateSheetProperties": {
        "properties": {
            "sheetId": sheet_id,
            "gridProperties": {"frozenRowCount": 1},
        },
        "fields": "gridProperties.frozenRowCount",
    }
}]})
```

### Color scale conditional format

```python
ctx.sheets.post(f"{sid}:batchUpdate", {"requests": [{
    "addConditionalFormatRule": {
        "rule": {
            "ranges": [{
                "sheetId": sheet_id,
                "startRowIndex": 1, "endRowIndex": 100,
                "startColumnIndex": 2, "endColumnIndex": 3,
            }],
            "gradientRule": {
                "minpoint": {"color": {"red": 1, "green": 1, "blue": 1}, "type": "MIN"},
                "maxpoint": {"color": {"red": 0.1, "green": 0.7, "blue": 0.3}, "type": "MAX"},
            },
        },
        "index": 0,
    }
}]})
```

### Resize column A to 200px

```python
ctx.sheets.post(f"{sid}:batchUpdate", {"requests": [{
    "updateDimensionProperties": {
        "range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                  "startIndex": 0, "endIndex": 1},
        "properties": {"pixelSize": 200},
        "fields": "pixelSize",
    }
}]})
```

---

## Key types

### GridRange
All indices are **0-based, end-exclusive**.
```python
{
    "sheetId": 0,
    "startRowIndex": 0,   # row 1 in UI
    "endRowIndex": 5,     # up to but not including row 6
    "startColumnIndex": 0,  # column A
    "endColumnIndex": 4,    # up to but not including column E
}
```

### Color (RGB floats 0–1)
```python
{"red": 1.0, "green": 0.0, "blue": 0.0}          # red
{"red": 0.102, "green": 0.451, "blue": 0.816}     # Google blue #1a73e8
```

Convert hex to float: `int("1a", 16) / 255 = 0.102`

### Fields mask
`fields` tells the API which fields to update; others are left unchanged.
Use `"*"` to update everything in the object.
```python
"fields": "userEnteredFormat.textFormat.bold"
"fields": "userEnteredFormat.backgroundColor,userEnteredFormat.textFormat.bold"
"fields": "userEnteredFormat"   # update everything in userEnteredFormat
```

---

## Smoke-test guard

Scripts with `ctx.args` must guard against empty args in smoke-test:

```python
def build(ctx):
    sid = ctx.args.get("spreadsheet_id")
    if not sid:
        return None
    # ... rest of script
```
