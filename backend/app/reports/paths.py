"""Filesystem layout for the scripts subsystem.

All paths derive from ``PROJECT_ROOT`` (set in :mod:`app.config`) so a
test fixture can monkeypatch the root and get isolated trees. The three
directories below are created lazily by callers — we don't touch the
disk at import time so importing this module from tests is harmless.
"""

from __future__ import annotations

from app.services.current_user import UserPath
from app.services.projects import project_data_root

# UserPath proxies: each resolves per access under the ACTIVE PROJECT of
# the current user (USER_DATA_ROOT[/users/<slug>]/projects/<project>/…).
# Server mode gets per-user scoping and desktop gets the plain root via
# the same chain (see app.services.current_user + app.services.projects).
# All existing call sites (``SCRIPTS_DIR / slug``, ``.mkdir()``,
# ``.iterdir()``) keep working untouched.

# Each script lives at SCRIPTS_DIR / <slug> / {code.py, meta.json}.
SCRIPTS_DIR = UserPath(lambda: project_data_root() / "scripts")

# Folder container — scripts/folders/{folder_name}/{slug}/
SCRIPTS_FOLDERS_DIR = UserPath(lambda: project_data_root() / "scripts" / "folders")

# One JSONL log per slug, FIFO-capped (see render_events.py).
ERROR_LOGS_DIR = UserPath(lambda: project_data_root() / "scripts_state" / "errors")

# Latest-wins per-slug snapshot of what the FE saw on render-ready.
INVENTORY_DIR = UserPath(lambda: project_data_root() / "scripts_state" / "inventory")
