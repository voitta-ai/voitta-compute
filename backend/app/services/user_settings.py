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


SETTINGS_DIR = Path.home() / ".config" / "voitta-bookmarklet"
SETTINGS_PATH = SETTINGS_DIR / "settings.json"


def js_compute_enabled() -> bool:
    """Whether the browser-side compute paradigm (JS buffers, in-browser
    plotting via Chart.js, ``buffer_eval`` JS sandbox, parser-to-buffer
    flow) is enabled.

    Default is **False** — the project has switched to a Python-only
    workflow (``download_to_python_storage`` + compute scripts). The
    JS-compute tool family is gated behind this flag so the LLM only
    sees one consistent paradigm at a time.

    The user toggles this via the Settings panel. Cheap to call (one
    file read); used as a ``ToolSpec.visibility_check`` so it runs on
    every chat turn.
    """
    try:
        return bool(read().get("jsCompute", False))
    except Exception:
        return False


def web_fetch_enabled() -> bool:
    """Whether the open-web retrieval tool (``web_fetch``) is exposed
    to the LLM.

    Default is **True** — the tool is on unless the user explicitly
    turns it off in the Settings panel. Older settings blobs that
    predate this field still get the default-on behaviour, which is
    why we test ``is False`` rather than truthiness.
    """
    try:
        return read().get("webFetch") is not False
    except Exception:
        return True


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
