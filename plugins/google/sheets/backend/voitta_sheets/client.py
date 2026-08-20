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

    ``account`` pins which Google account this client acts as — the
    script's pinned account email (captured when the script was saved),
    so a saved script runs identically regardless of what the settings
    default is on the day it re-runs. ``None`` = the settings default
    (only used by contexts that have no pin).
    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop | None = None,
        account: str | None = None,
    ) -> None:
        self._loop = loop
        self._account = account

    def _run(self, coro) -> Any:
        if self._loop is not None and self._loop.is_running():
            return asyncio.run_coroutine_threadsafe(coro, self._loop).result()
        return asyncio.run(coro)

    async def _headers(self) -> dict[str, str]:
        from app.services import google_oauth
        token = await google_oauth.get_access_token(self._account)
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
# Recording / dry-run wrapper
# ---------------------------------------------------------------------------


def _synth_response(method: str, path: str, body: dict | None) -> dict:
    """Shape-correct synthetic response for a dry-run write, so scripts
    that parse the write response survive smoke testing. Path-sniffed:
    batchUpdate / append / plain values PUT each get their real shape."""
    rng = (body or {}).get("range") or ""
    if ":batchUpdate" in path:
        return {"spreadsheetId": path.split(":", 1)[0], "replies": [], "dryRun": True}
    if ":append" in path:
        return {
            "spreadsheetId": path.split("/", 1)[0],
            "updates": {
                "updatedRange": rng, "updatedRows": 0,
                "updatedColumns": 0, "updatedCells": 0,
            },
            "dryRun": True,
        }
    return {
        "spreadsheetId": path.split("/", 1)[0],
        "updatedRange": rng, "updatedRows": 0,
        "updatedColumns": 0, "updatedCells": 0,
        "dryRun": True,
    }


class RecordingSheetsClient:
    """Wraps a real :class:`SheetsClient` (composition, NOT subclassing —
    ``get_metadata`` internally calls ``self.get``, which under composition
    stays on the inner client and records nothing, correctly).

    * Reads (``get`` / ``get_metadata``) pass straight through.
    * Writes (``put`` / ``post``) set ``effects["writes_external"] = True``
      and, when ``dry_run``, return a shape-correct synthetic response
      instead of hitting Google. Smoke tests always run dry — a script
      must never perform an external write at define/edit time.
    """

    def __init__(self, inner: SheetsClient, effects: dict, dry_run: bool) -> None:
        self._inner = inner
        self._effects = effects
        self._dry_run = dry_run

    # -- reads: pass through ------------------------------------------------

    def get(self, path: str, **params) -> dict:
        return self._inner.get(path, **params)

    def get_metadata(self, spreadsheet_id: str) -> dict:
        return self._inner.get_metadata(spreadsheet_id)

    # -- writes: record (+ stub when dry) ------------------------------------

    def put(self, path: str, body: dict | None = None, **params) -> dict:
        self._effects["writes_external"] = True
        if self._dry_run:
            return _synth_response("PUT", path, body)
        return self._inner.put(path, body, **params)

    def post(self, path: str, body: dict | None = None, **params) -> dict:
        self._effects["writes_external"] = True
        if self._dry_run:
            return _synth_response("POST", path, body)
        return self._inner.post(path, body, **params)


# ---------------------------------------------------------------------------
# Null guard
# ---------------------------------------------------------------------------

class _NullSheetsClient:
    """Raises a named, actionable error on ANY use. The message says
    exactly which account is the problem — a script pinned to a
    disconnected account must fail loudly, never silently fall through
    to whatever the default account is that day."""

    def __init__(self, reason: str | None = None) -> None:
        self._reason = reason or (
            "ctx.sheets is not available: no connected Google account has "
            "the 'spreadsheets' scope. Connect an account (or re-consent) "
            "via Settings → Google."
        )

    def __getattr__(self, name: str):
        reason = self._reason

        def _raise(*args, **kwargs):
            raise RuntimeError(reason)
        return _raise


NULL_SHEETS_CLIENT = _NullSheetsClient()
