"""Side-effect imports: every module under here registers itself with
``app.tools.registry`` at import time. Importing this module loads all
phase-1 tools.
"""

from __future__ import annotations

from app.tools.server import now as _now  # noqa: F401
from app.tools.browser import get_page_title as _get_page_title  # noqa: F401
from app.tools.server import scripts as _scripts  # noqa: F401
from app.tools.server import rag as _rag  # noqa: F401
from app.tools.domain import theme as _theme  # noqa: F401
