"""``preview_data(handle, filename?)`` — show a python_storage file inline.

Returns the file as an ``_image`` block (for image/video-thumb) so the LLM
can see it directly in the tool result, and also emits it to the chat.
For text files returns the content as plain text.
"""

from __future__ import annotations

import base64
import mimetypes
from pathlib import Path
from typing import Any

from app.services.python_storage import get as ps_get
from app.tools.registry import ToolCtx, ToolSpec, registry

_SKIP = {"meta.json", "raw.json", "curves.pkl"}
_IMG_EXTS  = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".bmp", ".avif"}
_TEXT_EXTS = {
    ".txt", ".md", ".csv", ".json", ".yaml", ".yml", ".log",
    ".py", ".js", ".ts", ".html", ".css", ".xml", ".sh",
    ".toml", ".ini", ".cfg", ".sql", ".r",
}


def _first_data_file(snap_dir: Path, prefer: str | None) -> Path | None:
    if prefer:
        p = snap_dir / prefer
        if p.is_file():
            return p
    for f in sorted(snap_dir.iterdir()):
        if f.name not in _SKIP and f.is_file():
            return f
    return None


async def _handler(args: dict[str, Any], _ctx: ToolCtx) -> dict[str, Any]:
    handle: str = args["handle"]
    filename: str | None = args.get("filename")

    rec = ps_get(handle)
    if rec is None:
        return {"ok": False, "error": f"no snapshot with handle {handle!r}"}

    snap_dir = Path(rec["path"])
    target = _first_data_file(snap_dir, filename)
    if target is None:
        return {"ok": False, "error": f"snapshot {handle!r} contains no data files"}

    ext = target.suffix.lower()
    mime, _ = mimetypes.guess_type(target.name)
    mime = mime or "application/octet-stream"

    if ext in _IMG_EXTS:
        data = target.read_bytes()
        b64 = base64.b64encode(data).decode()
        return {
            "ok": True,
            "handle": handle,
            "filename": target.name,
            "bytes": len(data),
            "_image": {"media_type": mime, "data": b64},
        }

    if ext in _TEXT_EXTS:
        text = target.read_text(errors="replace")
        if len(text) > 50_000:
            text = text[:50_000] + "\n[truncated]"
        return {
            "ok": True,
            "handle": handle,
            "filename": target.name,
            "bytes": target.stat().st_size,
            "text": text,
        }

    # Other types — return download hint
    return {
        "ok": True,
        "handle": handle,
        "filename": target.name,
        "bytes": target.stat().st_size,
        "mime": mime,
        "note": f"Binary file ({mime}). Use the Workspace panel to download/preview.",
    }


registry.register(
    ToolSpec(
        name="preview_data",
        description=(
            "Show a file from python_storage inline so you can see its contents.\n\n"
            "  handle   — snapshot handle (e.g. 'py_71e1c5b2') from list_data\n"
            "  filename — optional specific file inside the snapshot;\n"
            "             omit to use the first data file\n\n"
            "Images (jpg/png/webp/…) are returned as an inline _image block —\n"
            "you will see the image directly in the tool result.\n\n"
            "Text files (txt/json/csv/py/md/…) are returned as plain text.\n\n"
            "Use list_data first to discover available handles and filenames."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "handle": {
                    "type": "string",
                    "description": "python_storage handle, e.g. 'py_71e1c5b2'",
                },
                "filename": {
                    "type": "string",
                    "description": "Specific filename inside the snapshot. Omit for the first file.",
                },
            },
            "required": ["handle"],
            "additionalProperties": False,
        },
        side="server",
        handler=_handler,
    )
)
