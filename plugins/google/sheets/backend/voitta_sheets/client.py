"""SheetsClient — sync-callable Sheets API v4 wrapper for ScriptContext.

Scripts run inside ``asyncio.to_thread`` (``build(ctx)`` is a plain
``def``, not ``async def``). All methods on this client are therefore
sync: they bridge back to the running event loop via
``run_coroutine_threadsafe``, the same pattern ``ctx.ensure_local`` uses.

Usage inside a script::

    def build(ctx):
        sid = ctx.args["spreadsheet_id"]
        rows = ctx.sheets.read_range(sid, "Sheet1!A1:D20")
        ctx.sheets.format_range(sid, sheet_id=0, range="A1:D1", bold=True)

``ctx.sheets`` is ``None`` when the Google OAuth ``spreadsheets`` scope
is not active. Every method guards against this and raises a clear
``RuntimeError`` so smoke_test failures are readable.
"""

from __future__ import annotations

import asyncio
import re as _re
from typing import Any

import httpx

SHEETS_API_BASE = "https://sheets.googleapis.com/v4/spreadsheets"


# ---------------------------------------------------------------------------
# A1 → GridRange helpers (duplicated from tools.py to keep client self-contained)
# ---------------------------------------------------------------------------

_COL_RE = _re.compile(r"^([A-Za-z]+)(\d+)(?::([A-Za-z]+)(\d+))?$")


def _col_index(letters: str) -> int:
    n = 0
    for ch in letters.upper():
        n = n * 26 + (ord(ch) - ord("A") + 1)
    return n - 1


def a1_to_grid_range(a1: str, sheet_id: int) -> dict:
    """Convert 'A1:D5' (no sheet prefix) to a Sheets API GridRange dict."""
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


def rgb(hex_or_dict: str | dict) -> dict:
    """Accept '#RRGGBB' string or already-a-dict {red, green, blue}."""
    if isinstance(hex_or_dict, dict):
        return hex_or_dict
    h = hex_or_dict.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return {"red": r / 255, "green": g / 255, "blue": b / 255}


# ---------------------------------------------------------------------------
# SheetsClient
# ---------------------------------------------------------------------------

class SheetsClient:
    """Sync-callable Sheets API client, injected as ``ctx.sheets``.

    Pass ``loop`` (the running asyncio event loop from sandbox.run) so
    async API calls can be bridged back safely from the thread pool.
    If ``loop`` is None, falls back to ``asyncio.run`` (testing / REPL).
    """

    def __init__(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        self._loop = loop

    # ---- internal --------------------------------------------------------

    def _run(self, coro) -> Any:
        if self._loop is not None and self._loop.is_running():
            fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
            return fut.result()
        return asyncio.run(coro)

    async def _auth_headers(self) -> dict[str, str]:
        from app.services import google_oauth
        token = await google_oauth.get_access_token()
        return {"Authorization": f"Bearer {token}"}

    async def _get(self, url: str, params: dict | None = None) -> dict:
        headers = await self._auth_headers()
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.get(url, headers=headers, params=params or {})
        r.raise_for_status()
        return r.json()

    async def _put(self, url: str, params: dict | None = None, json: dict | None = None) -> dict:
        headers = await self._auth_headers()
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.put(url, headers=headers, params=params or {}, json=json or {})
        r.raise_for_status()
        return r.json()

    async def _post(self, url: str, params: dict | None = None, json: dict | None = None) -> dict:
        headers = await self._auth_headers()
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(url, headers=headers, params=params or {}, json=json or {})
        r.raise_for_status()
        return r.json()

    # ---- public API (all sync) -------------------------------------------

    def get_metadata(self, spreadsheet_id: str) -> dict:
        """Return spreadsheet title + list of sheets with name, sheet_id, row/col counts."""
        async def _go():
            data = await self._get(
                f"{SHEETS_API_BASE}/{spreadsheet_id}",
                params={"fields": "spreadsheetId,properties.title,sheets.properties"},
            )
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
                "spreadsheet_id": data.get("spreadsheetId"),
                "title": data.get("properties", {}).get("title"),
                "sheets": sheets,
            }
        return self._run(_go())

    def read_range(
        self,
        spreadsheet_id: str,
        range: str,
        value_render: str = "FORMATTED_VALUE",
    ) -> list[list]:
        """Read a range and return rows as a list of lists.

        range — A1 notation, e.g. 'Sheet1!A1:D20'
        value_render — 'FORMATTED_VALUE' | 'UNFORMATTED_VALUE' | 'FORMULA'
        """
        async def _go():
            data = await self._get(
                f"{SHEETS_API_BASE}/{spreadsheet_id}/values/{range}",
                params={"valueRenderOption": value_render},
            )
            return data.get("values", [])
        return self._run(_go())

    def write_range(
        self,
        spreadsheet_id: str,
        range: str,
        values: list[list],
        value_input_mode: str = "USER_ENTERED",
    ) -> dict:
        """Overwrite a range with values (list of lists).

        value_input_mode — 'USER_ENTERED' (interprets formulas) | 'RAW'
        """
        async def _go():
            return await self._put(
                f"{SHEETS_API_BASE}/{spreadsheet_id}/values/{range}",
                params={"valueInputOption": value_input_mode},
                json={"range": range, "values": values},
            )
        return self._run(_go())

    def append_rows(
        self,
        spreadsheet_id: str,
        range: str,
        values: list[list],
        value_input_mode: str = "USER_ENTERED",
    ) -> dict:
        """Append rows after the last non-empty row in the table anchored at range."""
        async def _go():
            return await self._post(
                f"{SHEETS_API_BASE}/{spreadsheet_id}/values/{range}:append",
                params={"valueInputOption": value_input_mode, "insertDataOption": "INSERT_ROWS"},
                json={"range": range, "values": values},
            )
        return self._run(_go())

    def batch_update(self, spreadsheet_id: str, requests: list[dict]) -> dict:
        """Send a raw batchUpdate request. Escape hatch for any operation not
        covered by the higher-level methods."""
        async def _go():
            return await self._post(
                f"{SHEETS_API_BASE}/{spreadsheet_id}:batchUpdate",
                json={"requests": requests},
            )
        return self._run(_go())

    def format_range(
        self,
        spreadsheet_id: str,
        sheet_id: int,
        range: str,
        *,
        background_color: str | None = None,
        font_color: str | None = None,
        bold: bool | None = None,
        italic: bool | None = None,
        font_size: int | None = None,
        font_family: str | None = None,
        horizontal_alignment: str | None = None,
        vertical_alignment: str | None = None,
        wrap_strategy: str | None = None,
        borders: dict | None = None,
    ) -> dict:
        """Apply cell formatting to a range.

        range — A1 notation WITHOUT sheet prefix, e.g. 'A1:D5'
        sheet_id — numeric GID from get_metadata()['sheets'][n]['sheet_id']
        Colors — '#RRGGBB' hex strings.
        borders — dict with optional keys top/bottom/left/right, each
                  {style: 'SOLID'|'DASHED'|'DOTTED'|'DOUBLE'|'NONE', color: '#RRGGBB'}
        """
        grid_range = a1_to_grid_range(range, sheet_id)
        cell_format: dict[str, Any] = {}
        fields: list[str] = []

        if background_color is not None:
            cell_format["backgroundColor"] = rgb(background_color)
            fields.append("userEnteredFormat.backgroundColor")

        text_format: dict[str, Any] = {}
        if font_color is not None:
            text_format["foregroundColor"] = rgb(font_color)
            fields.append("userEnteredFormat.textFormat.foregroundColor")
        if bold is not None:
            text_format["bold"] = bold
            fields.append("userEnteredFormat.textFormat.bold")
        if italic is not None:
            text_format["italic"] = italic
            fields.append("userEnteredFormat.textFormat.italic")
        if font_size is not None:
            text_format["fontSize"] = font_size
            fields.append("userEnteredFormat.textFormat.fontSize")
        if font_family is not None:
            text_format["fontFamily"] = font_family
            fields.append("userEnteredFormat.textFormat.fontFamily")
        if text_format:
            cell_format["textFormat"] = text_format

        if horizontal_alignment is not None:
            cell_format["horizontalAlignment"] = horizontal_alignment.upper()
            fields.append("userEnteredFormat.horizontalAlignment")
        if vertical_alignment is not None:
            cell_format["verticalAlignment"] = vertical_alignment.upper()
            fields.append("userEnteredFormat.verticalAlignment")
        if wrap_strategy is not None:
            cell_format["wrapStrategy"] = wrap_strategy.upper()
            fields.append("userEnteredFormat.wrapStrategy")

        if borders is not None:
            border_spec: dict[str, Any] = {}
            for side in ("top", "bottom", "left", "right"):
                if side in borders:
                    bd = borders[side]
                    border_spec[side] = {
                        "style": bd.get("style", "SOLID").upper(),
                        "color": rgb(bd["color"]) if "color" in bd else {"red": 0, "green": 0, "blue": 0},
                    }
            cell_format["borders"] = border_spec
            fields.append("userEnteredFormat.borders")

        if not fields:
            raise ValueError("format_range: no formatting properties provided")

        return self.batch_update(spreadsheet_id, [{
            "repeatCell": {
                "range": grid_range,
                "cell": {"userEnteredFormat": cell_format},
                "fields": ",".join(fields),
            }
        }])

    def conditional_format(
        self,
        spreadsheet_id: str,
        sheet_id: int,
        range: str,
        *,
        rule_type: str = "single_color",
        condition_type: str = "NOT_BLANK",
        condition_values: list[str] | None = None,
        background_color: str = "#ffff00",
        font_color: str | None = None,
        min_color: str = "#ffffff",
        max_color: str = "#0000ff",
        mid_color: str | None = None,
        index: int = 0,
    ) -> dict:
        """Add a conditional formatting rule.

        rule_type='single_color': highlight cells matching a condition.
          condition_type — BooleanConditionType, e.g. 'NOT_BLANK', 'BLANK',
            'NUMBER_GREATER', 'NUMBER_LESS', 'NUMBER_BETWEEN',
            'TEXT_CONTAINS', 'CUSTOM_FORMULA'
          condition_values — comparison values (for CUSTOM_FORMULA, the formula string)

        rule_type='color_scale': gradient from min_color to max_color.
          mid_color — optional midpoint at 50th percentile
        """
        grid_range = a1_to_grid_range(range, sheet_id)

        if rule_type == "color_scale":
            gradient: dict[str, Any] = {
                "minpoint": {"color": rgb(min_color), "type": "MIN"},
                "maxpoint": {"color": rgb(max_color), "type": "MAX"},
            }
            if mid_color:
                gradient["midpoint"] = {"color": rgb(mid_color), "type": "PERCENTILE", "value": "50"}
            rule: dict[str, Any] = {
                "addConditionalFormatRule": {
                    "rule": {"ranges": [grid_range], "gradientRule": gradient},
                    "index": index,
                }
            }
        else:
            fmt: dict[str, Any] = {"backgroundColor": rgb(background_color)}
            if font_color:
                fmt["textFormat"] = {"foregroundColor": rgb(font_color)}
            rule = {
                "addConditionalFormatRule": {
                    "rule": {
                        "ranges": [grid_range],
                        "booleanRule": {
                            "condition": {
                                "type": condition_type,
                                "values": [{"userEnteredValue": v} for v in (condition_values or [])],
                            },
                            "format": {"userEnteredFormat": fmt},
                        },
                    },
                    "index": index,
                }
            }
        return self.batch_update(spreadsheet_id, [rule])

    def merge_cells(
        self,
        spreadsheet_id: str,
        sheet_id: int,
        range: str,
        *,
        merge_type: str = "MERGE_ALL",
        unmerge: bool = False,
    ) -> dict:
        """Merge or unmerge a range.

        merge_type — 'MERGE_ALL' | 'MERGE_COLUMNS' | 'MERGE_ROWS'
        unmerge — True to unmerge instead of merge
        """
        grid_range = a1_to_grid_range(range, sheet_id)
        if unmerge:
            req = {"unmergeCells": {"range": grid_range}}
        else:
            req = {"mergeCells": {"range": grid_range, "mergeType": merge_type}}
        return self.batch_update(spreadsheet_id, [req])

    def resize_columns(
        self,
        spreadsheet_id: str,
        sheet_id: int,
        columns: list[dict],
    ) -> dict:
        """Set column widths. columns is a list of dicts:
          {index: 0, width_px: 120}         — single column (0-based)
          {start: 0, end: 3, width_px: 80}  — columns 0–3 inclusive
        """
        requests = []
        for col in columns:
            start = col["index"] if "index" in col else col["start"]
            end = (col["index"] + 1) if "index" in col else (col["end"] + 1)
            requests.append({"updateDimensionProperties": {
                "range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                          "startIndex": start, "endIndex": end},
                "properties": {"pixelSize": col["width_px"]},
                "fields": "pixelSize",
            }})
        return self.batch_update(spreadsheet_id, requests)

    def resize_rows(
        self,
        spreadsheet_id: str,
        sheet_id: int,
        rows: list[dict],
    ) -> dict:
        """Set row heights. rows is a list of dicts:
          {index: 0, height_px: 40}          — single row (0-based)
          {start: 0, end: 9, height_px: 24}  — rows 0–9 inclusive
        """
        requests = []
        for row in rows:
            start = row["index"] if "index" in row else row["start"]
            end = (row["index"] + 1) if "index" in row else (row["end"] + 1)
            requests.append({"updateDimensionProperties": {
                "range": {"sheetId": sheet_id, "dimension": "ROWS",
                          "startIndex": start, "endIndex": end},
                "properties": {"pixelSize": row["height_px"]},
                "fields": "pixelSize",
            }})
        return self.batch_update(spreadsheet_id, requests)


# ---------------------------------------------------------------------------
# Null guard — returned instead of None so missing ctx.sheets gives a clear error
# ---------------------------------------------------------------------------

class _NullSheetsClient:
    """Placeholder when OAuth is not connected or scope is missing.

    Every method raises RuntimeError with a clear message. This is what
    smoke_test and non-Sheets-page runs get, so failures are readable
    rather than ``AttributeError: 'NoneType' object has no attribute ...``
    """

    _MSG = (
        "ctx.sheets is not available: Google OAuth 'spreadsheets' scope is not "
        "active. Connect Google OAuth via the Drive settings panel, then run the "
        "script on a docs.google.com page."
    )

    def __getattr__(self, name: str):
        def _raise(*args, **kwargs):
            raise RuntimeError(self._MSG)
        return _raise


NULL_SHEETS_CLIENT = _NullSheetsClient()
