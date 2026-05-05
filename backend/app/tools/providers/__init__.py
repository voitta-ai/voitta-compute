"""Data-provider tools.

A "provider" is a third-party host the bookmarklet adds smarts for —
Google Drive today, with more on the roadmap (Dropbox, etc.). Each
provider lives in its own subpackage and registers ToolSpecs with the
global registry as a side-effect of import.

Adding a new provider
=====================

1. Create ``app/tools/providers/<name>/``.
2. Drop in a ``context.py`` (a ``<name>_get_page_context`` tool gated to
   the host's domain via ``host_pattern``) and a ``tools.py`` (the
   provider-specific fetch / search / download tools, gated the same
   way and additionally hidden via ``visibility_check`` until the user
   has connected the provider in Settings).
3. Wire it up in ``__init__.py`` of ``app.tools.providers`` so it is
   imported on backend startup.
4. If the provider needs OAuth, mirror the pattern in
   ``app/services/google_oauth.py`` (status / start / callback /
   disconnect endpoints in ``main.py``) and expose a connect button in
   the Settings panel.

The ``ToolSpec.host_pattern`` is the only gate the chat dispatcher
honours — provider tools never appear to the LLM on hosts they don't
match. ``visibility_check`` is an additional dynamic gate (e.g. "is
the user signed in?") evaluated per-turn.
"""

from app.tools.providers import (  # noqa: F401  — registration side-effects
    drive,
)
