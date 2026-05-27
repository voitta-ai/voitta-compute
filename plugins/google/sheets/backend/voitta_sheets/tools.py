"""Google Sheets LLM tools — thin wrappers for direct chat use.

For scripts, use ctx.sheets.get/post/put directly — full API access,
no wrappers. These tools exist for quick in-chat operations only.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.services import google_oauth
from app.tools.registry import ToolCtx, ToolSpec, registry

SHEETS_BASE = "https://sheets.googleapis.com/v4/spreadsheets"


async def _auth_headers() -> dict[str, str]:
    token = await google_oauth.get_access_token()
    return {"Authorization": f"Bearer {token}"}


def _scope_error() -> dict[str, Any]:
    return {
        "ok": False,
        "error": "insufficient_scope",
        "message": (
            "Google Sheets scope missing. Reconnect via the Google Drive "
            "settings panel to grant the spreadsheets permission."
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
        return _scope_error()
    sid = args["spreadsheet_id"]
    headers = await _auth_headers()
    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.get(f"{SHEETS_BASE}/{sid}", headers=headers,
                        params={"fields": "spreadsheetId,properties.title,sheets.properties"})
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

registry.register(ToolSpec(
    name="sheets_get_metadata",
    description=(
        "List all sheets in a Google Sheets workbook: names, numeric sheet_ids "
        "(GIDs), row/column counts. Call after sheets_get_page_context."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "spreadsheet_id": {"type": "string"},
        },
        "required": ["spreadsheet_id"],
        "additionalProperties": False,
    },
    handler=_sheets_get_metadata,
    side="server",
    visibility_check=google_oauth.has_sheets_scope,
))


# ---------------------------------------------------------------------------
# sheets_read_range
# ---------------------------------------------------------------------------

async def _sheets_read_range(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    if not google_oauth.has_sheets_scope():
        return _scope_error()
    sid = args["spreadsheet_id"]
    range_ = args["range"]
    render = args.get("value_render", "FORMATTED_VALUE")
    headers = await _auth_headers()
    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.get(f"{SHEETS_BASE}/{sid}/values/{range_}", headers=headers,
                        params={"valueRenderOption": render})
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

registry.register(ToolSpec(
    name="sheets_read_range",
    description=(
        "Read cells from a Google Sheet. Returns values as a list of lists.\n"
        "range — A1 notation, e.g. 'Sheet1!A1:D20'.\n"
        "value_render — FORMATTED_VALUE (default) | UNFORMATTED_VALUE | FORMULA."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "spreadsheet_id": {"type": "string"},
            "range": {"type": "string"},
            "value_render": {
                "type": "string",
                "enum": ["FORMATTED_VALUE", "UNFORMATTED_VALUE", "FORMULA"],
            },
        },
        "required": ["spreadsheet_id", "range"],
        "additionalProperties": False,
    },
    handler=_sheets_read_range,
    side="server",
    visibility_check=google_oauth.has_sheets_scope,
))


# ---------------------------------------------------------------------------
# sheets_write_range
# ---------------------------------------------------------------------------

async def _sheets_write_range(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    if not google_oauth.has_sheets_scope():
        return _scope_error()
    sid = args["spreadsheet_id"]
    range_ = args["range"]
    values = args["values"]
    mode = args.get("value_input_mode", "USER_ENTERED")
    headers = await _auth_headers()
    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.put(f"{SHEETS_BASE}/{sid}/values/{range_}", headers=headers,
                        params={"valueInputOption": mode},
                        json={"range": range_, "values": values})
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

registry.register(ToolSpec(
    name="sheets_write_range",
    description=(
        "Overwrite a range of cells. Confirm with user before calling.\n"
        "values — list of lists (rows × columns).\n"
        "value_input_mode — USER_ENTERED (default, formulas work) | RAW."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "spreadsheet_id": {"type": "string"},
            "range": {"type": "string"},
            "values": {"type": "array", "items": {"type": "array"}},
            "value_input_mode": {
                "type": "string",
                "enum": ["USER_ENTERED", "RAW"],
            },
        },
        "required": ["spreadsheet_id", "range", "values"],
        "additionalProperties": False,
    },
    handler=_sheets_write_range,
    side="server",
    visibility_check=google_oauth.has_sheets_scope,
))


# ---------------------------------------------------------------------------
# sheets_append_rows
# ---------------------------------------------------------------------------

async def _sheets_append_rows(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    if not google_oauth.has_sheets_scope():
        return _scope_error()
    sid = args["spreadsheet_id"]
    range_ = args["range"]
    values = args["values"]
    mode = args.get("value_input_mode", "USER_ENTERED")
    headers = await _auth_headers()
    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.post(f"{SHEETS_BASE}/{sid}/values/{range_}:append", headers=headers,
                         params={"valueInputOption": mode, "insertDataOption": "INSERT_ROWS"},
                         json={"range": range_, "values": values})
    if r.status_code != 200:
        return _http_error(r, "sheets_append_rows")
    data = r.json()
    updates = data.get("updates", {})
    return {
        "ok": True,
        "updated_range": updates.get("updatedRange"),
        "updated_rows": updates.get("updatedRows"),
        "updated_cells": updates.get("updatedCells"),
    }

registry.register(ToolSpec(
    name="sheets_append_rows",
    description=(
        "Append rows after the last non-empty row in a table. Confirm with user first.\n"
        "range — table anchor, e.g. 'Sheet1!A1'.\n"
        "values — list of lists to append."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "spreadsheet_id": {"type": "string"},
            "range": {"type": "string"},
            "values": {"type": "array", "items": {"type": "array"}},
            "value_input_mode": {
                "type": "string",
                "enum": ["USER_ENTERED", "RAW"],
            },
        },
        "required": ["spreadsheet_id", "range", "values"],
        "additionalProperties": False,
    },
    handler=_sheets_append_rows,
    side="server",
    visibility_check=google_oauth.has_sheets_scope,
))
