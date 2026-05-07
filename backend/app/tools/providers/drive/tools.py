"""Google Drive tools — read-only, OAuth-backed (plus one no-OAuth
fallback).

Six LLM-facing tools. The first five are server-side and gated behind
``visibility_check=google_oauth.is_connected`` so they only appear in
the LLM's tool list once the user has signed in via the Settings panel.
The sixth (``drive_pickup_to_python_storage``) is a hybrid fallback
that drives the user's logged-in browser to download a file and watches
the Downloads folder — visible only when OAuth is **not** connected
and the user has explicitly opted in via the
``driveDownloadViaPickup`` flag in Settings.

Verb_noun naming, JSON envelopes with ``ok`` + uniform error fields,
pagination via opaque ``cursor``, snapshot handles for content.

  • drive_list_files(folder_id?, recursive?, page_size?, cursor?, order_by?)
      List files in a folder. Default folder = ``root`` (My Drive). Set
      ``recursive=true`` to walk subfolders (capped at ~5 000 entries).
  • drive_search(query, page_size?, cursor?, order_by?)
      Drive full-text query syntax. Returns the same shape as
      drive_list_files. Best-of-class for "find me X".
  • drive_get_file(file_id, fields?)
      Single-file metadata, full set of fields.
  • drive_download_to_python_storage(file_id, name?, force_refresh?)
      Download binary content (PDF, image, .py, .csv, etc.) into
      python_storage. Reuses an existing snapshot if one's already
      cached for this file_id (24 h TTL); ``force_refresh=true``
      bypasses. NOT for native Google formats — use export_…
  • drive_export_to_python_storage(file_id, format?)
      Export a Google Doc / Sheet / Slide / Drawing in a non-native
      format (txt / pdf / docx / html / md / csv / tsv / xlsx / png).
      ``format`` defaults pick the most useful representation per
      mimeType.
  • drive_pickup_to_python_storage(file_id, name?, timeout_s?)
      No-OAuth fallback. Opens the Drive download URL in a new tab
      (uses the user's existing Google session) and moves the
      resulting file out of ``~/Downloads`` (configurable) into
      python_storage. Visible only when OAuth is not connected AND
      the user enabled the pickup flag in Settings.

By design, no tool returns file CONTENT to the LLM context. The LLM
gets metadata + a python_storage handle; actual content reads happen
in compute / report scripts via ``ctx.snapshot(handle)``. Keeps the
LLM's context clean and forces the analysis to live in code.

Auth: every call awaits ``google_oauth.get_access_token()`` which
auto-refreshes if the access token is within 60 s of expiry. On 401,
the error is surfaced with a hint to re-connect.

Read-only by design: ``drive.readonly`` scope only. There are no
upload/share/move tools here, on purpose.
"""

from __future__ import annotations

import asyncio as _asyncio
import time
from pathlib import Path
from typing import Any

import httpx

from app.services import drive_pickup, google_oauth, python_storage, user_settings
from app.tools.browser import BrowserToolError, call_browser
from app.tools.registry import ToolCtx, ToolSpec, registry


DRIVE_API_BASE = "https://www.googleapis.com/drive/v3"

# What we ask Google to return on listings/gets. Keep IDs/names/MIME
# always; the rest is enough to enable common LLM filtering ("which
# PDFs are mine, modified after Jan?") without a follow-up call.
DEFAULT_FILE_FIELDS = (
    "id,name,mimeType,size,modifiedTime,createdTime,parents,"
    "trashed,owners(emailAddress,displayName),shared,sharingUser(emailAddress),"
    "webViewLink,iconLink"
)
DEFAULT_LIST_FIELDS = f"nextPageToken,files({DEFAULT_FILE_FIELDS})"

# How long an existing python_storage snapshot for a given Drive
# file_id is considered fresh enough to reuse instead of re-downloading.
_REUSE_TTL_S = 24 * 60 * 60

# In-flight tracker so two concurrent calls for the same file_id share
# the same download future.
_inflight: dict[str, "_asyncio.Future[Any]"] = {}

# Map Google-format mimeTypes → (default export mime, default extension).
# Used by drive_export_to_python_storage when ``format`` is omitted.
_GOOGLE_FORMAT_DEFAULTS = {
    "application/vnd.google-apps.document":
        ("text/plain", "txt"),
    "application/vnd.google-apps.spreadsheet":
        ("text/csv", "csv"),
    "application/vnd.google-apps.presentation":
        ("application/pdf", "pdf"),
    "application/vnd.google-apps.drawing":
        ("image/png", "png"),
    "application/vnd.google-apps.script":
        ("application/vnd.google-apps.script+json", "json"),
}

# Format token (LLM-facing) → (export mime, extension).
_FORMAT_OPTIONS = {
    "txt":  ("text/plain", "txt"),
    "md":   ("text/markdown", "md"),
    "html": ("text/html", "html"),
    "pdf":  ("application/pdf", "pdf"),
    "docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", "docx"),
    "csv":  ("text/csv", "csv"),
    "tsv":  ("text/tab-separated-values", "tsv"),
    "xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "xlsx"),
    "png":  ("image/png", "png"),
    "jpeg": ("image/jpeg", "jpg"),
    "svg":  ("image/svg+xml", "svg"),
    "json": ("application/vnd.google-apps.script+json", "json"),
}


# ---- low-level HTTP helpers ----------------------------------------------


async def _drive_get(
    path: str,
    *,
    params: dict[str, Any] | None = None,
    stream: bool = False,
) -> Any:
    """GET against Drive REST. Returns ``httpx.Response`` if ``stream``,
    parsed JSON otherwise. On 401 retries ONCE after forced refresh."""
    token = await google_oauth.get_access_token()
    url = f"{DRIVE_API_BASE}{path}" if path.startswith("/") else f"{DRIVE_API_BASE}/{path}"
    headers = {"Authorization": f"Bearer {token}"}

    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.get(url, params=params, headers=headers)
        if r.status_code == 401:
            # Force refresh + one retry. ``get_access_token`` won't
            # refresh again because we may still be inside the grace
            # window — clear tokens.expires_at to force it.
            try:
                # Simplest: set expires_at to 0 in the stored blob.
                from app.services import user_settings as _us
                blob = _us.read()
                tok = (blob.get("googleOAuth") or {}).get("tokens") or {}
                tok["expires_at"] = 0
                blob["googleOAuth"]["tokens"] = tok
                _us.write(blob)
            except Exception:
                pass
            token = await google_oauth.get_access_token()
            headers = {"Authorization": f"Bearer {token}"}
            r = await client.get(url, params=params, headers=headers)
        if stream:
            # Caller handles non-200 themselves.
            return r
        if r.status_code != 200:
            raise _DriveError(r.status_code, r.text)
        return r.json()


class _DriveError(Exception):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"Drive API {status}: {body[:300]}")
        self.status = status
        self.body = body


def _envelope_drive_error(exc: Exception) -> dict[str, Any]:
    """Map a thrown error into a uniform tool-result envelope."""
    if isinstance(exc, _DriveError):
        if exc.status in (401, 403):
            return {
                "ok": False,
                "error": "drive_auth_failed",
                "status": exc.status,
                "message": str(exc),
                "hint": (
                    "Drive returned auth error. The user may need to "
                    "re-connect Google Drive in the Settings panel."
                ),
            }
        return {
            "ok": False,
            "error": "drive_api_error",
            "status": exc.status,
            "message": str(exc),
        }
    return {"ok": False, "error": type(exc).__name__, "message": str(exc)}


# ---- drive_list_files ----------------------------------------------------


async def _drive_list_files(args: dict[str, Any], ctx: ToolCtx) -> Any:
    folder_id = (args.get("folder_id") or "").strip() or "root"
    recursive = bool(args.get("recursive", False))
    page_size = int(args.get("page_size") or 100)
    page_size = max(1, min(1000, page_size))
    cursor = args.get("cursor") or None
    order_by = (args.get("order_by") or "modifiedTime desc").strip()

    if recursive:
        # Recursive walk — uses search-scoped queries. Cap to keep
        # results bounded and fast.
        return await _recursive_walk(folder_id, page_size=page_size, order_by=order_by)

    # One-folder listing.
    q = f"'{folder_id}' in parents and trashed = false"
    return await _list_with_q(q, page_size=page_size, cursor=cursor, order_by=order_by)


async def _list_with_q(
    q: str,
    *,
    page_size: int,
    cursor: str | None,
    order_by: str,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "q": q,
        "pageSize": page_size,
        "fields": DEFAULT_LIST_FIELDS,
        "orderBy": order_by,
        "spaces": "drive",
        # Read items from My Drive, Shared with me, Shared drives, etc.
        "supportsAllDrives": "true",
        "includeItemsFromAllDrives": "true",
        "corpora": "user",
    }
    if cursor:
        params["pageToken"] = cursor
    try:
        body = await _drive_get("/files", params=params)
    except Exception as exc:
        return _envelope_drive_error(exc)
    files = body.get("files") or []
    return {
        "ok": True,
        "files": [_trim_file(f) for f in files],
        "count": len(files),
        "next_cursor": body.get("nextPageToken"),
        "q": q,
    }


async def _recursive_walk(
    root_id: str,
    *,
    page_size: int,
    order_by: str,
    max_total: int = 5000,
) -> dict[str, Any]:
    """Breadth-first descent. Returns flat ``files`` list with each
    record carrying its parents. Capped at ``max_total`` to bound
    cost + token spend."""
    seen_folders: set[str] = set()
    queue: list[str] = [root_id]
    out: list[dict[str, Any]] = []
    truncated = False
    while queue and len(out) < max_total:
        batch = queue[:30]  # batch parent IDs into one query
        queue = queue[30:]
        parent_clause = " or ".join(f"'{fid}' in parents" for fid in batch)
        q = f"({parent_clause}) and trashed = false"
        cursor: str | None = None
        for _ in range(20):  # cap pages per batch
            params: dict[str, Any] = {
                "q": q,
                "pageSize": page_size,
                "fields": DEFAULT_LIST_FIELDS,
                "orderBy": order_by,
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
                "corpora": "user",
            }
            if cursor:
                params["pageToken"] = cursor
            try:
                body = await _drive_get("/files", params=params)
            except Exception as exc:
                return _envelope_drive_error(exc)
            files = body.get("files") or []
            for f in files:
                if len(out) >= max_total:
                    truncated = True
                    break
                out.append(_trim_file(f))
                if (
                    f.get("mimeType") == "application/vnd.google-apps.folder"
                    and f["id"] not in seen_folders
                ):
                    seen_folders.add(f["id"])
                    queue.append(f["id"])
            cursor = body.get("nextPageToken")
            if not cursor or len(out) >= max_total:
                break
    return {
        "ok": True,
        "files": out,
        "count": len(out),
        "recursive": True,
        "truncated": truncated,
        "max_total": max_total,
    }


def _trim_file(f: dict) -> dict[str, Any]:
    """Trim Drive's file resource to just the fields the LLM cares about,
    in a stable shape."""
    return {
        "id": f.get("id"),
        "name": f.get("name"),
        "mime_type": f.get("mimeType"),
        "size": int(f["size"]) if f.get("size") and str(f["size"]).isdigit() else None,
        "modified_time": f.get("modifiedTime"),
        "created_time": f.get("createdTime"),
        "parents": f.get("parents") or [],
        "owners": [
            {"email": o.get("emailAddress"), "name": o.get("displayName")}
            for o in (f.get("owners") or [])
        ],
        "shared": bool(f.get("shared")),
        "trashed": bool(f.get("trashed")),
        "view_url": f.get("webViewLink"),
        "kind": (
            "folder" if f.get("mimeType") == "application/vnd.google-apps.folder"
            else "file"
        ),
        "is_google_native": (f.get("mimeType") or "").startswith("application/vnd.google-apps."),
    }


registry.register(
    ToolSpec(
        name="drive_list_files",
        description=(
            "List files in a Google Drive folder. Default folder is "
            "`root` (My Drive). Set `recursive=true` to walk subfolders "
            "(capped at 5 000 entries — use `drive_search` for cross-"
            "drive name/MIME queries instead).\n"
            "\n"
            "Each record includes: id, name, mime_type, size, "
            "modified_time, parents[], owners[{email, name}], shared, "
            "kind ('file' | 'folder'), is_google_native (true for Docs/"
            "Sheets/Slides — those need `drive_export_to_python_storage`, "
            "not `drive_download_to_python_storage`), view_url.\n"
            "\n"
            "Pagination: pass the previous reply's `next_cursor` back as "
            "`cursor` for the next page."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "folder_id": {
                    "type": "string",
                    "description": "Drive folder ID, or 'root' for My Drive (default).",
                },
                "recursive": {
                    "type": "boolean",
                    "default": False,
                    "description": "If true, descend into subfolders.",
                },
                "page_size": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 1000,
                    "default": 100,
                },
                "cursor": {
                    "type": "string",
                    "description": "Pagination cursor from a previous reply's `next_cursor`.",
                },
                "order_by": {
                    "type": "string",
                    "default": "modifiedTime desc",
                    "description": (
                        "Drive orderBy clause. Examples: 'modifiedTime desc', "
                        "'name', 'createdTime desc', 'folder,name'."
                    ),
                },
            },
            "additionalProperties": False,
        },
        handler=_drive_list_files,
        side="server",
        visibility_check=google_oauth.is_connected,
    )
)


# ---- drive_search --------------------------------------------------------


async def _drive_search(args: dict[str, Any], ctx: ToolCtx) -> Any:
    query = (args.get("query") or "").strip()
    if not query:
        return {"ok": False, "error": "invalid_args", "message": "query required"}
    page_size = int(args.get("page_size") or 50)
    page_size = max(1, min(1000, page_size))
    cursor = args.get("cursor") or None
    order_by = (args.get("order_by") or "modifiedTime desc").strip()
    # Drive's search auto-includes trashed unless excluded.
    if "trashed" not in query:
        q = f"({query}) and trashed = false"
    else:
        q = query
    return await _list_with_q(q, page_size=page_size, cursor=cursor, order_by=order_by)


registry.register(
    ToolSpec(
        name="drive_search",
        description=(
            "Search Google Drive using Drive's native query syntax — much "
            "more powerful than substring matching on a folder listing.\n"
            "\n"
            "**Query syntax** (concatenate with `and` / `or`, group with "
            "parens):\n"
            "  • `name contains 'invoice'` — name substring\n"
            "  • `fullText contains 'Q3 forecast'` — content search\n"
            "  • `mimeType = 'application/pdf'`\n"
            "  • `mimeType contains 'image/'`\n"
            "  • `mimeType = 'application/vnd.google-apps.document'` (Google Docs)\n"
            "  • `mimeType = 'application/vnd.google-apps.spreadsheet'` (Sheets)\n"
            "  • `mimeType = 'application/vnd.google-apps.folder'` (folders)\n"
            "  • `modifiedTime > '2025-01-01T00:00:00'`\n"
            "  • `'<folder_id>' in parents` — restrict to one folder\n"
            "  • `'me' in owners` / `'someone@gmail.com' in owners`\n"
            "  • `sharedWithMe = true`\n"
            "  • `starred = true`\n"
            "\n"
            "Examples:\n"
            "  • PDFs from 2024: `mimeType = 'application/pdf' and modifiedTime > '2024-01-01'`\n"
            "  • Invoices: `name contains 'invoice'`\n"
            "  • Sheets shared with me: `mimeType = 'application/vnd.google-apps.spreadsheet' and sharedWithMe = true`\n"
            "\n"
            "Trashed files are excluded automatically unless your query mentions `trashed`. "
            "Returns the same shape as `drive_list_files`."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Drive query string (see description for syntax).",
                },
                "page_size": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 1000,
                    "default": 50,
                },
                "cursor": {"type": "string"},
                "order_by": {"type": "string", "default": "modifiedTime desc"},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        handler=_drive_search,
        side="server",
        visibility_check=google_oauth.is_connected,
    )
)


# ---- drive_get_file ------------------------------------------------------


async def _drive_get_file(args: dict[str, Any], ctx: ToolCtx) -> Any:
    file_id = (args.get("file_id") or "").strip()
    if not file_id:
        return {"ok": False, "error": "invalid_args", "message": "file_id required"}
    fields = (args.get("fields") or DEFAULT_FILE_FIELDS).strip()
    try:
        body = await _drive_get(
            f"/files/{file_id}",
            params={"fields": fields, "supportsAllDrives": "true"},
        )
    except Exception as exc:
        return _envelope_drive_error(exc)
    return {"ok": True, "file": _trim_file(body) | {"raw_fields_requested": fields}}


registry.register(
    ToolSpec(
        name="drive_get_file",
        description=(
            "Get full metadata for a single Drive file by ID. Use this when "
            "you already have the ID and need fresh / extra fields beyond "
            "what `drive_list_files` returned."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "file_id": {"type": "string"},
                "fields": {
                    "type": "string",
                    "description": (
                        "Drive field mask. Default covers the same fields "
                        "as drive_list_files. Pass an explicit string to "
                        "get more (e.g. 'permissions', 'capabilities')."
                    ),
                },
            },
            "required": ["file_id"],
            "additionalProperties": False,
        },
        handler=_drive_get_file,
        side="server",
        visibility_check=google_oauth.is_connected,
    )
)


# ---- shared download core ------------------------------------------------


async def _stream_to_path(url: str, params: dict[str, Any], dest: Path) -> int:
    """Stream a Drive download/export response to ``dest``. Returns
    bytes written. Auth handled by ``_drive_get(stream=True)``."""
    token = await google_oauth.get_access_token()
    headers = {"Authorization": f"Bearer {token}"}
    written = 0
    async with httpx.AsyncClient(timeout=300.0) as client:
        async with client.stream("GET", url, params=params, headers=headers) as r:
            if r.status_code == 401:
                # one retry with forced refresh
                try:
                    from app.services import user_settings as _us
                    blob = _us.read()
                    tok = (blob.get("googleOAuth") or {}).get("tokens") or {}
                    tok["expires_at"] = 0
                    blob["googleOAuth"]["tokens"] = tok
                    _us.write(blob)
                except Exception:
                    pass
                token = await google_oauth.get_access_token()
                headers = {"Authorization": f"Bearer {token}"}
                # re-issue the stream
                pass
            if r.status_code != 200:
                body_text = await r.aread()
                raise _DriveError(r.status_code, body_text.decode("utf-8", errors="replace"))
            with open(dest, "wb") as f:
                async for chunk in r.aiter_bytes(chunk_size=64 * 1024):
                    f.write(chunk)
                    written += len(chunk)
    return written


async def _resolve_file_meta(file_id: str) -> dict[str, Any]:
    body = await _drive_get(
        f"/files/{file_id}",
        params={
            "fields": "id,name,mimeType,size,parents,owners(emailAddress,displayName),shared,modifiedTime,webViewLink",
            "supportsAllDrives": "true",
        },
    )
    return body


# Per-process cache of folder metadata (id → {name, parents}) for path
# resolution. Drive folders move/rename rarely; we accept that an
# in-process restart wipes the cache. Folder fetches are a few hundred
# ms each, so caching matters when downloading multiple files from the
# same nested folder.
_folder_cache: dict[str, dict[str, Any]] = {}


async def _get_folder(folder_id: str) -> dict[str, Any] | None:
    """Return ``{id, name, parents}`` for a folder, or None if not
    accessible. Caches on success."""
    cached = _folder_cache.get(folder_id)
    if cached is not None:
        return cached
    try:
        body = await _drive_get(
            f"/files/{folder_id}",
            params={"fields": "id,name,parents", "supportsAllDrives": "true"},
        )
    except Exception:
        return None
    _folder_cache[folder_id] = body
    return body


async def _resolve_drive_path(file_meta: dict[str, Any]) -> str:
    """Build a human-readable Drive path: ``/My Drive/Folder A/Folder B/file.pdf``.

    ``file_meta`` must include ``name`` and ``parents``. Walks parents
    upward until we hit a folder with no further parents (the Drive
    root) or our walk-cap of 12 levels. Each folder lookup is one REST
    call (cached). On failure (permissions / 404) we substitute
    ``"<folder?>"`` and continue.
    """
    name = file_meta.get("name") or "?"
    parents = file_meta.get("parents") or []
    if not parents:
        # No parent → root or shared-with-me-loose. Just return the name.
        return f"/{name}"

    chain: list[str] = []
    current_id = parents[0]
    for _ in range(12):
        folder = await _get_folder(current_id)
        if folder is None:
            chain.append("<folder?>")
            break
        chain.append(folder.get("name") or "<folder?>")
        next_parents = folder.get("parents") or []
        if not next_parents:
            # Reached Drive root.
            chain.append("My Drive")
            break
        current_id = next_parents[0]
    return "/" + "/".join(reversed(chain)) + "/" + name


def _drive_view_url(file_id: str) -> str:
    return f"https://drive.google.com/file/d/{file_id}/view"


# ---- drive_download_to_python_storage ------------------------------------


async def _drive_download(args: dict[str, Any], ctx: ToolCtx) -> Any:
    file_id = (args.get("file_id") or "").strip()
    if not file_id:
        return {"ok": False, "error": "invalid_args", "message": "file_id required"}
    name_override = (args.get("name") or "").strip() or None
    force_refresh = bool(args.get("force_refresh", False))

    started = time.time()

    # Reuse cached snapshot if recent.
    if not force_refresh:
        existing = python_storage.find_latest_by_meta(
            lambda m: m.get("kind") == "drive_file"
            and m.get("drive_file_id") == file_id
        )
        if existing is not None:
            try:
                age_s = time.time() - Path(existing["path"]).stat().st_mtime
            except OSError:
                age_s = _REUSE_TTL_S + 1
            if age_s <= _REUSE_TTL_S:
                files = existing.get("files") or []
                stored_name = (
                    existing["meta"].get("stored_name")
                    or (files[0]["name"] if files else "")
                )
                return {
                    "ok": True,
                    "file_id": file_id,
                    "handle": existing["handle"],
                    "stored_name": stored_name,
                    "path": existing["path"],
                    "bytes": files[0]["bytes"] if files else 0,
                    "elapsed_s": round(time.time() - started, 2),
                    "reused": True,
                    "reused_age_s": round(age_s, 1),
                }

    # In-flight dedup.
    inflight = _inflight.get(file_id)
    if inflight is not None and not inflight.done():
        try:
            shared = await inflight
            if isinstance(shared, dict) and shared.get("ok"):
                merged = dict(shared)
                merged["coalesced"] = True
                merged["elapsed_s"] = round(time.time() - started, 2)
                return merged
            return shared
        except Exception as exc:
            return {"ok": False, "error": "inflight_failed", "message": str(exc)}

    loop = _asyncio.get_running_loop()
    fut: _asyncio.Future[Any] = loop.create_future()
    _inflight[file_id] = fut
    try:
        result = await _do_drive_download(
            file_id, name_override=name_override, started=started
        )
        fut.set_result(result)
        return result
    except Exception as exc:
        if not fut.done():
            fut.set_exception(exc)
        raise
    finally:
        _inflight.pop(file_id, None)


async def _do_drive_download(
    file_id: str, *, name_override: str | None, started: float,
) -> dict[str, Any]:
    # Need the metadata to know the filename + reject Google native formats.
    try:
        meta = await _resolve_file_meta(file_id)
    except Exception as exc:
        return _envelope_drive_error(exc)

    mime = meta.get("mimeType") or ""
    if mime.startswith("application/vnd.google-apps."):
        return {
            "ok": False,
            "error": "google_native_format",
            "mime_type": mime,
            "message": (
                f"This is a Google native format ({mime}). It can't be "
                "downloaded as bytes; use drive_export_to_python_storage "
                "instead, optionally specifying a `format` (txt/pdf/csv/etc)."
            ),
        }

    name = name_override or meta.get("name") or file_id
    # Stage to a tmp file, then hand to python_storage.put_file (which
    # moves it into a snapshot dir + writes meta.json).
    import tempfile

    fd, tmp_path_str = tempfile.mkstemp(prefix=f"drive_{file_id}_", suffix="_" + name)
    import os
    os.close(fd)
    tmp_path = Path(tmp_path_str)

    url = f"{DRIVE_API_BASE}/files/{file_id}"
    params = {"alt": "media", "supportsAllDrives": "true"}
    try:
        bytes_written = await _stream_to_path(url, params, tmp_path)
    except Exception as exc:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        return _envelope_drive_error(exc)

    # Resolve account + path for the origin block. Both best-effort —
    # if either lookup fails we still ingest the file with a partial
    # origin (None'd fields) rather than fail the whole download.
    drive_path = await _resolve_drive_path(meta)
    account = (google_oauth._get_tokens() or {}).get("account_email")
    owners = [
        {"email": o.get("emailAddress"), "name": o.get("displayName")}
        for o in (meta.get("owners") or [])
    ]

    snap = python_storage.put_file(
        src_path=tmp_path,
        original_name=name,
        kind="drive_file",
        meta={
            "origin": python_storage.make_origin(
                source="google_drive",
                account=account,
                path=drive_path,
                file_id=file_id,
                host="drive.google.com",
                url=meta.get("webViewLink") or _drive_view_url(file_id),
                extra={
                    "mime_type": mime,
                    "owners": owners,
                    "shared": bool(meta.get("shared")),
                    "modified_time": meta.get("modifiedTime"),
                },
            ),
            # Legacy convenience fields (kept for dedup lookups +
            # existing callers that look at meta.drive_file_id).
            "drive_file_id": file_id,
            "drive_mime_type": mime,
        },
        move=True,
    )
    return {
        "ok": True,
        "file_id": file_id,
        "handle": snap["handle"],
        "stored_name": snap["meta"].get("stored_name"),
        "path": snap["path"],
        "bytes": bytes_written,
        "mime_type": mime,
        "drive_path": drive_path,
        "account": account,
        "elapsed_s": round(time.time() - started, 2),
    }


registry.register(
    ToolSpec(
        name="drive_download_to_python_storage",
        description=(
            "Download a Drive file's binary content into python_storage so "
            "compute scripts (and other tools) can read it from disk.\n"
            "\n"
            "Use for: PDFs, images, CSVs, source files, .xlsx / .docx that "
            "were UPLOADED (not Google-native), zips, anything with raw "
            "bytes. NOT for Google Docs / Sheets / Slides / Drawings — "
            "use `drive_export_to_python_storage` for those.\n"
            "\n"
            "**Dedup**: if a snapshot for this `file_id` already exists "
            "in python_storage and is < 24 h old, this tool reuses it "
            "(`reused: true`) — no new download fired. Pass "
            "`force_refresh: true` only if the user said the file's "
            "content changed.\n"
            "\n"
            "Returns `{handle, stored_name, path, bytes, mime_type}`. "
            "Feed `handle` into `run_compute` (via `ctx.snapshot(handle)`) "
            "to actually read the bytes."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "file_id": {
                    "type": "string",
                    "description": "Drive file ID, from drive_list_files / drive_search.",
                },
                "name": {
                    "type": "string",
                    "description": (
                        "Optional override for the stored filename. "
                        "Default: the file's name in Drive."
                    ),
                },
                "force_refresh": {
                    "type": "boolean",
                    "default": False,
                    "description": "Bypass the 24 h dedup cache.",
                },
            },
            "required": ["file_id"],
            "additionalProperties": False,
        },
        handler=_drive_download,
        side="server",
        visibility_check=google_oauth.is_connected,
    )
)


# ---- drive_export_to_python_storage --------------------------------------


async def _drive_export(args: dict[str, Any], ctx: ToolCtx) -> Any:
    file_id = (args.get("file_id") or "").strip()
    if not file_id:
        return {"ok": False, "error": "invalid_args", "message": "file_id required"}
    fmt = (args.get("format") or "").strip().lower()
    force_refresh = bool(args.get("force_refresh", False))

    started = time.time()

    try:
        meta = await _resolve_file_meta(file_id)
    except Exception as exc:
        return _envelope_drive_error(exc)
    mime_in = meta.get("mimeType") or ""
    if not mime_in.startswith("application/vnd.google-apps."):
        return {
            "ok": False,
            "error": "not_google_native",
            "mime_type": mime_in,
            "message": (
                "This file is a regular binary, not a Google native "
                "format. Use drive_download_to_python_storage instead."
            ),
        }

    if fmt:
        if fmt not in _FORMAT_OPTIONS:
            return {
                "ok": False,
                "error": "invalid_format",
                "message": f"format must be one of {sorted(_FORMAT_OPTIONS)}",
            }
        export_mime, ext = _FORMAT_OPTIONS[fmt]
    else:
        export_mime, ext = _GOOGLE_FORMAT_DEFAULTS.get(
            mime_in, ("application/pdf", "pdf")
        )

    # Reuse cached export with same format.
    cache_key_meta_match = (
        lambda m: m.get("kind") == "drive_file"
        and m.get("drive_file_id") == file_id
        and m.get("drive_export_mime") == export_mime
    )
    if not force_refresh:
        existing = python_storage.find_latest_by_meta(cache_key_meta_match)
        if existing is not None:
            try:
                age_s = time.time() - Path(existing["path"]).stat().st_mtime
            except OSError:
                age_s = _REUSE_TTL_S + 1
            if age_s <= _REUSE_TTL_S:
                files = existing.get("files") or []
                return {
                    "ok": True,
                    "file_id": file_id,
                    "handle": existing["handle"],
                    "stored_name": existing["meta"].get("stored_name"),
                    "path": existing["path"],
                    "bytes": files[0]["bytes"] if files else 0,
                    "format": fmt or _mime_to_token(export_mime),
                    "export_mime": export_mime,
                    "source_mime_type": mime_in,
                    "elapsed_s": round(time.time() - started, 2),
                    "reused": True,
                    "reused_age_s": round(age_s, 1),
                }

    name = meta.get("name") or file_id
    name_with_ext = f"{name}.{ext}" if not name.lower().endswith("." + ext) else name

    import tempfile, os
    fd, tmp_path_str = tempfile.mkstemp(
        prefix=f"drive_export_{file_id}_", suffix="_" + name_with_ext,
    )
    os.close(fd)
    tmp_path = Path(tmp_path_str)

    url = f"{DRIVE_API_BASE}/files/{file_id}/export"
    params = {"mimeType": export_mime}
    try:
        bytes_written = await _stream_to_path(url, params, tmp_path)
    except Exception as exc:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        return _envelope_drive_error(exc)

    drive_path = await _resolve_drive_path(meta)
    account = (google_oauth._get_tokens() or {}).get("account_email")
    owners = [
        {"email": o.get("emailAddress"), "name": o.get("displayName")}
        for o in (meta.get("owners") or [])
    ]

    snap = python_storage.put_file(
        src_path=tmp_path,
        original_name=name_with_ext,
        kind="drive_file",
        meta={
            "origin": python_storage.make_origin(
                source="google_drive_export",
                account=account,
                path=drive_path,
                file_id=file_id,
                host="drive.google.com",
                url=meta.get("webViewLink") or _drive_view_url(file_id),
                extra={
                    "source_mime_type": mime_in,
                    "export_mime": export_mime,
                    "export_format": fmt or _mime_to_token(export_mime),
                    "owners": owners,
                    "shared": bool(meta.get("shared")),
                    "modified_time": meta.get("modifiedTime"),
                },
            ),
            # Legacy convenience fields kept for the dedup lookups.
            "drive_file_id": file_id,
            "drive_mime_type": mime_in,
            "drive_export_mime": export_mime,
        },
        move=True,
    )
    return {
        "ok": True,
        "file_id": file_id,
        "handle": snap["handle"],
        "stored_name": snap["meta"].get("stored_name"),
        "path": snap["path"],
        "bytes": bytes_written,
        "format": fmt or _mime_to_token(export_mime),
        "export_mime": export_mime,
        "source_mime_type": mime_in,
        "drive_path": drive_path,
        "account": account,
        "elapsed_s": round(time.time() - started, 2),
    }


def _mime_to_token(mime: str) -> str:
    for tok, (m, _ext) in _FORMAT_OPTIONS.items():
        if m == mime:
            return tok
    return mime


registry.register(
    ToolSpec(
        name="drive_export_to_python_storage",
        description=(
            "Export a Google native file (Doc / Sheet / Slide / Drawing) "
            "into a non-native format and store the result in "
            "python_storage. Use this for any file whose `mime_type` "
            "starts with `application/vnd.google-apps.`.\n"
            "\n"
            "If `format` is omitted, picks a sensible default per source "
            "type: Docs → txt, Sheets → csv, Slides → pdf, Drawings → "
            "png.\n"
            "\n"
            "Returns `{handle, stored_name, path, bytes, format, "
            "export_mime, source_mime_type}` — same handle flavour as "
            "drive_download_to_python_storage. Cached for 24 h per "
            "(file_id, export_mime); pass `force_refresh: true` to "
            "bypass."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "file_id": {"type": "string"},
                "format": {
                    "type": "string",
                    "enum": sorted(_FORMAT_OPTIONS.keys()),
                    "description": (
                        "Export format. Omit to use the default for the "
                        "source type (Docs→txt, Sheets→csv, Slides→pdf, "
                        "Drawings→png)."
                    ),
                },
                "force_refresh": {"type": "boolean", "default": False},
            },
            "required": ["file_id"],
            "additionalProperties": False,
        },
        handler=_drive_export,
        side="server",
        visibility_check=google_oauth.is_connected,
    )
)


# ---- drive_pickup_to_python_storage --------------------------------------
#
# Hacky fallback path used when OAuth isn't connected. The user opted in
# via the Settings panel; we drive their *logged-in* browser to download
# the file (no API token involved) and then move it out of their
# Downloads folder into python_storage. See services/drive_pickup.py for
# the directory-watching mechanics and the unavoidable race conditions.


def _pickup_visible() -> bool:
    """Tool gate: pickup is only exposed when (a) OAuth is NOT connected
    AND (b) the user has explicitly enabled it in Settings. Both checks
    happen on every chat turn (cheap)."""
    if google_oauth.is_connected():
        return False
    try:
        blob = user_settings.read()
    except Exception:
        return False
    return bool(blob.get("driveDownloadViaPickup"))


_DEFAULT_PICKUP_DIR = "~/Downloads"

# The Drive interstitial (consent / virus-scan / sign-in) can stall the
# download for tens of seconds; 120 s gives the user a comfortable window
# to click through.
_PICKUP_TIMEOUT_S = 120.0


import logging  # noqa: E402

_drive_pickup_logger = logging.getLogger("app.drive_pickup")


async def _drive_pickup(args: dict[str, Any], ctx: ToolCtx) -> Any:
    file_id = (args.get("file_id") or "").strip()
    pick_filename = (args.get("pick_filename") or "").strip() or None
    if not file_id and not pick_filename:
        return {
            "ok": False,
            "error": "invalid_args",
            "message": "file_id (or pick_filename, for adopt mode) required",
        }
    name_hint = (args.get("name") or "").strip() or None
    timeout_s = float(args.get("timeout_s") or _PICKUP_TIMEOUT_S)
    timeout_s = max(5.0, min(600.0, timeout_s))

    # Capture every progress line for inclusion in the result envelope —
    # the chat UI doesn't surface tool stdout, so without this the user
    # has no way to see *why* the watcher waited (interstitial click,
    # partial sibling deferral, slow connection, etc). Mirror to the
    # Python logger so it also appears in the backend's stdout.
    progress_log: list[str] = []

    def _log(msg: str) -> None:
        ts = time.strftime("%H:%M:%S")
        progress_log.append(f"{ts}  {msg}")
        _drive_pickup_logger.info(msg)

    settings_blob = user_settings.read()
    dl_dir_str = (
        (settings_blob.get("pickupDownloadsDir") or "").strip()
        or _DEFAULT_PICKUP_DIR
    )
    dl_dir = drive_pickup.expand_dir(dl_dir_str)
    if not dl_dir.is_dir():
        return {
            "ok": False,
            "error": "downloads_dir_missing",
            "message": (
                f"Downloads directory {dl_dir!s} does not exist. "
                "Update `pickupDownloadsDir` in Settings or create the path."
            ),
        }

    started = time.time()
    _log(
        f"pickup start: file_id={file_id or '(adopt-mode)'} "
        f"name_hint={name_hint!r} pick_filename={pick_filename!r} "
        f"dir={dl_dir!s}"
    )

    # ---- adopt-only path (skip browser trigger) ---------------------------
    # Used when the user already downloaded the file and just wants us to
    # move it into python_storage. ``pick_filename`` adopts a specific
    # file by exact name; without it but with ``name`` we look for a
    # unique recent match.
    if pick_filename and not file_id:
        target = dl_dir / pick_filename
        if not target.is_file():
            _log(f"pick_filename {pick_filename!r} not found in {dl_dir!s}")
            return {
                "ok": False,
                "error": "pick_filename_not_found",
                "message": f"{pick_filename!r} is not a file in {dl_dir!s}.",
                "downloads_dir": str(dl_dir),
                "progress_log": progress_log,
            }
        _log(f"adopt-pick: moving {target!s} into python_storage")
        return _adopt_from_path(
            target, dl_dir, file_id="", started=started, via="adopt_pick",
            progress_log=progress_log,
        )

    # ---- normal path: trigger + watch -------------------------------------
    baseline = drive_pickup.snapshot_listing(dl_dir)
    download_url = drive_pickup.drive_uc_download_url(file_id)
    _log(f"baseline: {len(baseline)} files in {dl_dir!s}")

    # Anchor-click trigger: no popup, no new tab, just a download. The
    # Save-As dialog appears only when the user has Chrome's "Ask where
    # to save each file" pref on — that's a browser setting we can't
    # override. The watcher handles however the file lands.
    _log(f"trigger_download: {download_url}")
    try:
        await call_browser(
            "trigger_download",
            {"url": download_url, "filename": name_hint or ""},
            ctx,
        )
    except BrowserToolError as exc:
        _log(f"trigger_download failed: {exc.kind}: {exc!s}")
        # Surface the URL in the error envelope so the LLM can render it
        # as a clickable hyperlink for the user — manual click + the
        # adopt fallback below recovers the flow.
        return {
            "ok": False,
            "error": "browser_primitive_failed",
            "kind": exc.kind,
            "message": str(exc),
            "manual_download_url": download_url,
            "manual_instructions": (
                f"Open this URL in your browser, save the file to "
                f"{dl_dir!s}, then call this tool again with "
                f"`pick_filename` set to whatever name appears in the "
                "folder."
            ),
            "progress_log": progress_log,
        }

    picked = await drive_pickup.wait_for_new_file(
        dl_dir, baseline, timeout_s=timeout_s, name_hint=name_hint, log=_log,
    )

    # ---- timeout fallback: scan recent matching files --------------------
    # The watcher only sees files that didn't exist at baseline. If the
    # user already had the file there (e.g. earlier retry) or the
    # download was in flight before we started the timer, the watcher
    # misses it. Sweep recent files matching the hint to recover.
    used_fallback = False
    candidates_meta: list[dict[str, Any]] = []
    if picked is None and (name_hint or pick_filename):
        if pick_filename:
            target = dl_dir / pick_filename
            if target.is_file():
                picked = target
                used_fallback = True
                _log(f"timeout fallback: adopting pick_filename {target!s}")
        else:
            recent = drive_pickup.find_recent_matching(dl_dir, name_hint or "")
            candidates_meta = [
                {
                    "filename": p.name,
                    "size": p.stat().st_size,
                    "mtime": round(p.stat().st_mtime, 2),
                }
                for p in recent[:10]
            ]
            _log(
                f"timeout fallback: scanning recent matches for {name_hint!r} "
                f"→ {len(recent)} hit(s)"
            )
            if len(recent) == 1:
                picked = recent[0]
                used_fallback = True
                _log(f"timeout fallback: adopting unique match {picked.name!r}")
            elif len(recent) > 1:
                _log(
                    f"timeout fallback: ambiguous match — {len(recent)} files "
                    f"need disambiguation via pick_filename"
                )
                return {
                    "ok": False,
                    "error": "ambiguous_match",
                    "message": (
                        f"{len(recent)} files in {dl_dir!s} match "
                        f"{name_hint!r} and were modified in the last 10 "
                        "minutes. Re-call with `pick_filename` set to one "
                        "of the candidates below."
                    ),
                    "candidates": candidates_meta,
                    "downloads_dir": str(dl_dir),
                    "elapsed_s": round(time.time() - started, 2),
                    "progress_log": progress_log,
                }

    if picked is None:
        return {
            "ok": False,
            "error": "pickup_timeout",
            "message": (
                f"No matching file appeared in {dl_dir!s} within "
                f"{timeout_s:.0f}s. If Chrome popped a Save-As dialog, "
                "the user may not have completed it. Either: (a) call "
                "this tool again with `pick_filename` once the file "
                "lands, or (b) ask the user to disable Chrome's "
                "'Ask where to save each file' preference."
            ),
            "manual_download_url": download_url,
            "downloads_dir": str(dl_dir),
            "elapsed_s": round(time.time() - started, 2),
            "progress_log": progress_log,
        }

    return _adopt_from_path(
        picked, dl_dir, file_id=file_id, started=started,
        via="adopt_fallback" if used_fallback else "watch",
        progress_log=progress_log,
    )


def _sniff_soundcheck(path: Path) -> str | None:
    """Return the SoundCheck cluster type ("DAT"/"WFM"/"RES") if this
    is a SoundCheck binary, else ``None``. Cheap header peek so we can
    tell the LLM "use dat_parse" instead of leaving it to scan strings.
    """
    try:
        with open(path, "rb") as f:
            head = f.read(8 + 4 + 16)
    except OSError:
        return None
    if len(head) < 28:
        return None
    tag = head[8:24].decode("ascii", errors="replace").rstrip()
    return {"Data": "DAT", "Waveform": "WFM", "Result": "RES"}.get(tag)


def _try_auto_parse_soundcheck(snap_dir: str, log: Any = None) -> dict[str, Any] | None:
    """If the file in ``snap_dir`` is a SoundCheck binary, run the
    canonical parser and write the same artefacts ``dat_parse`` would.

    Critical to call this *before* returning from the pickup tool — the
    LLM has demonstrated a tendency to roll its own byte-decoder via
    ``run_compute`` when handed a raw SoundCheck snapshot. Pre-parsing
    closes that loophole and returns the parsed summary directly.
    Parser failures are non-fatal — the snapshot still exists and the
    LLM can fall back to ``dat_parse`` manually.
    """
    try:
        from app.services import soundcheck_dat
        dat_path = soundcheck_dat.find_dat_in_dir(snap_dir)
    except Exception:
        return None
    try:
        return soundcheck_dat.parse_dat_to_snapshot(dat_path, snap_dir, log=log)
    except Exception as exc:  # pragma: no cover — parser failure
        if log is not None:
            log(f"auto-parse failed: {type(exc).__name__}: {exc}")
        return {"auto_parse_error": f"{type(exc).__name__}: {exc}"}


def _adopt_from_path(
    src: Path, dl_dir: Path, *, file_id: str, started: float, via: str,
    progress_log: list[str] | None = None,
) -> dict[str, Any]:
    """Move ``src`` into python_storage and return the standard envelope.

    ``put_file(move=True)`` does a ``shutil.move`` (rename when possible,
    copy+unlink otherwise). The copy+unlink fallback silently swallows
    unlink errors if the copy succeeded but the source remove failed —
    which leaves an orphan in Downloads. We post-verify and force-remove
    so the contract "source goes away after a successful pickup" holds
    even on the rare cross-volume / locked-file path.
    """
    soundcheck_kind = _sniff_soundcheck(src)
    snap = python_storage.put_file(
        src_path=src,
        original_name=src.name,
        kind="soundcheck_dat" if soundcheck_kind else "drive_file",
        meta={
            "origin": python_storage.make_origin(
                source="google_drive_pickup",
                account=None,
                path=None,
                file_id=file_id or None,
                host="drive.google.com",
                url=(
                    f"https://drive.google.com/file/d/{file_id}/view"
                    if file_id
                    else None
                ),
                extra={
                    "pickup_dir": str(dl_dir),
                    "picked_filename": src.name,
                    "pickup_mode": via,
                },
            ),
            "drive_file_id": file_id or None,
            "via_pickup": True,
            "pickup_mode": via,
        },
        move=True,
    )

    cleanup_error: str | None = None
    if src.exists():
        try:
            src.unlink()
        except OSError as exc:
            cleanup_error = f"{type(exc).__name__}: {exc}"

    out: dict[str, Any] = {
        "ok": cleanup_error is None,
        "file_id": file_id or None,
        "handle": snap["handle"],
        "stored_name": snap["meta"].get("stored_name"),
        "path": snap["path"],
        "bytes": (snap["files"][0] or {}).get("bytes") if snap.get("files") else None,
        "picked_filename": src.name,
        "downloads_dir": str(dl_dir),
        "removed_from_downloads": not src.exists(),
        "elapsed_s": round(time.time() - started, 2),
        "via_pickup": True,
        "pickup_mode": via,
    }
    if soundcheck_kind:
        # Auto-run the canonical SoundCheck parser. Returning a parsed
        # summary instead of raw bytes is the only reliable way to stop
        # the LLM from rolling its own bit-banger via run_compute. We
        # leave the detected_format hint in case auto-parse misses (e.g.
        # exotic future variant).
        out["detected_format"] = f"soundcheck_{soundcheck_kind.lower()}"

        def _parse_log(msg: str) -> None:
            if progress_log is not None:
                progress_log.append(f"  parse: {msg}")

        parse_summary = _try_auto_parse_soundcheck(snap["path"], log=_parse_log)
        if parse_summary and "auto_parse_error" not in parse_summary:
            out["auto_parsed"] = True
            out["parse_summary"] = {
                k: v for k, v in parse_summary.items()
                if k in ("kind", "n_items", "cluster_versions",
                         "curve_kinds", "n_unique_serials",
                         "unique_unit_triples", "all_clean_parses",
                         "first_curve_name", "elapsed_s")
            }
            # Tell the LLM exactly what to do next — a single tool call
            # away from a rendered plot. Keep the "do not hand-decode"
            # warning since pickup-then-roll-own happened in practice.
            out["next_action"] = (
                f"File is already parsed — {parse_summary.get('n_items')} "
                f"curves into `dat_curves.json` + `curves.pkl` in the "
                f"snapshot dir. To visualise: "
                f"`show_holoviz_report(name=\"dat_curves\")`. "
                f"DO NOT re-parse with a custom compute script."
            )
        else:
            # Fallback: parser unavailable / failed. Surface the
            # canonical compute-script call as a directive.
            err_note = (
                parse_summary.get("auto_parse_error")
                if parse_summary else "parser not invoked"
            )
            out["auto_parsed"] = False
            out["auto_parse_error"] = err_note
            out["next_action"] = (
                f"This is a SoundCheck {soundcheck_kind} binary. Call "
                f"`run_compute(name=\"dat_parse\", args={{\"snapshot\": "
                f"\"{snap['handle']}\"}})` — the parser is in "
                f"`backend/app/services/soundcheck_dat.py` and handles "
                f"DAT v2/v3/v6 + WFM + RES. DO NOT write a custom byte "
                f"decoder."
            )
    if progress_log is not None:
        out["progress_log"] = progress_log
    if cleanup_error is not None:
        out["error"] = "downloads_cleanup_failed"
        out["message"] = (
            f"File copied into python_storage as {snap['handle']!r}, "
            f"but the original {src!s} could not be removed: "
            f"{cleanup_error}. Delete it manually."
        )
    return out


registry.register(
    ToolSpec(
        name="drive_pickup_to_python_storage",
        description=(
            "Hacky fallback for downloading a Drive file when Google OAuth "
            "is NOT connected. Triggers a download via an `<a download>` "
            "click (no popup, no new tab) — your existing Google session "
            "handles the auth — then watches the configured Downloads "
            "folder for the resulting file and moves it into "
            "python_storage.\n"
            "\n"
            "Three modes:\n"
            "  • `file_id` only — trigger a download and watch.\n"
            "  • `file_id` + `name` — same, but the watcher prefers files "
            "    whose name matches the hint (token-overlap), AND if the "
            "    watch window times out it falls back to scanning recent "
            "    (last 10 min) matching files — recovering from previous "
            "    failed retries that left the file already in Downloads.\n"
            "  • `pick_filename` only — adopt-mode: skip the trigger, "
            "    move the named file from Downloads into python_storage. "
            "    Use this when the user has manually downloaded the file.\n"
            "\n"
            "Visible only when (a) OAuth is not connected AND (b) the user "
            "enabled `driveDownloadViaPickup` in Settings. Downloads dir "
            "is configurable via `pickupDownloadsDir` (default `~/Downloads`).\n"
            "\n"
            "Caveats:\n"
            "  • Save-As dialog: if the user has Chrome's 'Ask where to "
            "    save each file' preference on, a system dialog appears "
            "    and the user must click Save. We can't override that. "
            "    Either ask them to disable the pref or use adopt-mode.\n"
            "  • Racy. If `name` is missing and the user downloads "
            "    something else during the watch window, we may pick the "
            "    wrong file. Always pass `name` when you have it.\n"
            "  • Multiple-match ambiguity: if the timeout-fallback finds "
            "    several recent files matching `name`, the tool returns "
            "    `error: 'ambiguous_match'` with a `candidates` list — "
            "    re-call with `pick_filename` set to one of them.\n"
            "  • No Drive API metadata: only filename + byte count.\n"
            "\n"
            "Returns `{handle, stored_name, path, bytes, picked_filename, "
            "pickup_mode}` on success. Feed `handle` into `run_compute`.\n"
            "\n"
            "Auto-parsing: if the file sniffs as a SoundCheck binary "
            "(.dat / .wfm / .res), the canonical parser runs immediately "
            "and the response includes `parse_summary` (curve_kinds, "
            "serials, units) plus a `next_action` directive. **Do not "
            "write a custom byte decoder** — `dat_parse` and the "
            "`dat_curves` Panel report are the only supported analysis "
            "paths."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "file_id": {
                    "type": "string",
                    "description": (
                        "Drive file ID. Even without API access you can "
                        "extract this from a share URL "
                        "(`drive.google.com/file/d/<file_id>/view`). "
                        "Required unless `pick_filename` is set."
                    ),
                },
                "name": {
                    "type": "string",
                    "description": (
                        "Expected filename (or substring). Used to "
                        "disambiguate during the watch window AND as the "
                        "name-hint for the timeout fallback scan. Always "
                        "pass when known."
                    ),
                },
                "pick_filename": {
                    "type": "string",
                    "description": (
                        "Adopt mode: exact filename in the Downloads "
                        "directory to move into python_storage. Skips "
                        "the browser trigger. Use after an "
                        "`ambiguous_match` error or when the user has "
                        "downloaded the file manually."
                    ),
                },
                "timeout_s": {
                    "type": "number",
                    "minimum": 5,
                    "maximum": 600,
                    "default": 120,
                    "description": "How long to wait for the file to land. Default 120 s.",
                },
            },
            "additionalProperties": False,
        },
        handler=_drive_pickup,
        side="hybrid",
        visibility_check=_pickup_visible,
    )
)


