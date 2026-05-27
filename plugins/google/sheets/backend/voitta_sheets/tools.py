"""Google Sheets tools — cell-level read/write + formatting via Sheets API v4.

Eight LLM-facing tools, all gated on google_oauth.has_sheets_scope().

Values:
  • sheets_get_metadata       — list sheets, row/col counts
  • sheets_read_range         — read cells as list-of-lists
  • sheets_write_range        — overwrite a range
  • sheets_append_rows        — append rows after last non-empty row

Formatting (all via batchUpdate):
  • sheets_format_range       — background/font color, bold/italic, borders
  • sheets_conditional_format — add a conditional formatting rule
  • sheets_merge_cells        — merge or unmerge a range
  • sheets_resize_columns     — set column widths (pixels)
  • sheets_resize_rows        — set row heights (pixels)

Auth: every call awaits google_oauth.get_access_token() which
auto-refreshes if the access token is within 60 s of expiry. On 401
the error surfaces with a hint to re-connect / re-authorise.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.services import google_oauth
from app.tools.registry import ToolCtx, ToolSpec, registry

SHEETS_API_BASE = "https://sheets.googleapis.com/v4/spreadsheets"


async def _auth_headers() -> dict[str, str]:
    token = await google_oauth.get_access_token()
    return {"Authorization": f"Bearer {token}"}


def _insufficient_scope_error() -> dict[str, Any]:
    return {
        "ok": False,
        "error": "insufficient_scope",
        "message": (
            "The Google Sheets scope is missing from your OAuth token. "
            "Please reconnect via the Google Drive settings panel to grant "
            "the spreadsheets permission."
        ),
    }


def _http_error(r: httpx.Response, action: str) -> dict[str, Any]:
    return {
        "ok": False,
        "error": "api_error",
        "status": r.status_code,
        "message": f"{action} failed ({r.status_code}): {r.text[:300]}",
    }


# ---------------------------------------------------------------------------
# sheets_get_metadata
# ---------------------------------------------------------------------------

async def _sheets_get_metadata(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    if not google_oauth.has_sheets_scope():
        return _insufficient_scope_error()

    spreadsheet_id = args["spreadsheet_id"]
    headers = await _auth_headers()
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(
            f"{SHEETS_API_BASE}/{spreadsheet_id}",
            headers=headers,
            params={"fields": "spreadsheetId,properties.title,sheets.properties"},
        )
    if r.status_code != 200:
        return _http_error(r, "sheets_get_metadata")

    data = r.json()
    sheets = []
    for s in data.get("sheets", []):
        p = s.get("properties", {})
        grid = p.get("gridProperties", {})
        sheets.append({
            "sheet_id": p.get("sheetId"),
            "title": p.get("title"),
            "index": p.get("index"),
            "row_count": grid.get("rowCount"),
            "col_count": grid.get("columnCount"),
        })
    return {
        "ok": True,
        "spreadsheet_id": data.get("spreadsheetId"),
        "title": data.get("properties", {}).get("title"),
        "sheets": sheets,
    }


registry.register(
    ToolSpec(
        name="sheets_get_metadata",
        description=(
            "List all sheets in a Google Sheets workbook with their names, "
            "GIDs, and row/column counts. Call this after sheets_get_page_context "
            "to confirm sheet names before constructing range strings.\n"
            "\n"
            "Returns: spreadsheet_id, title, sheets[{sheet_id, title, index, "
            "row_count, col_count}]."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "spreadsheet_id": {
                    "type": "string",
                    "description": "The spreadsheet ID (from sheets_get_page_context).",
                },
            },
            "required": ["spreadsheet_id"],
            "additionalProperties": False,
        },
        handler=_sheets_get_metadata,
        side="server",
        visibility_check=google_oauth.has_sheets_scope,
    )
)


# ---------------------------------------------------------------------------
# sheets_read_range
# ---------------------------------------------------------------------------

async def _sheets_read_range(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    if not google_oauth.has_sheets_scope():
        return _insufficient_scope_error()

    spreadsheet_id = args["spreadsheet_id"]
    range_ = args["range"]
    value_render = args.get("value_render", "FORMATTED_VALUE")

    headers = await _auth_headers()
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(
            f"{SHEETS_API_BASE}/{spreadsheet_id}/values/{range_}",
            headers=headers,
            params={"valueRenderOption": value_render},
        )
    if r.status_code != 200:
        return _http_error(r, "sheets_read_range")

    data = r.json()
    values = data.get("values", [])
    return {
        "ok": True,
        "range": data.get("range"),
        "values": values,
        "row_count": len(values),
        "col_count": max((len(row) for row in values), default=0),
    }


registry.register(
    ToolSpec(
        name="sheets_read_range",
        description=(
            "Read a rectangular range of cells from a Google Sheet.\n"
            "\n"
            "Parameters:\n"
            "  • spreadsheet_id — from sheets_get_page_context\n"
            "  • range — A1 notation, e.g. 'Sheet1!A1:D20' or 'A1:D20' for "
            "the first sheet\n"
            "  • value_render — 'FORMATTED_VALUE' (default, as displayed), "
            "'UNFORMATTED_VALUE' (raw numbers), or 'FORMULA'\n"
            "\n"
            "Returns: range (resolved), values (list of lists), row_count, col_count."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "spreadsheet_id": {"type": "string"},
                "range": {
                    "type": "string",
                    "description": "A1 notation range, e.g. 'Sheet1!A1:D20'.",
                },
                "value_render": {
                    "type": "string",
                    "enum": ["FORMATTED_VALUE", "UNFORMATTED_VALUE", "FORMULA"],
                    "default": "FORMATTED_VALUE",
                },
            },
            "required": ["spreadsheet_id", "range"],
            "additionalProperties": False,
        },
        handler=_sheets_read_range,
        side="server",
        visibility_check=google_oauth.has_sheets_scope,
    )
)


# ---------------------------------------------------------------------------
# sheets_write_range
# ---------------------------------------------------------------------------

async def _sheets_write_range(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    if not google_oauth.has_sheets_scope():
        return _insufficient_scope_error()

    spreadsheet_id = args["spreadsheet_id"]
    range_ = args["range"]
    values = args["values"]
    value_input_mode = args.get("value_input_mode", "USER_ENTERED")

    headers = await _auth_headers()
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.put(
            f"{SHEETS_API_BASE}/{spreadsheet_id}/values/{range_}",
            headers=headers,
            params={"valueInputOption": value_input_mode},
            json={"range": range_, "values": values},
        )
    if r.status_code != 200:
        return _http_error(r, "sheets_write_range")

    data = r.json()
    return {
        "ok": True,
        "updated_range": data.get("updatedRange"),
        "updated_rows": data.get("updatedRows"),
        "updated_columns": data.get("updatedColumns"),
        "updated_cells": data.get("updatedCells"),
    }


registry.register(
    ToolSpec(
        name="sheets_write_range",
        description=(
            "Overwrite a rectangular range of cells in a Google Sheet.\n"
            "\n"
            "IMPORTANT: always show the user what will change and confirm before "
            "calling this tool.\n"
            "\n"
            "Parameters:\n"
            "  • spreadsheet_id — from sheets_get_page_context\n"
            "  • range — A1 notation, e.g. 'Sheet1!A1:D3'\n"
            "  • values — list of lists (rows × columns); shorter rows are "
            "padded with empty strings\n"
            "  • value_input_mode — 'USER_ENTERED' (default, formulas are "
            "interpreted) | 'RAW' (stored as-is)\n"
            "\n"
            "Returns: updated_range, updated_rows, updated_columns, updated_cells."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "spreadsheet_id": {"type": "string"},
                "range": {"type": "string"},
                "values": {
                    "type": "array",
                    "items": {"type": "array"},
                    "description": "Rows to write, each row is a list of cell values.",
                },
                "value_input_mode": {
                    "type": "string",
                    "enum": ["USER_ENTERED", "RAW"],
                    "default": "USER_ENTERED",
                },
            },
            "required": ["spreadsheet_id", "range", "values"],
            "additionalProperties": False,
        },
        handler=_sheets_write_range,
        side="server",
        visibility_check=google_oauth.has_sheets_scope,
    )
)


# ---------------------------------------------------------------------------
# sheets_append_rows
# ---------------------------------------------------------------------------

async def _sheets_append_rows(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    if not google_oauth.has_sheets_scope():
        return _insufficient_scope_error()

    spreadsheet_id = args["spreadsheet_id"]
    range_ = args["range"]
    values = args["values"]
    value_input_mode = args.get("value_input_mode", "USER_ENTERED")

    headers = await _auth_headers()
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(
            f"{SHEETS_API_BASE}/{spreadsheet_id}/values/{range_}:append",
            headers=headers,
            params={
                "valueInputOption": value_input_mode,
                "insertDataOption": "INSERT_ROWS",
            },
            json={"range": range_, "values": values},
        )
    if r.status_code != 200:
        return _http_error(r, "sheets_append_rows")

    data = r.json()
    updates = data.get("updates", {})
    return {
        "ok": True,
        "updates": {
            "updated_range": updates.get("updatedRange"),
            "updated_rows": updates.get("updatedRows"),
            "updated_cells": updates.get("updatedCells"),
        },
    }


registry.register(
    ToolSpec(
        name="sheets_append_rows",
        description=(
            "Append rows after the last non-empty row in a Google Sheet table.\n"
            "\n"
            "IMPORTANT: always confirm with the user before calling this tool.\n"
            "\n"
            "Parameters:\n"
            "  • spreadsheet_id — from sheets_get_page_context\n"
            "  • range — table anchor in A1 notation, e.g. 'Sheet1!A1'; "
            "the API detects the end of the table automatically\n"
            "  • values — rows to append (list of lists)\n"
            "  • value_input_mode — 'USER_ENTERED' (default) | 'RAW'\n"
            "\n"
            "Returns: updates{updated_range, updated_rows, updated_cells}."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "spreadsheet_id": {"type": "string"},
                "range": {
                    "type": "string",
                    "description": "Table anchor, e.g. 'Sheet1!A1'.",
                },
                "values": {
                    "type": "array",
                    "items": {"type": "array"},
                },
                "value_input_mode": {
                    "type": "string",
                    "enum": ["USER_ENTERED", "RAW"],
                    "default": "USER_ENTERED",
                },
            },
            "required": ["spreadsheet_id", "range", "values"],
            "additionalProperties": False,
        },
        handler=_sheets_append_rows,
        side="server",
        visibility_check=google_oauth.has_sheets_scope,
    )
)


# ---------------------------------------------------------------------------
# Shared batchUpdate helper + A1→GridRange converter
# ---------------------------------------------------------------------------

import re as _re

_COL_RE = _re.compile(r"^([A-Za-z]+)(\d+)(?::([A-Za-z]+)(\d+))?$")


def _col_index(letters: str) -> int:
    """'A'→0, 'B'→1, 'Z'→25, 'AA'→26, …"""
    n = 0
    for ch in letters.upper():
        n = n * 26 + (ord(ch) - ord("A") + 1)
    return n - 1


def _a1_to_grid_range(a1: str, sheet_id: int) -> dict:
    """Convert 'A1:D5' (no sheet prefix) to a GridRange dict.

    Row/col indices are 0-based, end-exclusive.
    """
    m = _COL_RE.match(a1.strip())
    if not m:
        raise ValueError(f"Cannot parse A1 range: {a1!r}")
    sc, sr, ec, er = m.group(1), m.group(2), m.group(3), m.group(4)
    gr: dict = {"sheetId": sheet_id}
    gr["startRowIndex"] = int(sr) - 1
    gr["startColumnIndex"] = _col_index(sc)
    if ec and er:
        gr["endRowIndex"] = int(er)
        gr["endColumnIndex"] = _col_index(ec) + 1
    else:
        gr["endRowIndex"] = int(sr)
        gr["endColumnIndex"] = _col_index(sc) + 1
    return gr


def _rgb(hex_or_dict: str | dict) -> dict:
    """Accept '#RRGGBB' string or already-a-dict {red, green, blue}."""
    if isinstance(hex_or_dict, dict):
        return hex_or_dict
    h = hex_or_dict.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return {"red": r / 255, "green": g / 255, "blue": b / 255}


async def _batch_update(spreadsheet_id: str, requests: list[dict]) -> dict:
    headers = await _auth_headers()
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(
            f"{SHEETS_API_BASE}/{spreadsheet_id}:batchUpdate",
            headers=headers,
            json={"requests": requests},
        )
    return r


# ---------------------------------------------------------------------------
# sheets_format_range
# ---------------------------------------------------------------------------

async def _sheets_format_range(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    if not google_oauth.has_sheets_scope():
        return _insufficient_scope_error()

    spreadsheet_id = args["spreadsheet_id"]
    sheet_id = int(args["sheet_id"])
    range_a1 = args["range"]

    try:
        grid_range = _a1_to_grid_range(range_a1, sheet_id)
    except ValueError as e:
        return {"ok": False, "error": "bad_range", "message": str(e)}

    cell_format: dict[str, Any] = {}
    fields: list[str] = []

    if "background_color" in args:
        cell_format["backgroundColor"] = _rgb(args["background_color"])
        fields.append("userEnteredFormat.backgroundColor")

    text_format: dict[str, Any] = {}
    if "font_color" in args:
        text_format["foregroundColor"] = _rgb(args["font_color"])
        fields.append("userEnteredFormat.textFormat.foregroundColor")
    if "bold" in args:
        text_format["bold"] = bool(args["bold"])
        fields.append("userEnteredFormat.textFormat.bold")
    if "italic" in args:
        text_format["italic"] = bool(args["italic"])
        fields.append("userEnteredFormat.textFormat.italic")
    if "font_size" in args:
        text_format["fontSize"] = int(args["font_size"])
        fields.append("userEnteredFormat.textFormat.fontSize")
    if "font_family" in args:
        text_format["fontFamily"] = args["font_family"]
        fields.append("userEnteredFormat.textFormat.fontFamily")
    if text_format:
        cell_format["textFormat"] = text_format

    if "horizontal_alignment" in args:
        cell_format["horizontalAlignment"] = args["horizontal_alignment"].upper()
        fields.append("userEnteredFormat.horizontalAlignment")
    if "vertical_alignment" in args:
        cell_format["verticalAlignment"] = args["vertical_alignment"].upper()
        fields.append("userEnteredFormat.verticalAlignment")
    if "wrap_strategy" in args:
        cell_format["wrapStrategy"] = args["wrap_strategy"].upper()
        fields.append("userEnteredFormat.wrapStrategy")

    if "borders" in args:
        b = args["borders"]
        border_spec: dict[str, Any] = {}
        for side in ("top", "bottom", "left", "right"):
            if side in b:
                bd = b[side]
                border_spec[side] = {
                    "style": bd.get("style", "SOLID").upper(),
                    "color": _rgb(bd["color"]) if "color" in bd else {"red": 0, "green": 0, "blue": 0},
                }
        cell_format["borders"] = border_spec
        fields.append("userEnteredFormat.borders")

    if not fields:
        return {"ok": False, "error": "no_fields", "message": "No formatting properties provided."}

    request = {
        "repeatCell": {
            "range": grid_range,
            "cell": {"userEnteredFormat": cell_format},
            "fields": ",".join(fields),
        }
    }

    r = await _batch_update(spreadsheet_id, [request])
    if r.status_code != 200:
        return _http_error(r, "sheets_format_range")
    return {"ok": True, "applied_fields": fields}


registry.register(
    ToolSpec(
        name="sheets_format_range",
        description=(
            "Apply cell formatting to a range: background color, font color, "
            "bold, italic, font size, borders, alignment, wrap strategy.\n"
            "\n"
            "IMPORTANT: confirm with the user before calling.\n"
            "\n"
            "Parameters:\n"
            "  • spreadsheet_id — from sheets_get_page_context\n"
            "  • sheet_id — numeric GID from sheets_get_metadata (NOT the name)\n"
            "  • range — A1 notation WITHOUT sheet prefix, e.g. 'A1:D5'\n"
            "  • background_color — '#RRGGBB' hex string\n"
            "  • font_color — '#RRGGBB' hex string\n"
            "  • bold — true/false\n"
            "  • italic — true/false\n"
            "  • font_size — integer points\n"
            "  • font_family — e.g. 'Arial'\n"
            "  • horizontal_alignment — 'LEFT' | 'CENTER' | 'RIGHT'\n"
            "  • vertical_alignment — 'TOP' | 'MIDDLE' | 'BOTTOM'\n"
            "  • wrap_strategy — 'OVERFLOW_CELL' | 'WRAP' | 'CLIP'\n"
            "  • borders — object with optional keys top/bottom/left/right, "
            "each {style: 'SOLID'|'DASHED'|'DOTTED'|'DOUBLE'|'NONE', color: '#RRGGBB'}\n"
            "\n"
            "Returns: applied_fields list."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "spreadsheet_id": {"type": "string"},
                "sheet_id": {"type": "integer"},
                "range": {"type": "string"},
                "background_color": {"type": "string"},
                "font_color": {"type": "string"},
                "bold": {"type": "boolean"},
                "italic": {"type": "boolean"},
                "font_size": {"type": "integer"},
                "font_family": {"type": "string"},
                "horizontal_alignment": {"type": "string", "enum": ["LEFT", "CENTER", "RIGHT"]},
                "vertical_alignment": {"type": "string", "enum": ["TOP", "MIDDLE", "BOTTOM"]},
                "wrap_strategy": {"type": "string", "enum": ["OVERFLOW_CELL", "WRAP", "CLIP"]},
                "borders": {
                    "type": "object",
                    "properties": {
                        "top":    {"type": "object", "properties": {"style": {"type": "string"}, "color": {"type": "string"}}, "additionalProperties": False},
                        "bottom": {"type": "object", "properties": {"style": {"type": "string"}, "color": {"type": "string"}}, "additionalProperties": False},
                        "left":   {"type": "object", "properties": {"style": {"type": "string"}, "color": {"type": "string"}}, "additionalProperties": False},
                        "right":  {"type": "object", "properties": {"style": {"type": "string"}, "color": {"type": "string"}}, "additionalProperties": False},
                    },
                    "additionalProperties": False,
                },
            },
            "required": ["spreadsheet_id", "sheet_id", "range"],
            "additionalProperties": False,
        },
        handler=_sheets_format_range,
        side="server",
        visibility_check=google_oauth.has_sheets_scope,
    )
)


# ---------------------------------------------------------------------------
# sheets_conditional_format
# ---------------------------------------------------------------------------

async def _sheets_conditional_format(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    if not google_oauth.has_sheets_scope():
        return _insufficient_scope_error()

    spreadsheet_id = args["spreadsheet_id"]
    sheet_id = int(args["sheet_id"])
    range_a1 = args["range"]
    rule_type = args.get("rule_type", "single_color")

    try:
        grid_range = _a1_to_grid_range(range_a1, sheet_id)
    except ValueError as e:
        return {"ok": False, "error": "bad_range", "message": str(e)}

    if rule_type == "color_scale":
        min_color = _rgb(args.get("min_color", "#ffffff"))
        max_color = _rgb(args.get("max_color", "#0000ff"))
        mid_color = args.get("mid_color")
        gradient = {
            "minpoint": {"color": min_color, "type": "MIN"},
            "maxpoint": {"color": max_color, "type": "MAX"},
        }
        if mid_color:
            gradient["midpoint"] = {"color": _rgb(mid_color), "type": "PERCENTILE", "value": "50"}
        rule: dict[str, Any] = {
            "addConditionalFormatRule": {
                "rule": {
                    "ranges": [grid_range],
                    "gradientRule": gradient,
                },
                "index": args.get("index", 0),
            }
        }
    else:
        # single_color: condition-based
        condition_type = args.get("condition_type", "NOT_BLANK")
        condition_values = args.get("condition_values", [])
        bg_color = _rgb(args.get("background_color", "#ffff00"))
        font_color = args.get("font_color")
        fmt: dict[str, Any] = {"backgroundColor": bg_color}
        if font_color:
            fmt["textFormat"] = {"foregroundColor": _rgb(font_color)}
        rule = {
            "addConditionalFormatRule": {
                "rule": {
                    "ranges": [grid_range],
                    "booleanRule": {
                        "condition": {
                            "type": condition_type,
                            "values": [{"userEnteredValue": v} for v in condition_values],
                        },
                        "format": {"userEnteredFormat": fmt},
                    },
                },
                "index": args.get("index", 0),
            }
        }

    r = await _batch_update(spreadsheet_id, [rule])
    if r.status_code != 200:
        return _http_error(r, "sheets_conditional_format")
    return {"ok": True, "rule_type": rule_type}


registry.register(
    ToolSpec(
        name="sheets_conditional_format",
        description=(
            "Add a conditional formatting rule to a range.\n"
            "\n"
            "IMPORTANT: confirm with the user before calling.\n"
            "\n"
            "Two rule types:\n"
            "\n"
            "  rule_type='single_color' (default):\n"
            "    • condition_type — Sheets API BooleanConditionType, e.g. "
            "'NOT_BLANK', 'BLANK', 'NUMBER_GREATER', 'NUMBER_LESS', "
            "'NUMBER_BETWEEN', 'TEXT_CONTAINS', 'CUSTOM_FORMULA'\n"
            "    • condition_values — list of value strings (required for "
            "comparisons; for CUSTOM_FORMULA pass the formula as the single value)\n"
            "    • background_color — '#RRGGBB' fill color (default '#ffff00')\n"
            "    • font_color — '#RRGGBB' optional\n"
            "\n"
            "  rule_type='color_scale':\n"
            "    • min_color — '#RRGGBB' (default white)\n"
            "    • max_color — '#RRGGBB' (default blue)\n"
            "    • mid_color — '#RRGGBB' optional midpoint at 50th percentile\n"
            "\n"
            "Common parameters:\n"
            "  • spreadsheet_id, sheet_id, range — same as sheets_format_range\n"
            "  • index — rule priority (0 = highest, default 0)\n"
            "\n"
            "Returns: ok, rule_type."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "spreadsheet_id": {"type": "string"},
                "sheet_id": {"type": "integer"},
                "range": {"type": "string"},
                "rule_type": {"type": "string", "enum": ["single_color", "color_scale"], "default": "single_color"},
                "condition_type": {"type": "string"},
                "condition_values": {"type": "array", "items": {"type": "string"}},
                "background_color": {"type": "string"},
                "font_color": {"type": "string"},
                "min_color": {"type": "string"},
                "max_color": {"type": "string"},
                "mid_color": {"type": "string"},
                "index": {"type": "integer", "default": 0},
            },
            "required": ["spreadsheet_id", "sheet_id", "range"],
            "additionalProperties": False,
        },
        handler=_sheets_conditional_format,
        side="server",
        visibility_check=google_oauth.has_sheets_scope,
    )
)


# ---------------------------------------------------------------------------
# sheets_merge_cells
# ---------------------------------------------------------------------------

async def _sheets_merge_cells(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    if not google_oauth.has_sheets_scope():
        return _insufficient_scope_error()

    spreadsheet_id = args["spreadsheet_id"]
    sheet_id = int(args["sheet_id"])
    range_a1 = args["range"]
    merge_type = args.get("merge_type", "MERGE_ALL")
    unmerge = bool(args.get("unmerge", False))

    try:
        grid_range = _a1_to_grid_range(range_a1, sheet_id)
    except ValueError as e:
        return {"ok": False, "error": "bad_range", "message": str(e)}

    if unmerge:
        request = {"unmergeCells": {"range": grid_range}}
    else:
        request = {"mergeCells": {"range": grid_range, "mergeType": merge_type}}

    r = await _batch_update(spreadsheet_id, [request])
    if r.status_code != 200:
        return _http_error(r, "sheets_merge_cells")
    return {"ok": True, "unmerge": unmerge, "merge_type": merge_type if not unmerge else None}


registry.register(
    ToolSpec(
        name="sheets_merge_cells",
        description=(
            "Merge or unmerge a range of cells.\n"
            "\n"
            "IMPORTANT: confirm with the user before calling.\n"
            "\n"
            "Parameters:\n"
            "  • spreadsheet_id, sheet_id, range — same as sheets_format_range\n"
            "  • merge_type — 'MERGE_ALL' (default), 'MERGE_COLUMNS' (merge each "
            "column independently), 'MERGE_ROWS' (merge each row independently)\n"
            "  • unmerge — true to unmerge instead of merge (default false)\n"
            "\n"
            "Returns: ok, unmerge, merge_type."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "spreadsheet_id": {"type": "string"},
                "sheet_id": {"type": "integer"},
                "range": {"type": "string"},
                "merge_type": {
                    "type": "string",
                    "enum": ["MERGE_ALL", "MERGE_COLUMNS", "MERGE_ROWS"],
                    "default": "MERGE_ALL",
                },
                "unmerge": {"type": "boolean", "default": False},
            },
            "required": ["spreadsheet_id", "sheet_id", "range"],
            "additionalProperties": False,
        },
        handler=_sheets_merge_cells,
        side="server",
        visibility_check=google_oauth.has_sheets_scope,
    )
)


# ---------------------------------------------------------------------------
# sheets_resize_columns
# ---------------------------------------------------------------------------

async def _sheets_resize_columns(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    if not google_oauth.has_sheets_scope():
        return _insufficient_scope_error()

    spreadsheet_id = args["spreadsheet_id"]
    sheet_id = int(args["sheet_id"])
    columns = args["columns"]  # list of {index, width_px} or {start, end, width_px}

    requests = []
    for col in columns:
        width_px = int(col["width_px"])
        if "index" in col:
            start = int(col["index"])
            end = start + 1
        else:
            start = int(col["start"])
            end = int(col["end"]) + 1
        requests.append({
            "updateDimensionProperties": {
                "range": {
                    "sheetId": sheet_id,
                    "dimension": "COLUMNS",
                    "startIndex": start,
                    "endIndex": end,
                },
                "properties": {"pixelSize": width_px},
                "fields": "pixelSize",
            }
        })

    r = await _batch_update(spreadsheet_id, requests)
    if r.status_code != 200:
        return _http_error(r, "sheets_resize_columns")
    return {"ok": True, "resized": len(requests)}


registry.register(
    ToolSpec(
        name="sheets_resize_columns",
        description=(
            "Set the width of one or more columns.\n"
            "\n"
            "Parameters:\n"
            "  • spreadsheet_id, sheet_id — same as sheets_format_range\n"
            "  • columns — list of column specs, each either:\n"
            "      {index: 0, width_px: 120}       — single column by 0-based index\n"
            "      {start: 0, end: 3, width_px: 80} — columns 0–3 inclusive\n"
            "\n"
            "Column indices are 0-based (A=0, B=1, …).\n"
            "\n"
            "Returns: ok, resized (count of resize operations)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "spreadsheet_id": {"type": "string"},
                "sheet_id": {"type": "integer"},
                "columns": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "index": {"type": "integer"},
                            "start": {"type": "integer"},
                            "end": {"type": "integer"},
                            "width_px": {"type": "integer"},
                        },
                        "required": ["width_px"],
                    },
                },
            },
            "required": ["spreadsheet_id", "sheet_id", "columns"],
            "additionalProperties": False,
        },
        handler=_sheets_resize_columns,
        side="server",
        visibility_check=google_oauth.has_sheets_scope,
    )
)


# ---------------------------------------------------------------------------
# sheets_resize_rows
# ---------------------------------------------------------------------------

async def _sheets_resize_rows(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    if not google_oauth.has_sheets_scope():
        return _insufficient_scope_error()

    spreadsheet_id = args["spreadsheet_id"]
    sheet_id = int(args["sheet_id"])
    rows = args["rows"]  # list of {index, height_px} or {start, end, height_px}

    requests = []
    for row in rows:
        height_px = int(row["height_px"])
        if "index" in row:
            start = int(row["index"])
            end = start + 1
        else:
            start = int(row["start"])
            end = int(row["end"]) + 1
        requests.append({
            "updateDimensionProperties": {
                "range": {
                    "sheetId": sheet_id,
                    "dimension": "ROWS",
                    "startIndex": start,
                    "endIndex": end,
                },
                "properties": {"pixelSize": height_px},
                "fields": "pixelSize",
            }
        })

    r = await _batch_update(spreadsheet_id, requests)
    if r.status_code != 200:
        return _http_error(r, "sheets_resize_rows")
    return {"ok": True, "resized": len(requests)}


registry.register(
    ToolSpec(
        name="sheets_resize_rows",
        description=(
            "Set the height of one or more rows.\n"
            "\n"
            "Parameters:\n"
            "  • spreadsheet_id, sheet_id — same as sheets_format_range\n"
            "  • rows — list of row specs, each either:\n"
            "      {index: 0, height_px: 40}       — single row by 0-based index\n"
            "      {start: 0, end: 9, height_px: 24} — rows 0–9 inclusive\n"
            "\n"
            "Row indices are 0-based (row 1 in Sheets = index 0).\n"
            "\n"
            "Returns: ok, resized (count of resize operations)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "spreadsheet_id": {"type": "string"},
                "sheet_id": {"type": "integer"},
                "rows": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "index": {"type": "integer"},
                            "start": {"type": "integer"},
                            "end": {"type": "integer"},
                            "height_px": {"type": "integer"},
                        },
                        "required": ["height_px"],
                    },
                },
            },
            "required": ["spreadsheet_id", "sheet_id", "rows"],
            "additionalProperties": False,
        },
        handler=_sheets_resize_rows,
        side="server",
        visibility_check=google_oauth.has_sheets_scope,
    )
)
