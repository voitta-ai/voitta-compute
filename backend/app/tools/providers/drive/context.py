"""Drive page context — what the user is looking at on drive.google.com.

When the bookmarklet runs on a Drive page, this tool tells the LLM the
folder / view / search the user is currently navigating, so the LLM can
operate on the right scope without having to ask "which folder?". Pure
URL + title parsing — no DOM scrape, since Drive's React UI is heavily
mangled and would break the moment Google reships the bundle.

Hybrid: calls the ``get_url`` browser primitive, then parses the result
server-side. Gated to ``drive.google.com`` via ``host_pattern`` so it
isn't visible anywhere else.

Designed to be the first call the LLM makes for any Drive-flavoured
task. It produces things like ``folder_id`` and ``search_query`` that
the existing ``drive_list_files`` / ``drive_search`` /
``drive_get_file`` tools take directly.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, urlparse

from app.tools.browser import BrowserToolError, call_browser
from app.tools.registry import ToolCtx, ToolSpec, registry


# Path patterns. ``/u/<N>/`` is the multi-account index Drive injects
# when you're signed into more than one Google account; it's optional in
# every URL Drive emits, hence the ``(?:...)?`` wrapper everywhere.
_FOLDER_RE = re.compile(r"^/drive(?:/u/(\d+))?/folders/([^/?]+)/?$")
_FILE_RE = re.compile(r"^/file/d/([^/?]+)(?:/[^?]*)?/?$")
_VIEW_RE = re.compile(
    r"^/drive(?:/u/(\d+))?/("
    r"my-drive|shared-with-me|recent|starred|trash|computers|priority|search"
    r")/?$"
)

# Drive sets ``document.title`` to ``"<thing> - Google Drive"`` (and
# rarely ``"<thing> – Google Drive"`` with an en-dash). Strip either.
_TITLE_SUFFIX_RE = re.compile(r"\s*[\-–]\s*Google Drive\s*$")


def _strip_title(title: str) -> str | None:
    if not title:
        return None
    cleaned = _TITLE_SUFFIX_RE.sub("", title).strip()
    return cleaned or None


def _flatten_qs(qs: dict[str, list[str]]) -> dict[str, Any]:
    """``parse_qs`` always returns lists; collapse single-value entries
    so the LLM sees ``{"q": "foo"}`` not ``{"q": ["foo"]}``."""
    return {k: (v[0] if len(v) == 1 else v) for k, v in qs.items()}


async def _drive_page_context(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    try:
        url_info = await call_browser("get_url", {}, ctx)
    except BrowserToolError as exc:
        return {
            "ok": False,
            "error": exc.kind,
            "message": str(exc),
        }

    href = str(url_info.get("href") or "")
    title = str(url_info.get("title") or "")
    parsed = urlparse(href)
    pathname = parsed.path or "/"
    params = _flatten_qs(parse_qs(parsed.query, keep_blank_values=True))
    name_from_title = _strip_title(title)

    out: dict[str, Any] = {
        "ok": True,
        "url": href,
        "host": parsed.netloc,
        "title": title,
        "pathname": pathname,
        "params": params,
    }
    # Hint the LLM about common filter params so it doesn't have to
    # guess at the meaning of `q`/`parent`/`type` from raw `params`.
    if "q" in params:
        out["search_query"] = params["q"]
    if "parent" in params:
        out["parent_folder_id"] = params["parent"]
    if "type" in params:
        out["file_type_filter"] = params["type"]
    if "ownership" in params:
        out["ownership_filter"] = params["ownership"]

    # Specific folder.
    m = _FOLDER_RE.match(pathname)
    if m:
        out["view"] = "folder"
        if m.group(1):
            out["account_index"] = int(m.group(1))
        out["folder_id"] = m.group(2)
        if name_from_title:
            out["folder_name"] = name_from_title
        return out

    # Specific file (preview / viewer).
    m = _FILE_RE.match(pathname)
    if m:
        out["view"] = "file"
        out["file_id"] = m.group(1)
        if name_from_title:
            out["file_name"] = name_from_title
        return out

    # Top-level views (My Drive, Shared, Recent, Search, …).
    m = _VIEW_RE.match(pathname)
    if m:
        if m.group(1):
            out["account_index"] = int(m.group(1))
        out["view"] = m.group(2)
        return out

    out["view"] = "unknown"
    return out


registry.register(
    ToolSpec(
        name="drive_get_page_context",
        description=(
            "Return what folder / view / search the user is currently "
            "looking at on Google Drive, parsed from the page's URL and "
            "title. Call this at the start of any Drive task — the URL "
            "already encodes the folder id / search query, so you don't "
            "need to ask the user to paste anything.\n"
            "\n"
            "Always present: url, host, title, pathname, params, view.\n"
            "\n"
            "`view` is one of:\n"
            "  • 'folder'        → folder_id (feed to drive_list_files), "
            "folder_name (from <title>).\n"
            "  • 'file'          → file_id (feed to drive_get_file), "
            "file_name.\n"
            "  • 'my-drive'      → top-level My Drive.\n"
            "  • 'shared-with-me' / 'recent' / 'starred' / 'trash' / "
            "'computers' / 'priority' → corresponding Drive sidebar view.\n"
            "  • 'search'        → search_query (feed to drive_search).\n"
            "  • 'unknown'       → URL didn't match any known Drive route; "
            "fall back to asking the user.\n"
            "\n"
            "Conditional fields lifted from URL params for convenience:\n"
            "  • search_query    — `q=` (set on /search and any folder "
            "URL where the user has typed in the search box).\n"
            "  • parent_folder_id — `parent=` restriction.\n"
            "  • file_type_filter — `type=` (e.g. 'document', "
            "'spreadsheet', 'folders').\n"
            "  • ownership_filter — `ownership=`.\n"
            "  • account_index   — the `/u/N/` digit (which signed-in "
            "Google account is showing this view).\n"
            "\n"
            "Only visible on drive.google.com pages."
        ),
        input_schema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        handler=_drive_page_context,
        side="hybrid",
        host_pattern="drive.google.com",
    )
)
