"""Filesystem layout for the scripts subsystem.

All paths derive from ``PROJECT_ROOT`` (set in :mod:`app.config`) so a
test fixture can monkeypatch the root and get isolated trees. The three
directories below are created lazily by callers — we don't touch the
disk at import time so importing this module from tests is harmless.
"""

from __future__ import annotations

from app.services.current_user import UserPath, user_data_root

# UserPath proxies: in server mode each resolves under the current user's
# folder (USER_DATA_ROOT/users/<slug>/…) per request/turn; in desktop/dev
# (no current user) they resolve to plain USER_DATA_ROOT, unchanged. All
# existing call sites (``SCRIPTS_DIR / slug``, ``.mkdir()``, ``.iterdir()``)
# keep working untouched. See app.services.current_user.

# Each script lives at SCRIPTS_DIR / <slug> / {code.py, meta.json}.
SCRIPTS_DIR = UserPath(lambda: user_data_root() / "scripts")

# Folder container — scripts/folders/{folder_name}/{slug}/
SCRIPTS_FOLDERS_DIR = UserPath(lambda: user_data_root() / "scripts" / "folders")

# One JSONL log per slug, FIFO-capped (see render_events.py).
ERROR_LOGS_DIR = UserPath(lambda: user_data_root() / "scripts_state" / "errors")

# Latest-wins per-slug snapshot of what the FE saw on render-ready.
INVENTORY_DIR = UserPath(lambda: user_data_root() / "scripts_state" / "inventory")
