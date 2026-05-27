"""Sheets page context — parse the current docs.google.com URL.

Extracts spreadsheet_id, GID, and title from the URL so the LLM never
has to ask the user to paste an ID. No OAuth required — pure URL parsing.

URL pattern:
  https://docs.google.com/spreadsheets/d/<spreadsheet_id>/edit#gid=<gid>
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from app.tools.browser import BrowserToolError, call_browser
from app.tools.registry import ToolCtx, ToolSpec, registry

_SHEETS_ID_RE = re.compile(r"/spreadsheets/d/([^/?#]+)")
_GID_RE = re.compile(r"[#&]gid=(\d+)")
_TITLE_SUFFIX_RE = re.compile(r"\s*-\s*Google Sheets\s*$")


async def _sheets_page_context(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    try:
        url_info = await call_browser("get_page_dump", {}, ctx)
    except BrowserToolError as exc:
        return {"ok": False, "error": exc.kind, "message": str(exc)}

    href = str(url_info.get("url") or "")
    title_raw = str(url_info.get("title") or "")
    title = _TITLE_SUFFIX_RE.sub("", title_raw).strip() or None

    parsed = urlparse(href)

    m_id = _SHEETS_ID_RE.search(parsed.path)
    spreadsheet_id = m_id.group(1) if m_id else None

    fragment = parsed.fragment or ""
    m_gid = _GID_RE.search(fragment)
    gid = int(m_gid.group(1)) if m_gid else None

    if not spreadsheet_id:
        return {
            "ok": False,
            "error": "not_a_sheet",
            "message": "Could not extract a spreadsheet ID from the current URL.",
            "url": href,
        }

    return {
        "ok": True,
        "spreadsheet_id": spreadsheet_id,
        "gid": gid,
        "title": title,
        "url": href,
    }


registry.register(
    ToolSpec(
        name="sheets_get_page_context",
        description=(
            "Return the spreadsheet ID and active sheet GID parsed from the "
            "current docs.google.com URL. Call this first for any Sheets task — "
            "the spreadsheet_id it returns is required by all other sheets_* tools.\n"
            "\n"
            "Returns: spreadsheet_id, gid (numeric, may be null), title, url.\n"
            "\n"
            "No OAuth required. Only visible on docs.google.com pages."
        ),
        input_schema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        handler=_sheets_page_context,
        side="hybrid",
    )
)
