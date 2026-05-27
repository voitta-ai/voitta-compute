# Sheets API v4 — batchUpdate Request Types

All formatting and structural mutations go through:

```python
ctx.sheets.post(f"{sid}:batchUpdate", {"requests": [
    {REQUEST_TYPE: { ... }},
    ...
]})
```

Each item in `requests` is an object with exactly one key (the request type).
Multiple requests can be batched in one call.

---

## Cell content

### RepeatCellRequest
Apply data/format to every cell in a range.
```python
{"repeatCell": {
    "range": GridRange,
    "cell": CellData,           # see CellFormat below
    "fields": "userEnteredFormat.backgroundColor,userEnteredFormat.textFormat.bold",
}}
```

### UpdateCellsRequest
Write specific cell data row by row.
```python
{"updateCells": {
    "rows": [RowData, ...],
    "fields": "userEnteredValue,userEnteredFormat",
    "start": {"sheetId": 0, "rowIndex": 0, "columnIndex": 0},
    # or "range": GridRange  (unmatched cells cleared)
}}
```

### AppendCellsRequest
Append cells after the last row with data.
```python
{"appendCells": {
    "sheetId": 0,
    "rows": [RowData, ...],
    "fields": "userEnteredValue",
}}
```

---

## Sheet structure

### AddSheetRequest
```python
{"addSheet": {
    "properties": {"title": "New Sheet", "index": 1}
}}
```

### DeleteSheetRequest
```python
{"deleteSheet": {"sheetId": sheet_id}}
```

### DuplicateSheetRequest
```python
{"duplicateSheet": {
    "sourceSheetId": 0,
    "insertSheetIndex": 1,
    "newSheetName": "Copy of Sheet1",
}}
```

### UpdateSheetPropertiesRequest
```python
{"updateSheetProperties": {
    "properties": {
        "sheetId": 0,
        "title": "Renamed",
        "gridProperties": {"frozenRowCount": 1, "frozenColumnCount": 1},
        "tabColorStyle": {"rgbColor": {"red": 1.0, "green": 0.0, "blue": 0.0}},
    },
    "fields": "title,gridProperties.frozenRowCount",
}}
```

---

## Dimensions (rows/columns)

### InsertDimensionRequest
```python
{"insertDimension": {
    "range": {"sheetId": 0, "dimension": "ROWS",
              "startIndex": 2, "endIndex": 4},   # inserts 2 rows before row 3
    "inheritFromBefore": True,
}}
```

### DeleteDimensionRequest
```python
{"deleteDimension": {
    "range": {"sheetId": 0, "dimension": "COLUMNS",
              "startIndex": 1, "endIndex": 3},   # deletes columns B and C
}}
```

### AppendDimensionRequest
```python
{"appendDimension": {
    "sheetId": 0,
    "dimension": "ROWS",
    "length": 100,
}}
```

### MoveDimensionRequest
```python
{"moveDimension": {
    "source": {"sheetId": 0, "dimension": "COLUMNS",
               "startIndex": 3, "endIndex": 4},  # column D
    "destinationIndex": 0,                         # move to column A
}}
```

### UpdateDimensionPropertiesRequest
Resize rows or columns.
```python
{"updateDimensionProperties": {
    "range": {"sheetId": 0, "dimension": "COLUMNS",
              "startIndex": 0, "endIndex": 1},
    "properties": {"pixelSize": 200, "hidden": False},
    "fields": "pixelSize",
}}
```

### AutoResizeDimensionsRequest
```python
{"autoResizeDimensions": {
    "dimensions": {"sheetId": 0, "dimension": "COLUMNS",
                   "startIndex": 0, "endIndex": 5}
}}
```

---

## Merging

### MergeCellsRequest
```python
{"mergeCells": {
    "range": GridRange,
    "mergeType": "MERGE_ALL",  # "MERGE_COLUMNS" | "MERGE_ROWS"
}}
```

### UnmergeCellsRequest
```python
{"unmergeCells": {"range": GridRange}}
```

---

## Borders

### UpdateBordersRequest
```python
{"updateBorders": {
    "range": GridRange,
    "top":    {"style": "SOLID_MEDIUM", "colorStyle": {"rgbColor": {"red": 0, "green": 0, "blue": 0}}},
    "bottom": {"style": "SOLID_MEDIUM", "colorStyle": {"rgbColor": {"red": 0, "green": 0, "blue": 0}}},
    "left":   {"style": "SOLID", "colorStyle": {"rgbColor": {"red": 0.5, "green": 0.5, "blue": 0.5}}},
    "right":  {"style": "SOLID", "colorStyle": {"rgbColor": {"red": 0.5, "green": 0.5, "blue": 0.5}}},
    "innerHorizontal": {"style": "DOTTED"},
    "innerVertical":   {"style": "NONE"},
}}
```
Border styles: `SOLID` | `SOLID_MEDIUM` | `SOLID_THICK` | `DASHED` | `DOTTED` | `DOUBLE` | `NONE`

---

## Conditional formatting

### AddConditionalFormatRuleRequest

**Single color (boolean condition):**
```python
{"addConditionalFormatRule": {
    "rule": {
        "ranges": [GridRange],
        "booleanRule": {
            "condition": {
                "type": "NUMBER_GREATER",        # see ConditionType below
                "values": [{"userEnteredValue": "90"}],
            },
            "format": {
                "userEnteredFormat": {
                    "backgroundColor": {"red": 0.71, "green": 0.88, "blue": 0.80},
                }
            },
        },
    },
    "index": 0,
}}
```

**Color scale (gradient):**
```python
{"addConditionalFormatRule": {
    "rule": {
        "ranges": [GridRange],
        "gradientRule": {
            "minpoint": {"colorStyle": {"rgbColor": {"red": 0.96, "green": 0.80, "blue": 0.76}}, "type": "MIN"},
            "midpoint": {"colorStyle": {"rgbColor": {"red": 1.0,  "green": 1.0,  "blue": 1.0}},  "type": "PERCENTILE", "value": "50"},
            "maxpoint": {"colorStyle": {"rgbColor": {"red": 0.71, "green": 0.88, "blue": 0.80}}, "type": "MAX"},
        },
    },
    "index": 0,
}}
```

### ConditionType values
`NUMBER_GREATER` | `NUMBER_GREATER_THAN_EQ` | `NUMBER_LESS` | `NUMBER_LESS_THAN_EQ` |
`NUMBER_EQ` | `NUMBER_NOT_EQ` | `NUMBER_BETWEEN` | `NUMBER_NOT_BETWEEN` |
`TEXT_CONTAINS` | `TEXT_NOT_CONTAINS` | `TEXT_STARTS_WITH` | `TEXT_ENDS_WITH` |
`TEXT_EQ` | `TEXT_IS_EMAIL` | `TEXT_IS_URL` |
`DATE_EQ` | `DATE_BEFORE` | `DATE_AFTER` | `DATE_ON_OR_BEFORE` | `DATE_ON_OR_AFTER` |
`DATE_BETWEEN` | `DATE_NOT_BETWEEN` | `DATE_IS_VALID` | `ONE_OF_RANGE` | `ONE_OF_LIST` |
`BLANK` | `NOT_BLANK` | `CUSTOM_FORMULA` | `BOOLEAN`

### DeleteConditionalFormatRuleRequest
```python
{"deleteConditionalFormatRule": {"index": 0, "sheetId": sheet_id}}
```

---

## Data validation

### SetDataValidationRequest
```python
{"setDataValidation": {
    "range": GridRange,
    "rule": {
        "condition": {
            "type": "ONE_OF_LIST",
            "values": [
                {"userEnteredValue": "Yes"},
                {"userEnteredValue": "No"},
                {"userEnteredValue": "Maybe"},
            ],
        },
        "showCustomUi": True,
        "strict": True,
    },
}}
```

---

## Named ranges

### AddNamedRangeRequest
```python
{"addNamedRange": {
    "namedRange": {"name": "SalesData", "range": GridRange}
}}
```

### DeleteNamedRangeRequest
```python
{"deleteNamedRange": {"namedRangeId": "named_range_id_string"}}
```

---

## Sorting, find/replace, utilities

### SortRangeRequest
```python
{"sortRange": {
    "range": GridRange,
    "sortSpecs": [
        {"dimensionIndex": 1, "sortOrder": "ASCENDING"},   # sort by column B
        {"dimensionIndex": 0, "sortOrder": "DESCENDING"},  # then by column A
    ],
}}
```

### FindReplaceRequest
```python
{"findReplace": {
    "find": "foo",
    "replacement": "bar",
    "matchCase": False,
    "matchEntireCell": False,
    "searchByRegex": False,
    "allSheets": True,       # or "sheetId": 0 or "range": GridRange
}}
```

### TrimWhitespaceRequest
```python
{"trimWhitespace": {"range": GridRange}}
```

### DeleteDuplicatesRequest
```python
{"deleteDuplicates": {
    "range": GridRange,
    "comparisonColumns": [
        {"sheetId": 0, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1}
    ],
}}
```

### InsertRangeRequest
```python
{"insertRange": {
    "range": GridRange,
    "shiftDimension": "ROWS",    # push existing rows down; or "COLUMNS"
}}
```

### DeleteRangeRequest
```python
{"deleteRange": {
    "range": GridRange,
    "shiftDimension": "ROWS",    # pull rows up; or "COLUMNS"
}}
```

### CopyPasteRequest
```python
{"copyPaste": {
    "source": GridRange,
    "destination": GridRange,
    "pasteType": "PASTE_NORMAL",    # see PasteType enum
    "pasteOrientation": "NORMAL",   # or "TRANSPOSE"
}}
```

### UpdateSpreadsheetPropertiesRequest
```python
{"updateSpreadsheetProperties": {
    "properties": {"title": "New Spreadsheet Title"},
    "fields": "title",
}}
```

---

## CellFormat (for repeatCell / updateCells)

```python
"userEnteredFormat": {
    "backgroundColor":      Color,           # deprecated; use backgroundColorStyle
    "backgroundColorStyle": ColorStyle,
    "borders":              Borders,
    "padding":              {"top": 4, "right": 4, "bottom": 4, "left": 4},
    "horizontalAlignment":  "LEFT" | "CENTER" | "RIGHT",
    "verticalAlignment":    "TOP" | "MIDDLE" | "BOTTOM",
    "wrapStrategy":         "OVERFLOW_CELL" | "WRAP" | "CLIP",
    "textFormat": {
        "foregroundColorStyle": ColorStyle,
        "fontFamily":  "Arial",
        "fontSize":    12,
        "bold":        True,
        "italic":      False,
        "strikethrough": False,
        "underline":   False,
    },
    "numberFormat": {
        "type":    "NUMBER" | "TEXT" | "PERCENT" | "CURRENCY" | "DATE" |
                   "TIME" | "DATE_TIME" | "SCIENTIFIC",
        "pattern": "0.00" | "MMM dd yyyy" | "$#,##0.00" | ...,
    },
    "textRotation": {"angle": 45},           # or {"vertical": True}
}
```

## GridRange reference

```python
{
    "sheetId":          0,     # numeric GID from get_metadata()
    "startRowIndex":    0,     # 0-based, inclusive (row 1 = index 0)
    "endRowIndex":      5,     # 0-based, exclusive (up to row 5)
    "startColumnIndex": 0,     # 0-based, inclusive (col A = index 0)
    "endColumnIndex":   4,     # 0-based, exclusive (up to col D)
}
```

## Color reference

```python
# Use ColorStyle (not deprecated Color) for new code:
{"rgbColor": {"red": 0.102, "green": 0.451, "blue": 0.816}}   # #1a73e8

# Convert hex to float:  int("1a", 16) / 255  →  0.102
```
