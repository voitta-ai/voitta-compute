"""``vre://`` resolver — owned by the voitta-enterprise plugin.

Refs reach this resolver as already-parsed :class:`refs.Ref` objects.
The canonical key set:

  • ``file_id``  — required, integer.
  • ``asset``    — required (e.g. ``original``, ``cad_mesh``,
                   ``cad_projection``).
  • ``slug``     — required for per-component asset variants.
  • ``export``   — for ``cad_projection``, picks one view; without it
                   we materialise a directory of all variants.

Two-step flow on cache miss:

  1. Call the remote MCP tool ``request_asset(file_id, asset_type,
     slug=…)`` via the VRE connector. The response carries either a
     single URL (``original``, whole-file ``cad_mesh``, per-slug
     ``cad_mesh``) or a dict of named URLs (``cad_projection`` →
     front/top/side/iso).
  2. Stream each URL into a fresh snapshot dir under
     ``python_storage/cache/snapshot_<handle>/``. ``meta.json::origin``
     is stamped with the canonical ``ref`` so future lookups hit.

Auth + connector resolution:
  The VRE connector is registered by the ``voitta-enterprise`` plugin
  (``plugin_name=voitta-enterprise``, ``connector_id=vre``). We pull
  its URL + bearer token via the same helpers the synthesised
  ``vre_request_asset`` tool uses — so a manual call and an
  ensure_local hit go over the exact same wire path.
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from pathlib import Path
from typing import Any

import httpx

from app.services import ensure_local as _ensure_local, python_storage, refs
from app.services.mcp import client as mcp_client
from app.services.mcp import registry as mcp_registry

_logger = logging.getLogger(__name__)

# Single-component refs map to a single-file snapshot. Multi-component
# refs (cad_projection without &export=…) map to a directory snapshot
# containing one file per variant. The chunk size + max bytes mirror
# fetch_to_python_storage so a misbehaving signed URL can't fill the
# disk.
_CHUNK = 64 * 1024
_MAX_BYTES = 512 * 1024 * 1024  # 512 MiB — same as fetch_to_python_storage


_VRE_CONNECTOR_KEY = "voitta-enterprise:vre"


def _connector_endpoint() -> tuple[str, str | None]:
    """Resolve the (url, bearer) for the VRE connector.

    Raises if the plugin isn't loaded or its URL setting hasn't been
    configured. The bearer is optional (single-user / dev modes).
    """
    plugin, _, cid = _VRE_CONNECTOR_KEY.partition(":")
    conn = mcp_registry.get_connector(plugin, cid)
    if conn is None:
        raise RuntimeError(
            f"VRE MCP connector not registered "
            f"(plugin {plugin!r}, id {cid!r}) — is the voitta-enterprise "
            "plugin loaded?"
        )
    # Reuse the private read helpers from the registry. They handle
    # the dotted settings path, missing fields, and the dev-mode empty
    # token case in one place — duplicating the logic here would drift.
    url = mcp_registry._read_url(conn.decl)        # noqa: SLF001
    if not url:
        raise RuntimeError(
            "VRE MCP url not configured — fill in the Server URL field "
            "under Settings → voitta-enterprise."
        )
    token = mcp_registry._read_token(conn.decl)    # noqa: SLF001
    return url, token


async def _call_request_asset(
    file_id: int, asset_type: str, slug: str | None, params: dict[str, Any] | None
) -> dict[str, Any]:
    """Invoke the remote ``request_asset`` tool and return its
    structured payload. Raises on transport/tool errors."""
    url, bearer = _connector_endpoint()
    arguments: dict[str, Any] = {"file_id": file_id, "asset_type": asset_type}
    if slug is not None:
        arguments["slug"] = slug
    if params:
        arguments["params"] = params
    result = await mcp_client.call_tool(url, bearer, "request_asset", arguments)
    if result.is_error:
        text = "; ".join(
            getattr(b, "text", "") for b in (result.content or []) if getattr(b, "text", "")
        )
        raise RuntimeError(f"request_asset error: {text or 'unknown'}")
    # fastmcp's CallToolResult has ``structured_content`` (wire dict)
    # plus ``data`` (parsed). We want the dict; ``data`` may contain
    # pydantic models that aren't trivially indexable.
    structured = getattr(result, "structured_content", None)
    if not isinstance(structured, dict):
        # Some fastmcp versions wrap the payload under a top-level
        # ``result`` key. Try that first; otherwise fall back to
        # parsing the concatenated text content.
        if isinstance(structured, dict) and "result" in structured:
            return structured["result"]  # type: ignore[index]
        # Fall back to text:
        text_blob = "\n".join(
            getattr(b, "text", "") for b in (result.content or []) if getattr(b, "text", "")
        )
        try:
            return json.loads(text_blob)
        except Exception as exc:
            raise RuntimeError(
                f"request_asset returned unparseable payload: {exc}"
            )
    return structured


async def _download_to(
    url: str, bearer: str | None, dst: Path, max_bytes: int = _MAX_BYTES
) -> int:
    """Stream ``url`` to ``dst`` with the same byte cap as
    fetch_to_python_storage. Returns the byte count.

    Signed URLs from VRE embed the credential, so no auth header is
    needed — ``bearer`` is reserved for future per-asset auth modes.
    """
    headers: dict[str, str] = {}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    written = 0
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(connect=10.0, read=120.0, write=120.0, pool=10.0),
        follow_redirects=True,
        verify=False,                # local dev: VRE often uses a self-signed cert
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
    """Pick a friendly filename for a signed URL.

    VRE's signed URLs end in an HMAC token (no extension), so URL
    parsing is mostly useless — we use the caller-supplied fallback
    (built from the canonical ref keys). The fallback always wins
    when the URL doesn't surface a recognisable suffix.
    """
    tail = url.rsplit("/", 1)[-1].split("?", 1)[0]
    if "." in tail and len(tail) < 200:
        return tail
    return fallback


async def resolve(ref: refs.Ref) -> Path:
    """Materialise ``ref`` into a fresh snapshot dir and return its path.

    Dispatch by ``asset``:

      • ``cad_projection`` without ``&export=`` → directory of four PNGs
        (one per view). Returned path IS the snapshot dir; the report
        picks ``front.png``/``iso.png`` from it.
      • ``cad_projection`` with ``&export=front`` → single PNG;
        returned path is the file.
      • ``cad_mesh`` (whole-file or per-slug) → single GLB.
      • ``original`` → single file (source bytes).

    For single-file modes the returned ``Path`` is the *file*, not its
    parent dir — that's what report scripts want (`pathlib.Path(p).
    read_bytes()` Just Works). Multi-variant directory mode returns
    the dir; the script enumerates its contents.
    """
    file_id_s = ref.get("file_id")
    asset = ref.get("asset")
    if not file_id_s or not asset:
        raise RuntimeError(f"vre ref missing file_id/asset: {ref.canonical}")
    try:
        file_id = int(file_id_s)
    except ValueError as exc:
        raise RuntimeError(f"vre file_id not an int: {file_id_s!r}") from exc
    slug = ref.get("slug")
    export = ref.get("export")  # cad_projection variant name, optional

    # ---- 1. Request asset, get URLs --------------------------------------
    params: dict[str, Any] = {}
    payload = await _call_request_asset(file_id, asset, slug, params or None)

    # Response: either {"inline": {...}} (rare; not what reports want
    # — they want bytes), or {"urls": {<variant>: <url>}}. We only
    # handle the URL flavour here.
    urls = payload.get("urls")
    if not isinstance(urls, dict) or not urls:
        raise RuntimeError(
            f"request_asset({asset}) returned no urls: {payload!r}"
        )

    # ---- 2. Stream URL(s) into a fresh snapshot --------------------------
    python_storage._ensure_root()  # noqa: SLF001
    handle = f"py_{secrets.token_hex(4)}"
    snap_dir = python_storage.STORAGE_ROOT / f"snapshot_{handle}"
    snap_dir.mkdir()

    # Build the friendly filename base from the canonical ref so files
    # are recognisable in the artefact browser ("vre_42_cad_mesh.glb"
    # is more useful than the signed-URL token).
    fname_base = f"vre_{file_id}_{asset}"
    if slug:
        fname_base += "_" + slug.replace("/", "-")
    if export:
        fname_base += "_" + export

    files: list[dict] = []
    total_bytes = 0

    multi = len(urls) > 1
    if multi:
        # Multi-variant (cad_projection without &export=…). One file
        # per variant; returned path IS the dir.
        for variant, url in urls.items():
            if export and variant != export:
                continue                # &export= acts as a filter
            ext = _derive_filename(url, "").rsplit(".", 1)[-1]
            ext = ext if (1 <= len(ext) <= 5 and ext.isalnum()) else "bin"
            fname = f"{variant}.{ext}"
            n = await _download_to(url, None, snap_dir / fname)
            files.append({"name": fname, "bytes": n, "variant": variant})
            total_bytes += n
    else:
        # Single-variant. Returned path is the file.
        (variant, url), = urls.items()
        fname = _derive_filename(url, fname_base + _suffix_for(asset))
        # Avoid clobbering meta.json
        if fname == "meta.json":
            fname = "file_" + fname
        n = await _download_to(url, None, snap_dir / fname)
        files.append({"name": fname, "bytes": n, "variant": variant})
        total_bytes += n

    # ---- 3. Write meta.json with canonical ref --------------------------
    meta = {
        "handle": handle,
        "kind": "vre_asset",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "files": files,
        "stored_name": files[0]["name"] if len(files) == 1 else None,
        "bytes": total_bytes,
        "origin": python_storage.make_origin(
            source="vre",
            account=None,
            path=None,
            file_id=str(file_id),
            host=None,
            url=None,
            extra={"asset": asset, "slug": slug, "export": export},
        ) | {"ref": ref.canonical},
    }
    (snap_dir / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False))

    if not multi or export:
        # Single-file: return the file path itself.
        return snap_dir / files[0]["name"]
    return snap_dir


def _suffix_for(asset: str) -> str:
    """Best-guess extension when the signed URL doesn't carry one."""
    return {
        "cad_mesh": ".glb",
        "cad_projection": ".png",
        "original": "",
    }.get(asset, "")


# Side-effect: register on import.
_ensure_local.register("vre", resolve)
