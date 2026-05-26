"""Backend-stored user settings (LLM API keys, provider, model, caps).

Migrated out of the browser's localStorage so settings persist across
host origins (the bookmarklet is the same widget regardless of which
site you click it on, but ``localStorage`` is partitioned per origin —
keys saved on one host would be invisible on another). The new home is
a JSON blob in the user's XDG config dir, owned by the running user
(``0600``).

Stays on the host machine. The backend already binds to ``127.0.0.1``,
so this file is no more reachable from the network than the LLM-key
state in localStorage was. Trade-off accepted: keys end up in plain
text on disk.

The blob is opaque to this module — we don't validate provider IDs,
model names, etc. Validation lives in the frontend (``lib/settings.ts``)
and is reapplied on every read there. Server-side, we only enforce
that the blob is a JSON object.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


# Sourced from ``app.config`` so the chainlit build keeps its own
# settings file (``~/.config/voitta-bookmarklet-chainlit/settings.json``)
# rather than clobbering / inheriting the legacy bookmarklet's blob.
from app.config import USER_CONFIG_DIR, USER_SETTINGS_PATH

SETTINGS_DIR = USER_CONFIG_DIR
SETTINGS_PATH = USER_SETTINGS_PATH


def mcp_debug_enabled() -> bool:
    """Whether the localhost-only MCP debugging endpoint at ``/mcp`` is
    exposed. Default **False**: the surface lets external MCP clients
    enumerate live bookmarklet sessions and eval JS in them, which is
    exactly the level of privilege we want gated behind an explicit
    user toggle. The tray-bar Settings dialog flips this; the
    ``_MCPGate`` middleware (``app.routes.mcp``) reads it on every
    request so toggling takes effect without a restart.
    """
    try:
        return bool(read().get("mcpDebugEnabled", False))
    except Exception:
        return False


def set_mcp_debug_enabled(enabled: bool) -> None:
    """Persist the MCP-debug kill switch. Used by the tray menu."""
    blob: dict[str, Any] = {}
    try:
        blob = read()
    except Exception:
        blob = {}
    blob["mcpDebugEnabled"] = bool(enabled)
    write(blob)


def read() -> dict[str, Any]:
    """Return the persisted settings dict, or ``{}`` if no file yet.

    Tolerates a missing or unreadable file (returns ``{}``); only a
    *corrupt* file (invalid JSON or non-object root) raises so the
    caller knows manual intervention is needed.
    """
    if not SETTINGS_PATH.exists():
        return {}
    raw = SETTINGS_PATH.read_text(encoding="utf-8")
    if not raw.strip():
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError(
            f"settings file at {SETTINGS_PATH} is not a JSON object"
        )
    return data


def write(blob: dict[str, Any]) -> None:
    """Persist ``blob`` to disk atomically with ``0600`` perms.

    Atomic via tmpfile + ``os.replace`` so a crash mid-write never
    leaves a half-written file. Mode is set on the tmpfile before the
    rename so the final file is born ``0600``, never momentarily
    world-readable.
    """
    if not isinstance(blob, dict):
        raise TypeError("settings must be a dict")
    SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
    # Lock down the dir too — same user-only intent.
    try:
        os.chmod(SETTINGS_DIR, 0o700)
    except OSError:
        pass

    fd, tmp_path = tempfile.mkstemp(
        prefix=".settings.", suffix=".json.tmp", dir=str(SETTINGS_DIR)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(blob, fh, ensure_ascii=False, indent=2)
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, SETTINGS_PATH)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
