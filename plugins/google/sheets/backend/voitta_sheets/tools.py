"""Google Sheets LLM tools — thin wrappers for direct chat use.

For scripts, use ctx.sheets.get/post/put directly — full API access,
no wrappers. These tools exist for quick in-chat operations only.

Multi-account: every tool takes an optional ``account`` selector (email
or label; the connected roster is appended to each description at
list-build time). Omitted → the settings default. Scope checks run
against the RESOLVED account — visibility is "any account has the
Sheets scope", but the chosen account must actually hold it.

Reads (metadata / read_range) that 403/404 probe the other connected
Sheets-scoped accounts in deterministic order (spreadsheet IDs are
globally unique); the serving account is stamped into the result.
WRITES NEVER PROBE — a write with the wrong account is a hard, named
error, not a fallback.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.services import google_oauth
from app.tools.registry import ToolCtx, ToolSpec, registry

logger = logging.getLogger(__name__)

SHEETS_BASE = "https://sheets.googleapis.com/v4/spreadsheets"

_ACCOUNT_ARG_SCHEMA = {
    "type": "string",
    "description": (
        "Which connected Google account to use — email or label (the "
        "tool description lists what's connected). Omit to use the "
        "default account."
    ),
}


class _SheetsError(Exception):
    def __init__(self, status: int, body: str, action: str) -> None:
        super().__init__(f"{action} failed ({status}): {body[:300]}")
        self.status = status
        self.body = body
        self.action = action


def _resolve_account_arg(args: dict[str, Any]) -> tuple[str, bool]:
    """(account_id, explicit) from the optional ``account`` selector."""
    selector = (args.get("account") or "").strip() or None
    return google_oauth.resolve_account(selector), selector is not None


def _scope_error(account_id: str) -> dict[str, Any]:
    scoped = [
        google_oauth.account_email(aid) or aid
        for aid in google_oauth.connected_account_ids(google_oauth.SHEETS_SCOPE)
    ]
    who = google_oauth.account_email(account_id) or google_oauth.account_label(account_id)
    return {
        "ok": False,
        "error": "insufficient_scope",
        "message": (
            f"Google account {who} does not have the Sheets scope. "
            + (
                f"Accounts that do: {', '.join(scoped)} — pass one as "
                f"`account`, or reconnect {who} via Settings → Google to "
                "grant the spreadsheets permission."
                if scoped
                else "No connected account has it — reconnect via "
                     "Settings → Google to grant the spreadsheets permission."
            )
        ),
    }


def _envelope_error(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, google_oauth.UnknownAccount):
        return {"ok": False, "error": "unknown_account", "message": str(exc)}
    if isinstance(exc, _SheetsError):
        return {
            "ok": False,
            "error": "api_error",
            "status": exc.status,
            "message": str(exc),
        }
    return {"ok": False, "error": type(exc).__name__, "message": str(exc)}


async def _request(
    method: str,
    path: str,
    *,
    account_id: str,
    action: str,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One Sheets REST call as ``account_id``. On 401, ONE forced token
    refresh + retry. Raises ``_SheetsError`` on non-200."""
    token = await google_oauth.get_access_token(account_id)
    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.request(
            method, f"{SHEETS_BASE}/{path}",
            headers={"Authorization": f"Bearer {token}"},
            params=params, json=json_body,
        )
        if r.status_code == 401:
            token = await google_oauth.get_access_token(account_id, force_refresh=True)
            r = await c.request(
                method, f"{SHEETS_BASE}/{path}",
                headers={"Authorization": f"Bearer {token}"},
                params=params, json=json_body,
            )
    if r.status_code != 200:
        raise _SheetsError(r.status_code, r.text, action)
    return r.json()


async def _with_read_probe(account_id: str, explicit: bool, op) -> tuple[Any, str]:
    """READS ONLY: on 403/404 with a non-explicit account, retry the
    other connected Sheets-scoped accounts in deterministic order.
    Returns ``(result, account_id_used)``; fallback hits are logged."""
    try:
        return await op(account_id), account_id
    except _SheetsError as exc:
        if explicit or exc.status not in (403, 404):
            raise
        for other in google_oauth.connected_account_ids(google_oauth.SHEETS_SCOPE):
            if other == account_id:
                continue
            try:
                result = await op(other)
            except _SheetsError:
                continue
            logger.info(
                "sheets read probe: %s returned %d; served by %s instead",
                google_oauth.account_email(account_id) or account_id,
                exc.status,
                google_oauth.account_email(other) or other,
            )
            return result, other
        raise exc


# ---------------------------------------------------------------------------
# sheets_get_metadata
# ---------------------------------------------------------------------------

async def _sheets_get_metadata(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    sid = args["spreadsheet_id"]
    try:
        account_id, explicit = _resolve_account_arg(args)
        if not google_oauth.has_sheets_scope(account_id):
            return _scope_error(account_id)
        data, used = await _with_read_probe(
            account_id,
            explicit,
            lambda aid: _request(
                "GET", sid, account_id=aid, action="sheets_get_metadata",
                params={"fields": "spreadsheetId,properties.title,sheets.properties"},
            ),
        )
    except Exception as exc:
        return _envelope_error(exc)
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
        "account": google_oauth.account_email(used),
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
            "account": _ACCOUNT_ARG_SCHEMA,
        },
        "required": ["spreadsheet_id"],
        "additionalProperties": False,
    },
    handler=_sheets_get_metadata,
    side="server",
    visibility_check=google_oauth.has_sheets_scope,
    dynamic_description=google_oauth.describe_accounts,
))


# ---------------------------------------------------------------------------
# sheets_read_range
# ---------------------------------------------------------------------------

async def _sheets_read_range(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    sid = args["spreadsheet_id"]
    range_ = args["range"]
    render = args.get("value_render", "FORMATTED_VALUE")
    try:
        account_id, explicit = _resolve_account_arg(args)
        if not google_oauth.has_sheets_scope(account_id):
            return _scope_error(account_id)
        data, used = await _with_read_probe(
            account_id,
            explicit,
            lambda aid: _request(
                "GET", f"{sid}/values/{range_}", account_id=aid,
                action="sheets_read_range",
                params={"valueRenderOption": render},
            ),
        )
    except Exception as exc:
        return _envelope_error(exc)
    values = data.get("values", [])
    return {
        "ok": True,
        "range": data.get("range"),
        "values": values,
        "row_count": len(values),
        "col_count": max((len(row) for row in values), default=0),
        "account": google_oauth.account_email(used),
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
            "account": _ACCOUNT_ARG_SCHEMA,
        },
        "required": ["spreadsheet_id", "range"],
        "additionalProperties": False,
    },
    handler=_sheets_read_range,
    side="server",
    visibility_check=google_oauth.has_sheets_scope,
    dynamic_description=google_oauth.describe_accounts,
))


# ---------------------------------------------------------------------------
# sheets_write_range
# ---------------------------------------------------------------------------

async def _sheets_write_range(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    sid = args["spreadsheet_id"]
    range_ = args["range"]
    values = args["values"]
    mode = args.get("value_input_mode", "USER_ENTERED")
    try:
        account_id, _explicit = _resolve_account_arg(args)
        if not google_oauth.has_sheets_scope(account_id):
            return _scope_error(account_id)
        # WRITE: no probe, ever. Wrong account → hard error naming it.
        data = await _request(
            "PUT", f"{sid}/values/{range_}", account_id=account_id,
            action="sheets_write_range",
            params={"valueInputOption": mode},
            json_body={"range": range_, "values": values},
        )
    except Exception as exc:
        return _envelope_error(exc)
    return {
        "ok": True,
        "updated_range": data.get("updatedRange"),
        "updated_rows": data.get("updatedRows"),
        "updated_columns": data.get("updatedColumns"),
        "updated_cells": data.get("updatedCells"),
        "account": google_oauth.account_email(account_id),
    }

registry.register(ToolSpec(
    name="sheets_write_range",
    description=(
        "Overwrite a range of cells. Confirm with user before calling.\n"
        "values — list of lists (rows × columns).\n"
        "value_input_mode — USER_ENTERED (default, formulas work) | RAW.\n"
        "Writes always use exactly the selected account (no fallback) — "
        "pass `account` explicitly when the target sheet is not in the "
        "default account."
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
            "account": _ACCOUNT_ARG_SCHEMA,
        },
        "required": ["spreadsheet_id", "range", "values"],
        "additionalProperties": False,
    },
    handler=_sheets_write_range,
    side="server",
    visibility_check=google_oauth.has_sheets_scope,
    dynamic_description=google_oauth.describe_accounts,
))


# ---------------------------------------------------------------------------
# sheets_append_rows
# ---------------------------------------------------------------------------

async def _sheets_append_rows(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    sid = args["spreadsheet_id"]
    range_ = args["range"]
    values = args["values"]
    mode = args.get("value_input_mode", "USER_ENTERED")
    try:
        account_id, _explicit = _resolve_account_arg(args)
        if not google_oauth.has_sheets_scope(account_id):
            return _scope_error(account_id)
        # WRITE: no probe, ever.
        data = await _request(
            "POST", f"{sid}/values/{range_}:append", account_id=account_id,
            action="sheets_append_rows",
            params={"valueInputOption": mode, "insertDataOption": "INSERT_ROWS"},
            json_body={"range": range_, "values": values},
        )
    except Exception as exc:
        return _envelope_error(exc)
    updates = data.get("updates", {})
    return {
        "ok": True,
        "updated_range": updates.get("updatedRange"),
        "updated_rows": updates.get("updatedRows"),
        "updated_cells": updates.get("updatedCells"),
        "account": google_oauth.account_email(account_id),
    }

registry.register(ToolSpec(
    name="sheets_append_rows",
    description=(
        "Append rows after the last non-empty row in a table. Confirm with user first.\n"
        "range — table anchor, e.g. 'Sheet1!A1'.\n"
        "values — list of lists to append.\n"
        "Writes always use exactly the selected account (no fallback) — "
        "pass `account` explicitly when the target sheet is not in the "
        "default account."
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
            "account": _ACCOUNT_ARG_SCHEMA,
        },
        "required": ["spreadsheet_id", "range", "values"],
        "additionalProperties": False,
    },
    handler=_sheets_append_rows,
    side="server",
    visibility_check=google_oauth.has_sheets_scope,
    dynamic_description=google_oauth.describe_accounts,
))
