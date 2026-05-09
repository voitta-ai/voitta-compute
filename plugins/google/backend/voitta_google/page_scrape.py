"""Drive folder DOM scrape — no-OAuth fallback for ``drive_list_files``.

Drive file rows in every layout (grid, list, side rail) carry the file's
ID on a ``data-id`` attribute. When OAuth isn't connected, we can't call
the Drive API, but we *can* read what the user already sees on screen.
This tool runs an ``eval_js`` primitive in the active Drive tab,
enumerates ``[data-id]`` elements, dedupes by ID, and returns a clean
``[{id, name}, ...]`` list.

Gated to ``drive.google.com`` host AND to the OAuth-not-connected state
— otherwise ``drive_list_files`` is the better path. The user does not
need to enable the pickup flag for this tool to appear; enumeration is
benign even without the download fallback.

Brittleness budget: the only contract this tool depends on is that
Drive renders file rows with ``data-id="<file_id>"`` and a
human-readable ``aria-label``. That has been stable across React
re-shipments for years; if it breaks we can replace it with screen
scraping via ``screenshot_report`` + OCR — but that's a future problem.
"""

from __future__ import annotations

from typing import Any

from app.services import google_oauth
from app.tools.browser import BrowserToolError, call_browser
from app.tools.registry import ToolCtx, ToolSpec, registry


# eval_js source. Runs synchronously inside the page; returns
# `[{id, name}, ...]`. Keep small — the primitive bundles this verbatim
# into an AsyncFunction body.
_SCRAPE_JS = r"""
const seen = new Map();
document.querySelectorAll('[data-id]').forEach(el => {
  const id = el.getAttribute('data-id');
  if (!id || !/^[a-zA-Z0-9_-]{20,}$/.test(id)) return;

  // Prefer aria-label (Drive's accessibility-rich labelling), then
  // tooltip, then visible text — first non-empty wins.
  let name = el.getAttribute('aria-label') || '';
  if (!name) name = el.querySelector('[data-tooltip]')?.getAttribute('data-tooltip') || '';
  if (!name) name = (el.innerText || '').trim().split('\n')[0].slice(0, 200);
  name = (name || '').trim();

  // Drive appends " Binary" to the aria-label of non-Google file types.
  // Strip it so the LLM sees the canonical filename.
  const cleaned = name.replace(/\s+Binary$/, '').trim();

  // Dedup: prefer the longest cleaned name we've seen for this id —
  // Drive's nested DOM emits the same id three or four times with
  // different labels (icon node, row node, contextmenu node, ...).
  const prev = seen.get(id);
  if (!prev || cleaned.length > prev.length) seen.set(id, cleaned);
});
return Array.from(seen, ([id, name]) => ({id, name}));
"""


def _list_visible_visible() -> bool:
    """Tool gate: visible only when OAuth is NOT connected. (When OAuth
    is connected, ``drive_list_files`` is a strictly better path —
    sortable, pageable, returns full Drive metadata.)"""
    return not google_oauth.is_connected()


async def _drive_list_visible_files(args: dict[str, Any], ctx: ToolCtx) -> Any:
    try:
        result = await call_browser("eval_js", {"js": _SCRAPE_JS}, ctx)
    except BrowserToolError as exc:
        return {
            "ok": False,
            "error": exc.kind,
            "message": str(exc),
        }
    if not result.get("ok"):
        return {
            "ok": False,
            "error": "eval_js_failed",
            "message": (result.get("error") or {}).get("message") or "scrape failed",
        }
    files = result.get("value") or []
    if not isinstance(files, list):
        return {
            "ok": False,
            "error": "unexpected_eval_return",
            "message": f"eval_js returned {type(files).__name__}, expected list",
        }
    return {"ok": True, "count": len(files), "files": files}


registry.register(
    ToolSpec(
        name="drive_list_visible_files",
        description=(
            "List the files currently visible in the active Google Drive "
            "page (folder / search results / recent / shared) by scraping "
            "the DOM. **No-OAuth fallback** for `drive_list_files`: "
            "visible only when Google OAuth is NOT connected, since "
            "`drive_list_files` is strictly better when it's available "
            "(pagination, sorting, full metadata).\n"
            "\n"
            "Returns `{ok, count, files}` where each file is "
            "`{id, name}`. The id is exactly what "
            "`drive_pickup_to_python_storage` takes as its `file_id`.\n"
            "\n"
            "Limitations:\n"
            "  • Sees only what's currently rendered. Drive lazy-loads "
            "    long lists — scroll the page first if you expect more.\n"
            "  • No mime types, sizes, modified times, or owners — "
            "    those need the API.\n"
            "  • Folders look like files (same `data-id` shape). Use the "
            "    name to disambiguate.\n"
            "\n"
            "Only visible on drive.google.com and only when OAuth is off."
        ),
        input_schema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        handler=_drive_list_visible_files,
        side="hybrid",
        # host_pattern auto-applied from manifest.json — see context.py.
        visibility_check=_list_visible_visible,
    )
)
