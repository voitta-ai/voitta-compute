"""Ref-scheme resolvers.

Importing this package registers every resolver bundled with the core
backend (currently: vre, drive). Plugins can register their own via
``app.services.ensure_local.register(scheme, fn)`` at import time of
their own backend module — same pattern, no central registration list.
"""

from __future__ import annotations

from . import vre as _vre   # noqa: F401  (import side effects register)
from . import drive as _drive  # noqa: F401
