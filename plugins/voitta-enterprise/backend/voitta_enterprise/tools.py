"""Voitta Enterprise portal tool registrations.

Tools registered here are auto-gated to ``enterprise.voitta.ai`` via
the plugin's ``manifest.json::host_patterns`` — no per-tool
``host_pattern`` needed.
"""

from __future__ import annotations

from typing import Any

from app.tools.browser import BrowserToolError, call_browser
from app.tools.registry import ToolCtx, ToolSpec, registry


# ---- voitta_enterprise_get_page_context -----------------------------------
#
# Reads the enterprise SPA's selection state from the rendered DOM. The
# SPA marks every tree row with ``data-folder-id`` / ``data-rel-dir`` /
# ``data-file-id`` (see static/js/render/tree.js) and adds a ``selected``
# class on the active row (see flows/selection.js). We read those
# markers directly — no coupling to the SPA's internal store / module
# exports, so refactors there don't break us.

_PAGE_CONTEXT_JS = r"""
const sel = document.querySelector('li.tree-row.selected');
if (!sel) {
  return { selection: "none" };
}

const folderId = sel.dataset.folderId ? Number(sel.dataset.folderId) : null;
const relDir = sel.dataset.relDir || "";
const fileId = sel.dataset.fileId ? Number(sel.dataset.fileId) : null;

// File-row name: the `.text` child (see render/tree.js setupFile).
// Folder-row name: walk up to the folder root row and read its `.text`.
function rowText(li) {
  return (li.querySelector('.text')?.textContent || '').trim() || null;
}

let fileName = null;
let folderName = null;

if (fileId !== null) {
  fileName = rowText(sel);
}

// Folder display name comes from the folder ROOT row (rel-dir === ""),
// which exists regardless of whether the selection is the root, a
// subdir, or a file. Match by data-folder-id + empty rel-dir.
if (folderId !== null) {
  const rootSel = `li.tree-row[data-folder-id="${folderId}"][data-rel-dir=""]`;
  const rootLi = document.querySelector(rootSel);
  if (rootLi) folderName = rowText(rootLi);
}

let kind;
if (fileId !== null) kind = "file";
else if (relDir) kind = "rel_dir";
else kind = "folder";

return {
  selection: kind,
  folder_id: folderId,
  folder_name: folderName,
  rel_dir: relDir || null,
  file_id: fileId,
  file_name: fileName,
};
"""


async def _voitta_enterprise_get_page_context(
    args: dict[str, Any], ctx: ToolCtx
) -> dict[str, Any]:
    try:
        result = await call_browser("eval_js", {"js": _PAGE_CONTEXT_JS}, ctx)
    except BrowserToolError as exc:
        return {"ok": False, "error": exc.kind, "message": str(exc)}
    if not result.get("ok"):
        err = result.get("error") or {}
        return {
            "ok": False,
            "error": "eval_js_failed",
            "message": err.get("message") or "scrape failed",
        }
    value = result.get("value") or {}
    if not isinstance(value, dict):
        return {
            "ok": False,
            "error": "unexpected_eval_return",
            "message": f"eval_js returned {type(value).__name__}, expected dict",
        }
    return {"ok": True, **value}


registry.register(
    ToolSpec(
        name="voitta_enterprise_get_page_context",
        description=(
            "Return what the user is currently looking at in the Voitta "
            "Enterprise SPA — selected folder, subdirectory, or file. "
            "Read directly from the rendered tree's data-* attributes "
            "(no coupling to the SPA's internal store).\n"
            "\n"
            "Result shape:\n"
            "  • ok                — always True on a successful read.\n"
            "  • selection         — 'file' | 'rel_dir' | 'folder' | 'none'.\n"
            "                        'none' = no row is selected.\n"
            "  • folder_id         — integer (or null when 'none').\n"
            "  • folder_name       — display name from the folder root row.\n"
            "  • rel_dir           — '' / null at folder root, otherwise the\n"
            "                        relative subdir (e.g. 'drafts/q4').\n"
            "  • file_id           — integer when selection='file'; null otherwise.\n"
            "  • file_name         — basename of the selected file; null otherwise.\n"
            "\n"
            "Call this at the start of any enterprise-flavoured task — "
            "the LLM can then operate on the selection without asking "
            "the user to paste an id. Pairs naturally with the vre_* "
            "tools from the MCP integration (vre_get_file etc.).\n"
            "\n"
            "Only visible on enterprise.voitta.ai."
        ),
        input_schema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        handler=_voitta_enterprise_get_page_context,
        side="hybrid",
        # host_pattern auto-applied from manifest.json.
    )
)
