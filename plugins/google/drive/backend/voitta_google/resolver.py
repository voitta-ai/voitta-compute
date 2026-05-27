"""``drive://`` resolver — owned by the google plugin.

Reuses the plugin's already-built download path (``_drive_download`` /
``_drive_export``) — same auth refresh, same Google-native handling,
same origin block — and stamps the canonical ``ref`` onto the
resulting snapshot's ``meta.json`` so the next ensure_local hit is
free.

Key set:
  • ``file_id`` — required. The Drive file id.
  • ``export``  — optional; presence means "this is a Google-native
                  type, export to this MIME". Absent ⇒ plain download.

Registered via :func:`app.services.ensure_local.register` as an
import-time side effect at the bottom of this module. If this plugin
isn't loaded, the ``drive://`` scheme simply doesn't exist —
``ensure_local`` raises a clear "no resolver registered" error.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from app.services import ensure_local as _ensure_local, refs

_logger = logging.getLogger(__name__)


async def resolve(ref: refs.Ref) -> Path:
    file_id = ref.get("file_id")
    if not file_id:
        raise RuntimeError(f"drive ref missing file_id: {ref.canonical}")
    export_format = ref.get("export")  # e.g. "text/csv", "application/pdf"

    from voitta_google.tools import _drive_download, _drive_export
    # Build a stand-in ctx — the drive handlers only use it for
    # session_id (which ensure_local doesn't have); the real auth
    # comes from google_oauth's stored tokens. A dataclass-shaped
    # object with the same attributes is enough.
    from app.tools.registry import ToolCtx

    fake_ctx = ToolCtx(session_id="ensure_local")

    if export_format:
        # Translate the canonical "format" key into the tool handler's
        # ``format`` arg. The handler accepts either a short token
        # (``"csv"``, ``"pdf"``) or a full MIME (``"text/csv"``).
        args: dict[str, Any] = {"file_id": file_id, "format": export_format}
        result = await _drive_export(args, fake_ctx)
    else:
        args = {"file_id": file_id}
        result = await _drive_download(args, fake_ctx)
    if not result.get("ok"):
        raise RuntimeError(
            f"drive download failed for {file_id!r}: "
            f"{result.get('error')}: {result.get('message') or ''}"
        )

    snap_path = Path(result["path"])
    stored = result.get("stored_name")

    # Stamp the canonical ref into meta.json::origin so future
    # ensure_local calls hit this snapshot. The plugin's download
    # handler wrote a valid meta.json already; we just edit it in
    # place (cheap, single-file).
    meta_path = snap_path / "meta.json"
    try:
        meta = json.loads(meta_path.read_text())
    except Exception as exc:
        raise RuntimeError(f"drive resolver: meta.json unreadable: {exc}")
    origin = meta.get("origin") or {}
    if not isinstance(origin, dict):
        origin = {}
    origin["ref"] = ref.canonical
    meta["origin"] = origin
    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False))

    # Single file → return the file path. (Drive download always
    # produces one file per snapshot.)
    if stored:
        return snap_path / stored
    return snap_path


# Side-effect: register on import.
_ensure_local.register("drive", resolve)
