"""Python-side storage management tools.

Snapshots live under ``python_storage/snapshot_<handle>/`` at the
project root. Each subdir contains ``meta.json`` (provenance) plus
whatever payload files the writer emitted.

The directory is gitignored and survives backend restarts. Provider
download tools (e.g. Google Drive's
``drive_download_to_python_storage``) write into this storage; the
tools registered here let the LLM list / inspect / delete / clear it.

For browser-side data the equivalent surface is
``buffers.py`` (in-memory, scoped to a tab).
"""

from __future__ import annotations

from typing import Any

from app.services import python_storage
from app.tools.registry import ToolCtx, ToolSpec, registry


async def _list_python_storage(args: dict[str, Any], ctx: ToolCtx) -> Any:
    snapshots = python_storage.list_all()

    name = (args.get("name_contains") or "").strip().lower()
    src = (args.get("source") or "").strip().lower()
    account = (args.get("account") or "").strip().lower()
    path_sub = (args.get("path_contains") or "").strip().lower()
    kind = (args.get("kind") or "").strip().lower()
    file_id = (args.get("file_id") or "").strip()
    since = args.get("since_ts")
    before = args.get("before_ts")
    limit = int(args.get("limit") or 100)
    limit = max(1, min(500, limit))

    def _origin(s: dict) -> dict:
        return (s.get("meta") or {}).get("origin") or {}

    out = []
    for s in snapshots:
        meta = s.get("meta") or {}
        stored = meta.get("stored_name") or meta.get("original_name")
        if not stored and meta.get("files"):
            stored = meta["files"][0].get("name")
        stored = stored or s.get("handle")

        ori = _origin(s)
        if name and name not in str(stored).lower():
            continue
        if kind and kind != str(meta.get("kind") or "").lower():
            continue
        if src and src not in str(ori.get("source") or "").lower():
            continue
        if account and account not in str(ori.get("account") or "").lower():
            continue
        if path_sub and path_sub not in str(ori.get("path") or "").lower():
            continue
        if file_id and file_id != str(ori.get("file_id") or ""):
            continue
        ts_str = meta.get("created_at")
        ts: float | None = None
        if isinstance(ts_str, str):
            try:
                from datetime import datetime, timezone
                ts = datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
            except Exception:
                ts = None
        if since is not None and ts is not None and ts < float(since):
            continue
        if before is not None and ts is not None and ts > float(before):
            continue

        out.append({
            "handle": s.get("handle"),
            "kind": meta.get("kind"),
            "name": stored,
            "bytes": (meta.get("files") or [{}])[0].get("bytes") if meta.get("files") else None,
            "created_at": ts_str,
            "origin": ori,
        })
        if len(out) >= limit:
            break
    return {"ok": True, "snapshots": out, "count": len(out),
            "filtered": bool(name or src or account or path_sub or kind or file_id or since or before)}


registry.register(
    ToolSpec(
        name="list_python_storage",
        description=(
            "List snapshots in Python on-disk storage with optional filters. "
            "Returns `{snapshots: [{handle, kind, name, bytes, created_at, origin}, ...], count}`.\n"
            "\n"
            "Each snapshot includes its `origin` block: "
            "`{source, account, path, file_id, host, url, extra}`. "
            "`source` is e.g. 'google_drive', 'google_drive_export'; "
            "`account` is the user identity at the source (e.g. Drive "
            "OAuth email); `path` is the human location at the source.\n"
            "\n"
            "Filters (all case-insensitive substring unless noted):\n"
            "  • `name_contains` — match against the stored filename.\n"
            "  • `source` — e.g. 'google_drive'.\n"
            "  • `account` — the originating account (e.g. Drive email).\n"
            "  • `path_contains` — match against origin.path.\n"
            "  • `kind` — 'drive_file', etc. (exact).\n"
            "  • `file_id` — exact source file ID match.\n"
            "  • `since_ts` / `before_ts` — unix-seconds bounds on created_at.\n"
            "  • `limit` — cap (default 100, max 500).\n"
            "\n"
            "Survives backend restarts. For browser-side state see "
            "list_buffers (when JS-compute is enabled)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name_contains": {"type": "string"},
                "source": {"type": "string"},
                "account": {"type": "string"},
                "path_contains": {"type": "string"},
                "kind": {"type": "string"},
                "file_id": {"type": "string"},
                "since_ts": {"type": "number"},
                "before_ts": {"type": "number"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100},
            },
            "additionalProperties": False,
        },
        handler=_list_python_storage,
        side="server",
    )
)


async def _get_python_storage_info(args: dict[str, Any], ctx: ToolCtx) -> Any:
    handle = str(args.get("handle") or "").strip()
    if not handle:
        return {"ok": False, "error": "handle required"}
    rec = python_storage.get(handle)
    if rec is None:
        return {"ok": False, "error": f"no snapshot {handle}"}
    return {"ok": True, **rec}


registry.register(
    ToolSpec(
        name="get_python_storage_info",
        description=(
            "Re-show one Python-storage snapshot's metadata (handle, "
            "path on disk, meta.json contents). Useful when you've "
            "forgotten what a handle points at."
        ),
        input_schema={
            "type": "object",
            "properties": {"handle": {"type": "string"}},
            "required": ["handle"],
            "additionalProperties": False,
        },
        handler=_get_python_storage_info,
        side="server",
    )
)


async def _delete_python_storage(args: dict[str, Any], ctx: ToolCtx) -> Any:
    handle = str(args.get("handle") or "").strip()
    if not handle:
        return {"ok": False, "error": "handle required"}
    deleted = python_storage.delete(handle)
    return {"ok": True, "deleted": deleted}


registry.register(
    ToolSpec(
        name="delete_python_storage",
        description=(
            "Delete one Python-storage snapshot from disk. Irreversible. "
            "Confirm with the user before calling on data they might "
            "want to keep."
        ),
        input_schema={
            "type": "object",
            "properties": {"handle": {"type": "string"}},
            "required": ["handle"],
            "additionalProperties": False,
        },
        handler=_delete_python_storage,
        side="server",
    )
)


async def _clear_python_storage(args: dict[str, Any], ctx: ToolCtx) -> Any:
    return python_storage.clear()


registry.register(
    ToolSpec(
        name="clear_python_storage",
        description=(
            "Delete EVERY Python-storage snapshot. Returns "
            "{freed_bytes, removed_snapshots}. Confirm with the user — "
            "this cannot be undone."
        ),
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=_clear_python_storage,
        side="server",
    )
)
