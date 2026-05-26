"""Unified script/report subsystem.

One abstraction: a *script* at ``scripts/<slug>/code.py``. The script
defines ``build(ctx)`` whose return value (or ``ctx.*`` side-effects)
determine what happens: a renderable goes to the report pane; inline
emissions land in the chat.

This package is internally layered:

* :mod:`.paths` / :mod:`.slug` — filesystem layout + name validation.
* :mod:`.store` — read/write/delete with atomic semantics.
* :mod:`.ctx` — the context object scripts receive in ``build(ctx)``.
* :mod:`.sandbox` — controlled ``exec`` and smoke-test harness.
* :mod:`.render_events` — in-memory + FIFO disk drain for FE → BE
  render-event posts.
* :mod:`.detect` / :mod:`.dispatch` / :mod:`.renderers` — added in R2+.
"""

from app.reports.paths import SCRIPTS_DIR, ERROR_LOGS_DIR, INVENTORY_DIR
from app.reports.slug import SLUG_RE, validate_slug

__all__ = [
    "SCRIPTS_DIR",
    "ERROR_LOGS_DIR",
    "INVENTORY_DIR",
    "SLUG_RE",
    "validate_slug",
]
