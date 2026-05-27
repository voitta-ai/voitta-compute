"""SheetsClient — raw Sheets API v4 HTTP access for ScriptContext.

Scripts run inside ``asyncio.to_thread`` (``build(ctx)`` is a plain
``def``). All methods are sync: they bridge to the running event loop
via ``run_coroutine_threadsafe``, the same pattern ``ctx.ensure_local`` uses.

Usage inside a script::

    def build(ctx):
        sid = ctx.args.get("spreadsheet_id")
        if not sid:
            return None

        # Read a range — returns the raw API JSON
        data = ctx.sheets.get(f"{sid}/values/Sheet1!A1:D20",
                              valueRenderOption="UNFORMATTED_VALUE")
        rows = data.get("values", [])

        # Write values
        ctx.sheets.put(f"{sid}/values/Sheet1!A1",
                       {"range": "Sheet1!A1", "values": [["Name", "Score"]]},
                       valueInputOption="USER_ENTERED")

        # Any batchUpdate request
        ctx.sheets.post(f"{sid}:batchUpdate", {"requests": [
            {"repeatCell": {
                "range": {"sheetId": 0,
                          "startRowIndex": 0, "endRowIndex": 1,
                          "startColumnIndex": 0, "endColumnIndex": 4},
                "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                "fields": "userEnteredFormat.textFormat.bold",
            }}
        ]})
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

SHEETS_BASE = "https://sheets.googleapis.com/v4/spreadsheets"


class SheetsClient:
    """Sync-callable raw HTTP client for the Google Sheets API v4.

    Every method maps directly to an HTTP verb against
    ``sheets.googleapis.com/v4/spreadsheets/{path}``.
    Auth header is injected automatically from the stored OAuth token.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        self._loop = loop

    def _run(self, coro) -> Any:
        if self._loop is not None and self._loop.is_running():
            fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
            return fut.result(timeout=60)  # never block forever
        return asyncio.run(coro)

    async def _headers(self) -> dict[str, str]:
        from app.services import google_oauth
        token = await google_oauth.get_access_token()
        return {"Authorization": f"Bearer {token}"}

    # ------------------------------------------------------------------
    # Raw HTTP methods
    # ------------------------------------------------------------------

    def get(self, path: str, **params) -> dict:
        """GET spreadsheets/{path} with optional query params.

        Example::

            data = ctx.sheets.get(f"{sid}/values/Sheet1!A1:D20",
                                  valueRenderOption="UNFORMATTED_VALUE")
            rows = data.get("values", [])
        """
        async def _go():
            h = await self._headers()
            async with httpx.AsyncClient(timeout=30.0) as c:
                r = await c.get(f"{SHEETS_BASE}/{path}", headers=h, params=params)
            r.raise_for_status()
            return r.json()
        return self._run(_go())

    def post(self, path: str, body: dict | None = None, **params) -> dict:
        """POST spreadsheets/{path} with JSON body and optional query params.

        Example::

            ctx.sheets.post(f"{sid}:batchUpdate", {"requests": [...]})
            ctx.sheets.post(f"{sid}/values/Sheet1!A1:append",
                            {"values": [["a", "b"]]},
                            valueInputOption="USER_ENTERED",
                            insertDataOption="INSERT_ROWS")
        """
        async def _go():
            h = await self._headers()
            async with httpx.AsyncClient(timeout=30.0) as c:
                r = await c.post(f"{SHEETS_BASE}/{path}", headers=h,
                                 json=body or {}, params=params)
            r.raise_for_status()
            return r.json()
        return self._run(_go())

    def put(self, path: str, body: dict | None = None, **params) -> dict:
        """PUT spreadsheets/{path} with JSON body and optional query params.

        Example::

            ctx.sheets.put(f"{sid}/values/Sheet1!A1",
                           {"range": "Sheet1!A1", "values": [[1, 2, 3]]},
                           valueInputOption="USER_ENTERED")
        """
        async def _go():
            h = await self._headers()
            async with httpx.AsyncClient(timeout=30.0) as c:
                r = await c.put(f"{SHEETS_BASE}/{path}", headers=h,
                                json=body or {}, params=params)
            r.raise_for_status()
            return r.json()
        return self._run(_go())

    # ------------------------------------------------------------------
    # Convenience: parse spreadsheet structure
    # ------------------------------------------------------------------

    def get_metadata(self, spreadsheet_id: str) -> dict:
        """Return spreadsheet title + list of sheets.

        Returns::

            {
              "spreadsheet_id": "...",
              "title": "My Sheet",
              "sheets": [
                {"sheet_id": 0, "title": "Sheet1", "index": 0,
                 "row_count": 1000, "col_count": 26},
                ...
              ]
            }

        ``sheet_id`` is the numeric GID used in GridRange and formatting
        requests. ``title`` is the sheet tab name used in A1 range notation.
        """
        data = self.get(
            spreadsheet_id,
            fields="spreadsheetId,properties.title,sheets.properties",
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


# ---------------------------------------------------------------------------
# Null guard
# ---------------------------------------------------------------------------

class _NullSheetsClient:
    _MSG = (
        "ctx.sheets is not available: Google OAuth 'spreadsheets' scope is not "
        "active. Connect Google OAuth via the Google Drive settings panel, then "
        "run the script on a docs.google.com page."
    )

    def __getattr__(self, name: str):
        def _raise(*args, **kwargs):
            raise RuntimeError(self._MSG)
        return _raise


NULL_SHEETS_CLIENT = _NullSheetsClient()
