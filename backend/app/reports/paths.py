"""Filesystem layout for the scripts subsystem.

All paths derive from ``PROJECT_ROOT`` (set in :mod:`app.config`) so a
test fixture can monkeypatch the root and get isolated trees. The three
directories below are created lazily by callers — we don't touch the
disk at import time so importing this module from tests is harmless.
"""

from __future__ import annotations

from pathlib import Path

from app.config import USER_DATA_ROOT

# Each script lives at SCRIPTS_DIR / <slug> / {code.py, meta.json}.
SCRIPTS_DIR: Path = USER_DATA_ROOT / "scripts"

# Folder container — scripts/folders/{folder_name}/{slug}/
SCRIPTS_FOLDERS_DIR: Path = SCRIPTS_DIR / "folders"

# One JSONL log per slug, FIFO-capped (see render_events.py).
ERROR_LOGS_DIR: Path = USER_DATA_ROOT / "scripts_state" / "errors"

# Latest-wins per-slug snapshot of what the FE saw on render-ready.
INVENTORY_DIR: Path = USER_DATA_ROOT / "scripts_state" / "inventory"
