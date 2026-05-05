"""pytest config — make `app.*` importable without installing the package.

Registering the project's `backend/` directory on `sys.path` keeps the
test runner self-contained: ``cd backend && python -m pytest`` works
out of a venv that hasn't run ``pip install -e .``.
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))
