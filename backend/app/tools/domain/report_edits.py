"""Read the user's live edits to the currently-open editable report.

100% browser-side: the report iframe (Muuri grid + Bokeh document + our
shim's selection state) is the source of truth. This tool wraps the
``get_report_edits`` browser primitive into a ToolSpec so the LLM can
poll it whenever it needs to know what the user has changed.

Three return shapes (passed through verbatim from the primitive):

* ``{status: "no_active_report", message}`` — no report mounted, or the
  open report isn't in edit mode (URL lacks ``editable=true``).
* ``{status: "active_no_edits", report_id, message}`` — report is in
  edit mode but every card is at default width (100 %) / height
  (Bokeh-natural) / visible, original order, and nothing is selected.
* ``{status: "active", report_id, elements, selected_id, order_changed}``
  — at least one card moved, resized, hidden, or selected. Each entry
  in ``elements`` carries ``name`` (the Python ``name=`` argument set
  in the script — ``null`` if unset) plus index/title/width_pct/
  height_px/visible/selected.

The LLM is expected to use ``name`` as the stable identifier across
re-renders. If it's ``null``, the script didn't set a name on that
component; reach for ``title`` or ``id`` as fallbacks, and consider
suggesting a ``name=`` next time the user asks for a script change.
"""

from __future__ import annotations

from typing import Any

from app.tools.browser import BrowserToolError, call_browser
from app.tools.registry import ToolCtx, ToolSpec, registry


async def _get_report_edits(args: dict[str, Any], ctx: ToolCtx) -> Any:
    try:
        return await call_browser(
            "get_report_edits",
            {},
            ctx,
            timeout_ms=10_000,
        )
    except BrowserToolError as exc:
        return {
            "status": "no_active_report",
            "error": exc.kind,
            "message": str(exc),
        }


registry.register(
    ToolSpec(
        name="get_report_edits",
        description=(
            "Read the user's live edits to the report currently open in "
            "the iframe pane. Use this to understand what the user has "
            "changed about the rendered layout (drag-reorder, resize, "
            "hide via the delete handle, visual selection) when you are "
            "discussing or modifying the report.\n"
            "\n"
            "Returns one of three shapes:\n"
            "  • {status: 'no_active_report', message} — no report is "
            "open, or the open report is not in edit mode.\n"
            "  • {status: 'active_no_edits', report_id, message} — "
            "report is in edit mode but the user has not moved, "
            "resized, hidden, or selected anything yet.\n"
            "  • {status: 'active', report_id, elements[], selected_id, "
            "order_changed} — at least one user edit has happened. "
            "Each `elements` entry: {index, id, name, title, "
            "width_pct, height_px, visible, selected}. `name` is the "
            "Python `name=` argument the script set on that component "
            "(prefer this for referring to elements); `title` is the "
            "best-effort title text (Bokeh figure title); `id` is the "
            "Bokeh model id (always present, but cryptic).\n"
            "\n"
            "When the script doesn't set `name=` on its main "
            "components, identifiers come back as null and you must "
            "fall back to `title` or `id`. If the user wants their "
            "edits to be referable by name, encourage adding `name=` "
            "to each main component the next time you edit the script.\n"
            "\n"
            "This tool does NOT modify the script. The user's edits "
            "live only in the iframe; closing the report or reloading "
            "loses them. If the user wants edits persisted, that's a "
            "separate script edit you make explicitly."
        ),
        input_schema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        handler=_get_report_edits,
        side="hybrid",
    )
)
