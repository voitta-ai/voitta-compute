"""``list_data()`` — list all python_storage data snapshots.

Returns one entry per snapshot: handle, name, kind, source, size,
file list, created_at. Use the handle with ``ctx.file(handle)`` or
``ctx.ensure_local("py://handle/filename")`` inside a report script
to read the actual file.
"""

from __future__ import annotations

from typing import Any

from app.services.python_storage import list_all
from app.tools.registry import ToolCtx, ToolSpec, registry


def _summarise(snap: dict) -> dict:
    meta = snap.get("meta") or {}
    files = meta.get("files") or []
    origin = meta.get("origin") or {}
    return {
        "handle":      snap["handle"],
        "name":        meta.get("label") or meta.get("original_name") or snap["handle"],
        "kind":        meta.get("kind"),
        "source":      origin.get("source") or meta.get("source"),
        "bytes":       sum(f.get("bytes", 0) for f in files),
        "file_count":  len(files),
        "files":       [{"name": f.get("name"), "bytes": f.get("bytes", 0)} for f in files],
        "created_at":  meta.get("created_at"),
        "folder_name": snap.get("folder_name"),
        "corrupt":     snap.get("corrupt", False),
    }


async def _handler(_args: dict[str, Any], _ctx: ToolCtx) -> dict[str, Any]:
    snaps = list_all()
    return {
        "ok":    True,
        "count": len(snaps),
        "data":  [_summarise(s) for s in snaps],
    }


registry.register(
    ToolSpec(
        name="list_data",
        description=(
            "List all data snapshots stored in python_storage (downloaded files,\n"
            "veed frames, VRE assets, Drive files, etc.).\n\n"
            "Each entry has:\n"
            "  handle     — use with ctx.file(handle) in a report script, or\n"
            "               ctx.ensure_local('py://handle/filename')\n"
            "  name       — human-readable name (filename or label)\n"
            "  kind       — e.g. 'veed_frame', 'vre_asset', 'drive_file'\n"
            "  source     — origin system: 'video_seek', 'vre', 'drive', etc.\n"
            "  bytes      — total size\n"
            "  files[]    — individual files inside the snapshot\n"
            "  created_at — ISO timestamp\n\n"
            "Use this to discover available data before writing a report script\n"
            "that reads it."
        ),
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        side="server",
        handler=_handler,
    )
)
