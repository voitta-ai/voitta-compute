"""Voitta Enterprise portal tool registrations.

Tools registered here are auto-gated to ``enterprise.voitta.ai`` via
the plugin's ``manifest.json::host_patterns`` — no per-tool
``host_pattern`` needed.
"""
# get_ui_state
# Returns two independent slices of UI state in one call:
#
#   1. Enterprise SPA tree selection (folder / file / none)
#      Read directly from the rendered tree's data-* attributes.
#
#   2. Voitta report-pane tab state
#      Read from the shadow DOM's .report-tab buttons — what tabs are
#      open and which one is active.
#

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

_UI_STATE_JS = r"""
// ── 1. Enterprise SPA tree selection ──────────────────────────────────────
const sel = document.querySelector('li.tree-row.selected');
let treeSelection;
if (!sel) {
  treeSelection = { selection: "none", folder_id: null, folder_name: null,
                    rel_dir: null, file_id: null, file_name: null };
} else {
  const folderId = sel.dataset.folderId ? Number(sel.dataset.folderId) : null;
  const relDir = sel.dataset.relDir || "";
  const fileId = sel.dataset.fileId ? Number(sel.dataset.fileId) : null;

  function rowText(li) {
    return (li.querySelector('.text')?.textContent || '').trim() || null;
  }

  let fileName = null;
  let folderName = null;

  if (fileId !== null) fileName = rowText(sel);

  if (folderId !== null) {
    const rootLi = document.querySelector(
      `li.tree-row[data-folder-id="${folderId}"][data-rel-dir=""]`
    );
    if (rootLi) folderName = rowText(rootLi);
  }

  let kind;
  if (fileId !== null) kind = "file";
  else if (relDir) kind = "rel_dir";
  else kind = "folder";

  treeSelection = {
    selection: kind,
    folder_id: folderId,
    folder_name: folderName,
    rel_dir: relDir || null,
    file_id: fileId,
    file_name: fileName,
  };
}

// ── 2. Report-pane tab state ───────────────────────────────────────────────
// Tabs live inside the shadow DOM. VoittaBookmarklet.getShadowRoot() is
// injected by the bookmarklet loader. Each open tab is a button.report-tab;
// the active one carries aria-selected="true". Tab labels use .tab-label
// for report tabs or bare text for the Workspace tab; either way we strip
// the × close button text by cloning and removing .tab-close first.
let reportTabs = null;
try {
  const shadowRoot = window.VoittaBookmarklet?.getShadowRoot?.();
  if (shadowRoot) {
    const tabButtons = Array.from(shadowRoot.querySelectorAll('button.report-tab'));
    const tabs = tabButtons.map(btn => {
      const clone = btn.cloneNode(true);
      clone.querySelectorAll('.tab-close').forEach(n => n.remove());
      clone.querySelectorAll('svg').forEach(n => n.remove());
      const label = (clone.querySelector('.tab-label')?.textContent
                     || clone.textContent || '').trim();
      return {
        label,
        active: btn.getAttribute('aria-selected') === 'true',
      };
    });
    reportTabs = {
      tabs,
      active_tab: tabs.find(t => t.active)?.label ?? null,
      tab_count: tabs.length,
    };
  }
} catch (_) { /* shadow DOM not accessible — skip */ }

return {
  ...treeSelection,
  report_pane: reportTabs,
};
"""


async def _get_ui_state(
    args: dict[str, Any], ctx: ToolCtx
) -> dict[str, Any]:
    try:
        result = await call_browser("eval_js", {"js": _UI_STATE_JS}, ctx)
    except BrowserToolError as exc:
        return {"ok": False, "error": exc.kind, "message": str(exc)}
    if not result.get("ok"):
        err = result.get("error") or {}
        return {
            "ok": False,
            "error": "eval_js_failed",
            "message": err.get("message") or "scrape failed",
        }
    value = result.get("result") or {}
    if not isinstance(value, dict):
        return {
            "ok": False,
            "error": "unexpected_eval_return",
            "message": f"eval_js returned {type(value).__name__}, expected dict",
        }
    return {"ok": True, **value}


registry.register(
    ToolSpec(
        name="get_ui_state",
        description=(
            "Return the current UI state of the Voitta Enterprise portal in one call.\n"
            "\n"
            "Two slices are returned:\n"
            "\n"
            "── 1. Enterprise SPA tree selection ──────────────────────────\n"
            "Read directly from the rendered tree's data-* attributes (no\n"
            "coupling to the SPA's internal store).\n"
            "\n"
            "  • selection   — 'file' | 'rel_dir' | 'folder' | 'none'.\n"
            "  • folder_id   — integer id of the selected folder (or null).\n"
            "  • folder_name — display name of that folder root.\n"
            "  • rel_dir     — relative subdir within the folder, e.g.\n"
            "                  'drafts/q4'; null at the folder root.\n"
            "  • file_id     — integer when selection='file'; null otherwise.\n"
            "  • file_name   — basename of the selected file; null otherwise.\n"
            "\n"
            "── 2. Report-pane tab state ───────────────────────────────────\n"
            "Read from the bookmarklet shadow DOM's .report-tab buttons.\n"
            "null when the report pane is closed or the shadow root is not\n"
            "accessible.\n"
            "\n"
            "  • report_pane.tabs        — list of { label, active } for every\n"
            "                              open tab (Workspace + report tabs).\n"
            "  • report_pane.active_tab  — label of the currently focused tab,\n"
            "                              or null if no tab is active.\n"
            "  • report_pane.tab_count   — total number of open tabs.\n"
            "\n"
            "Call this at the start of any enterprise task to orient without\n"
            "asking the user to paste ids. Pairs naturally with vre_* MCP\n"
            "tools (vim_get_file, vim_search, etc.).\n"
            "\n"
            "Only visible on enterprise.voitta.ai."
        ),
        input_schema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        handler=_get_ui_state,
        side="hybrid",
        # host_pattern auto-applied from manifest.json.
    )
)
