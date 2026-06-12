"""``vre://`` resolver — owned by the voitta-enterprise plugin.

Refs reach this resolver as already-parsed :class:`refs.Ref` objects.

URI grammar::

    vre://<folder_display_name>/<relative/path/to/file.ext>[?params]

Query params:
  • ``asset``  — asset type (``original``, ``cad_mesh``, ``cad_projection``).
                 Defaults to ``original``.
  • ``slug``   — required for per-component CAD asset variants.
  • ``export`` — for ``cad_projection``, pick one view; omit for all views.

Examples::

    vre://Stella NFS/stella_park_prepared/docs/report.pdf
    vre://Stella NFS/parts/base-frame.glb?asset=cad_mesh
    vre://Stella NFS/parts/rail-l.glb?asset=cad_projection&export=iso

Resolution flow:

  1. Resolve folder display_name → folder id via ``list_indexed_folders``
     (cached in module-level ``_FOLDER_CACHE``; refreshed on miss).
  2. Resolve folder_id + rel_path → file_id via
     ``list_indexed_folders(prefix="<display_name>/<rel_path_parent>")``,
     matching the leaf filename.
  3. Call ``request_asset(file_id, asset_type, slug=…)`` → signed URLs.
  4. Stream each URL into a fresh snapshot dir under
     ``python_storage/cache/snapshot_<handle>/``. ``meta.json::origin``
     is stamped with the canonical ``ref`` so future lookups can cache.
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from pathlib import Path, PurePosixPath
from typing import Any

import httpx

from app.services import ensure_local as _ensure_local, python_storage, refs
from app.services.mcp import client as mcp_client
from app.services.mcp import registry as mcp_registry

_logger = logging.getLogger(__name__)

_CHUNK = 64 * 1024
_MAX_BYTES = 512 * 1024 * 1024  # 512 MiB

_VRE_CONNECTOR_KEY = "voitta-enterprise:vre"

# Module-level cache: display_name (lowered) → folder id.
# Populated lazily; cleared on miss so new folders appear after a
# server-side re-index without a restart.
_FOLDER_CACHE: dict[str, int] = {}


# ---------------------------------------------------------------------------
# Connector helpers
# ---------------------------------------------------------------------------


def _connector_endpoint() -> tuple[str, str | None]:
    plugin, _, cid = _VRE_CONNECTOR_KEY.partition(":")
    conn = mcp_registry.get_connector(plugin, cid)
    if conn is None:
        raise RuntimeError(
            f"VRE MCP connector not registered "
            f"(plugin {plugin!r}, id {cid!r}) — is voitta-enterprise loaded?"
        )
    # No page host in scope here (resolver runs inside script
    # execution) — endpoint_for falls back to the URL the last
    # successful refresh used.
    url = mcp_registry.endpoint_for(conn)
    if not url:
        raise RuntimeError(
            "VRE MCP endpoint unknown — no explicit URL configured and no "
            "successful refresh yet. Open Settings → Plugins and hit "
            "'Refresh tool list'."
        )
    token = mcp_registry.token_for(conn.decl, url)
    return url, token


async def _mcp_call(tool: str, args: dict[str, Any]) -> Any:
    """Call a VRE MCP tool and return the parsed structured payload."""
    url, bearer = _connector_endpoint()
    result = await mcp_client.call_tool(url, bearer, tool, args)
    if result.is_error:
        text = "; ".join(
            getattr(b, "text", "") for b in (result.content or []) if getattr(b, "text", "")
        )
        raise RuntimeError(f"{tool} error: {text or 'unknown'}")
    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict):
        return structured
    # Some fastmcp versions return text; parse it.
    text_blob = "\n".join(
        getattr(b, "text", "") for b in (result.content or []) if getattr(b, "text", "")
    )
    try:
        return json.loads(text_blob)
    except Exception as exc:
        raise RuntimeError(f"{tool} returned unparseable payload: {exc}") from exc


# ---------------------------------------------------------------------------
# Folder / file resolution
# ---------------------------------------------------------------------------


async def _fetch_folder_cache() -> dict[str, int]:
    """Fetch all top-level folders and return display_name.lower() → id."""
    payload = await _mcp_call("list_indexed_folders", {})
    folders = payload.get("result") or payload if isinstance(payload, list) else payload.get("result", [])
    if isinstance(payload, list):
        folders = payload
    return {
        entry["display_name"].lower(): entry["id"]
        for entry in (folders if isinstance(folders, list) else [])
        if "display_name" in entry and "id" in entry
    }


async def _resolve_folder_id(display_name: str) -> int:
    """Return the folder id for ``display_name``. Refreshes cache on miss."""
    key = display_name.lower()
    if key in _FOLDER_CACHE:
        return _FOLDER_CACHE[key]
    fresh = await _fetch_folder_cache()
    _FOLDER_CACHE.clear()
    _FOLDER_CACHE.update(fresh)
    if key not in _FOLDER_CACHE:
        raise RuntimeError(
            f"VRE folder {display_name!r} not found. "
            f"Available: {sorted(_FOLDER_CACHE)}"
        )
    return _FOLDER_CACHE[key]


async def _resolve_file_id(display_name: str, rel_path: str) -> int:
    """Return the file_id for ``display_name/rel_path``.

    Strategy: list the parent directory (one level up from the target
    filename) and match by name. The ``path`` field from VRE is
    ``"display_name/rel/path"`` so we match by the leaf name.
    """
    ppath = PurePosixPath(rel_path)
    parent_rel = str(ppath.parent) if ppath.parent != PurePosixPath(".") else ""
    leaf = ppath.name

    prefix = f"{display_name}/{parent_rel}" if parent_rel else display_name
    payload = await _mcp_call("list_indexed_folders", {"prefix": prefix})
    entries = payload.get("result") or (payload if isinstance(payload, list) else [])
    if isinstance(payload, list):
        entries = payload

    for entry in (entries if isinstance(entries, list) else []):
        if entry.get("kind") == "file" and entry.get("name") == leaf:
            fid = entry.get("file_id")
            if fid is not None:
                return int(fid)

    raise RuntimeError(
        f"VRE file not found: {display_name}/{rel_path}. "
        f"Listed {len(entries) if isinstance(entries, list) else '?'} entries "
        f"under {prefix!r}."
    )


# ---------------------------------------------------------------------------
# Asset download helpers  (unchanged from prior implementation)
# ---------------------------------------------------------------------------


async def _call_request_asset(
    file_id: int, asset_type: str, slug: str | None, params: dict[str, Any] | None
) -> dict[str, Any]:
    arguments: dict[str, Any] = {"file_id": file_id, "asset_type": asset_type}
    if slug is not None:
        arguments["slug"] = slug
    if params:
        arguments["params"] = params
    return await _mcp_call("request_asset", arguments)


async def _download_to(
    url: str, bearer: str | None, dst: Path, max_bytes: int = _MAX_BYTES
) -> int:
    headers: dict[str, str] = {}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    written = 0
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(connect=10.0, read=120.0, write=120.0, pool=10.0),
        follow_redirects=True,
        verify=False,
    ) as client:
        async with client.stream("GET", url, headers=headers) as resp:
            resp.raise_for_status()
            with dst.open("wb") as fp:
                async for chunk in resp.aiter_bytes(chunk_size=_CHUNK):
                    fp.write(chunk)
                    written += len(chunk)
                    if written > max_bytes:
                        raise RuntimeError(
                            f"download exceeded {max_bytes} bytes; aborted"
                        )
    return written


def _derive_filename(url: str, fallback: str) -> str:
    tail = url.rsplit("/", 1)[-1].split("?", 1)[0]
    if "." in tail and len(tail) < 200:
        return tail
    return fallback


def _suffix_for(asset: str) -> str:
    return {
        "cad_mesh": ".glb",
        "cad_projection": ".png",
        "original": "",
    }.get(asset, "")


# ---------------------------------------------------------------------------
# Main resolver
# ---------------------------------------------------------------------------


async def resolve(ref: refs.Ref) -> Path:
    """Materialise ``ref`` into a snapshot dir and return the local path.

    ``ref.authority`` is the VRE folder display_name; ``ref.path`` is the
    relative path within that folder. We look up ``file_id`` from the live
    index — no integer IDs in the ref itself.

    Returned path:
      • Single-file assets (``original``, single-variant) → the file path.
      • Multi-variant ``cad_projection`` (no ``&export=``) → snapshot dir.
    """
    display_name = ref.authority
    rel_path = ref.path
    asset = ref.get("asset", "original") or "original"
    slug = ref.get("slug")
    export = ref.get("export")

    if not rel_path:
        raise RuntimeError(
            f"vre ref has no file path (authority={display_name!r}): {ref.canonical}"
        )

    # Disk-based dedup: reuse an existing snapshot if it was resolved from
    # the same canonical ref (survives process restarts).
    python_storage._ensure_root()  # noqa: SLF001
    for snap_dir in sorted(python_storage.STORAGE_ROOT.glob("snapshot_*"), reverse=True):
        meta_path = snap_dir / "meta.json"
        if not meta_path.exists():
            continue
        try:
            existing = json.loads(meta_path.read_text())
            if existing.get("origin", {}).get("ref") == ref.canonical:
                stored = existing.get("stored_name")
                candidate = snap_dir / stored if stored else snap_dir
                if candidate.exists():
                    _logger.info(
                        "vre resolve: reusing existing snapshot %s for %r",
                        snap_dir.name, ref.canonical,
                    )
                    return candidate
        except Exception:
            pass

    # Step 1+2: name → id → file_id
    await _resolve_folder_id(display_name)   # warms cache + validates folder
    file_id = await _resolve_file_id(display_name, rel_path)

    _logger.info(
        "vre resolve: folder=%r path=%r → file_id=%d asset=%r slug=%r export=%r",
        display_name, rel_path, file_id, asset, slug, export,
    )

    # Step 3: request signed URLs
    payload = await _call_request_asset(file_id, asset, slug, None)
    urls = payload.get("urls")
    if not isinstance(urls, dict) or not urls:
        raise RuntimeError(
            f"request_asset({asset}) returned no urls: {payload!r}"
        )

    # Step 4: stream into snapshot
    handle = f"py_{secrets.token_hex(4)}"
    snap_dir = python_storage.STORAGE_ROOT / f"snapshot_{handle}"
    snap_dir.mkdir()

    leaf_name = PurePosixPath(rel_path).name
    fname_base = f"vre_{leaf_name}_{asset}"
    if slug:
        fname_base += "_" + slug.replace("/", "-")
    if export:
        fname_base += "_" + export

    files: list[dict] = []
    total_bytes = 0
    multi = len(urls) > 1

    leaf_stem = PurePosixPath(rel_path).stem

    if multi:
        for variant, url in urls.items():
            if export and variant != export:
                continue
            # Use readable names: "<leaf>_<variant>.png" not JWT tokens
            fname = f"{leaf_stem}_{variant}.png"
            n = await _download_to(url, None, snap_dir / fname)
            files.append({"name": fname, "bytes": n, "variant": variant})
            total_bytes += n
    else:
        (variant, url), = urls.items()
        # Prefer the original filename + suffix; fall back to URL only if
        # it carries a genuine extension (not a JWT token like "eyJ...")
        url_tail = url.rsplit("/", 1)[-1].split("?", 1)[0]
        url_has_ext = "." in url_tail and len(url_tail) < 120 and not url_tail.startswith("ey")
        fname = url_tail if url_has_ext else (leaf_stem + _suffix_for(asset))
        if not fname or fname == "meta.json":
            fname = fname_base + _suffix_for(asset)
        n = await _download_to(url, None, snap_dir / fname)
        files.append({"name": fname, "bytes": n, "variant": variant})
        total_bytes += n

    meta = {
        "handle": handle,
        "kind": "vre_asset",
        "label": f"{display_name}/{rel_path}",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "files": files,
        "stored_name": files[0]["name"] if len(files) == 1 else None,
        "bytes": total_bytes,
        "origin": python_storage.make_origin(
            source="vre",
            account=None,
            path=f"{display_name}/{rel_path}",
            file_id=str(file_id),
            host=None,
            url=None,
            extra={"asset": asset, "slug": slug, "export": export},
        ) | {"ref": ref.canonical},
    }
    (snap_dir / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False))

    if not multi or export:
        return snap_dir / files[0]["name"]
    return snap_dir


# Side-effect: register on import.
_ensure_local.register("vre", resolve)
