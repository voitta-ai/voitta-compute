"""Backend activity registry — drives the menu bar's color-coded status.

Every long-ish tool call enters a *category* via :func:`begin`, runs,
then exits via :func:`end`. The menu bar's rumps timer polls
:func:`current_glyph` every ~500 ms to render the appropriate icon:

  ● gray   idle               (no tools running)
  🟢 green  rag                (knowledge retrieval)
  🔵 blue   report             (rendering / iframes / screenshots)
  🟣 purple compute            (run_compute / buffer_eval)
  🔴 red    code_edit          (LLM mutating script source)
  🟡 yellow drive_download     (waiting on Drive UI / Downloads watcher)
  🟠 orange web                (web_fetch)
  ⚪ white  generic            (anything else with non-trivial latency)

When multiple tools run concurrently the state with the highest
priority "wins" the icon (priority ordering matches the column above
top-down). Tokens are opaque objects so callers can hold them across
async boundaries without worrying about ID collisions.

Thread-safety: the active list is guarded by a single lock; all public
functions are O(N) in the number of currently-active tools, which is
typically 0–3.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field


# Priority order (most-significant first). When several tools are
# active, the top-most match here wins the icon. "code_edit" beats
# "compute" because the model writing code is more interesting to
# surface than the same model running it; "drive_download" wins over
# "rag" because the user might be staring at the modal.
_PRIORITY = (
    "error",
    "code_edit",
    "compute",
    "report",
    "drive_download",
    "rag",
    "web",
    "generic",
)

# Status indicator: always the letter "V", coloured per category. Using
# NSAttributedString with NSForegroundColorAttributeName means the
# status-item button keeps the same single-character glyph at all
# times — no emoji-vs-text width transitions, no duplicate-slot
# render glitch we observed on macOS Sequoia when going from "V" to
# 🟢-style emoji titles. The desktop layer reads ``current_color()``
# and applies it via attributed title.
_GLYPH_TEXT = "V"

# Colour names map to ``NSColor.system<Name>Color()`` selectors —
# the desktop layer resolves them lazily so this module doesn't
# need to import AppKit (kept Cocoa-free for testability).
_COLORS = {
    "idle":           "label",       # default system text colour
    "error":          "red",
    "code_edit":      "red",
    "compute":        "purple",
    "report":         "blue",
    "drive_download": "yellow",
    "rag":            "green",
    "web":            "orange",
    "generic":        "gray",
}


@dataclass
class _Slot:
    token: object
    category: str
    detail: str = ""


_lock = threading.Lock()
_active: list[_Slot] = field(default_factory=list)  # type: ignore[assignment]
# `field` only works inside dataclass; just re-bind plain list.
_active = []


# ----- public API -----------------------------------------------------------


def begin(category: str, detail: str = "") -> object:
    """Mark a tool as running. Returns an opaque token to feed
    :func:`end` when the tool finishes (success or error).

    Unknown ``category`` values are accepted — they map to the
    ``"generic"`` glyph but are still recorded with their original
    label for diagnostics.
    """
    token = object()
    with _lock:
        _active.append(_Slot(token=token, category=category, detail=detail))
    return token


def end(token: object) -> None:
    """Mark a tool as finished. Idempotent — calling with an already-
    removed token is a no-op."""
    with _lock:
        _active[:] = [s for s in _active if s.token is not token]


def current_category() -> str:
    """Return the category whose glyph the menu bar should display
    *right now*. Returns ``"idle"`` when nothing is running."""
    with _lock:
        if not _active:
            return "idle"
        active_cats = {s.category for s in _active}
    for cat in _PRIORITY:
        if cat in active_cats:
            return cat
    # Unknown / unmapped category — surface as generic so the user
    # sees *something* moving.
    return "generic"


def current_glyph() -> str:
    """The text the menu bar should render. Always the single-letter
    brand glyph; colour is conveyed via :func:`current_color`."""
    return _GLYPH_TEXT


def current_color() -> str:
    """Logical colour name (``red``, ``green``, …, or ``label`` for the
    default system text colour). Mapped to ``NSColor.system<Name>Color``
    by the desktop layer."""
    return _COLORS.get(current_category(), _COLORS["generic"])


def snapshot() -> list[dict[str, str]]:
    """Diagnostic — list of currently-running slots. Used by the
    Settings dialog and any future health endpoint."""
    with _lock:
        return [
            {"category": s.category, "detail": s.detail}
            for s in _active
        ]


# ----- tool-name → category classification ---------------------------------


def classify(tool_name: str) -> str:
    """Map a tool name to a category. Used by the registry dispatch
    wrapper so every tool call is automatically tracked.

    Pattern matching is intentional: future tools that share a prefix
    (e.g. ``rag_*``, ``drive_*``) inherit the right category without
    edits here.
    """
    n = tool_name.lower()
    # ``define_*`` / ``edit_*`` precede the ``"report" in n`` test
    # because ``define_report`` and ``edit_report_script`` are
    # *writing* code, not running an existing report.
    if n.startswith("define_") or n.startswith("edit_") and (
        "compute" in n or "report" in n or "script" in n
    ) or n in ("edit_script",):
        return "code_edit"
    if n.startswith("rag_"):
        return "rag"
    if n in ("show_holoviz_report", "screenshot_report") or "report" in n:
        return "report"
    if n in ("run_compute", "buffer_eval"):
        return "compute"
    if "pickup" in n or "drive_download" in n or "drive_export" in n:
        return "drive_download"
    if n == "web_fetch":
        return "web"
    return "generic"
