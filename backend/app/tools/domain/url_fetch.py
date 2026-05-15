"""``fetch_to_python_storage`` — download a URL into python_storage.

One tool, one job: GET a URL, write the bytes to ``python_storage``,
return a handle the LLM can pass into ``run_compute`` /
``ctx.snapshot(handle)``. No interception of other tools, no
auto-rewriting of result envelopes — the LLM explicitly calls this
when it has a URL it wants to ingest.

Typical chain (with a rag-enterprise asset URL):

    asset = vre_request_asset(file_id=14, asset_type="original")
    url   = asset["urls"]["file"]
    snap  = fetch_to_python_storage(url=url, name="report.pdf")
    run_compute(code=f"rec = ctx.snapshot({snap['handle']!r}); ...")

The signed URL carries its own credential (HMAC-signed token), so
this tool sends no Authorization header. Same scope as ``web_fetch``
— gated by the user's ``web_fetch`` Settings toggle, follows
redirects, TLS-verifies. The difference is that web_fetch returns
*text* the LLM reads directly, while this returns a *handle* the
LLM passes to a compute script.
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import Any

import httpx

from app.services import python_storage
from app.services.user_settings import web_fetch_enabled
from app.tools.registry import ToolCtx, ToolSpec, registry

_logger = logging.getLogger(__name__)

# Bound the wall-clock of a single fetch. Indexed PDFs are typically
# O(MB) and download in seconds over a fast connection; 5 minutes is
# the upper bound for a slow link on a large file.
_TIMEOUT_S = 300.0

# Hard cap on per-call body size. A runaway URL shouldn't fill the
# user's disk. 1 GB is generous — voitta-rag-enterprise indexes top
# out well below this.
_MAX_BYTES = 1024 * 1024 * 1024

# Stream chunk for the tempfile write. Big enough to hide syscall
# overhead; small enough that we trip the size cap promptly.
_CHUNK = 64 * 1024


async def _fetch_to_python_storage(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    """Tool handler. Validates input, streams the URL to a tempfile,
    hands off to :func:`python_storage.put_file` which moves it into
    a snapshot dir with ``meta.json``.

    Returns the same envelope shape as the Drive download tools so
    downstream callers (``run_compute``, ``list_python_storage``) see
    a uniform schema regardless of where the file came from.
    """
    url = args.get("url")
    if not isinstance(url, str) or not url.strip():
        return {"ok": False, "error": "missing_url", "message": "url is required"}
    url = url.strip()

    # Scope: HTTPS by default, allow HTTP only for localhost loopback
    # (so the local rag-enterprise dev server works without TLS).
    if not (
        url.startswith("https://")
        or url.startswith("http://127.0.0.1")
        or url.startswith("http://localhost")
    ):
        return {
            "ok": False,
            "error": "unsupported_scheme",
            "message": (
                "fetch_to_python_storage requires https:// (or http:// "
                "for localhost). Got: " + url[:80]
            ),
        }

    if not web_fetch_enabled():
        return {
            "ok": False,
            "error": "web_fetch_disabled",
            "message": (
                "Outbound HTTP from the backend is disabled in Settings "
                "(see the 'Web retrieval' toggle). Enable it to use "
                "fetch_to_python_storage."
            ),
        }

    name_override = args.get("name")
    if name_override is not None and not isinstance(name_override, str):
        return {
            "ok": False,
            "error": "bad_name",
            "message": "name must be a string if provided",
        }
    kind = args.get("kind") or "downloaded_file"
    if not isinstance(kind, str) or not kind.strip():
        kind = "downloaded_file"

    # Stage to a temp file streamed from the response body. Avoids
    # buffering the whole response in memory — a 100 MB PDF would
    # otherwise sit on the heap until put_file moves it.
    fd, tmp_path_str = tempfile.mkstemp(prefix="fetch_url_")
    os.close(fd)
    tmp_path = Path(tmp_path_str)

    bytes_written = 0
    mime_type = "application/octet-stream"
    derived_name: str | None = None
    try:
        async with httpx.AsyncClient(
            timeout=_TIMEOUT_S,
            follow_redirects=True,
        ) as http:
            async with http.stream("GET", url) as resp:
                resp.raise_for_status()
                mime_type = (
                    resp.headers.get("content-type", "")
                    .split(";", 1)[0]
                    .strip()
                    or mime_type
                )
                # Best-effort filename guess from the URL path. The
                # signed URLs from rag-enterprise end in a token (no
                # extension); the LLM should pass an explicit ``name``
                # when it knows the real filename (it usually does —
                # vre_search returned file_path).
                derived_name = url.rsplit("/", 1)[-1].split("?", 1)[0] or None
                with tmp_path.open("wb") as fp:
                    async for chunk in resp.aiter_bytes(chunk_size=_CHUNK):
                        fp.write(chunk)
                        bytes_written += len(chunk)
                        if bytes_written > _MAX_BYTES:
                            raise ValueError(
                                f"download exceeded {_MAX_BYTES} bytes; aborted"
                            )
    except Exception as exc:
        # Clean up the temp file we never handed to put_file.
        try:
            tmp_path.unlink()
        except OSError:
            pass
        _logger.warning("fetch_to_python_storage failed url=%s err=%s", url[:120], exc)
        return {
            "ok": False,
            "error": "fetch_failed",
            "message": str(exc),
            "url": url,
        }

    original_name = (
        name_override.strip()
        if isinstance(name_override, str) and name_override.strip()
        else (derived_name or "downloaded_file")
    )

    snap = python_storage.put_file(
        src_path=tmp_path,
        original_name=original_name,
        kind=kind,
        meta={
            "origin": python_storage.make_origin(
                source="fetch_to_python_storage",
                account=None,
                path=None,
                file_id=None,
                host=None,
                url=url,
                extra={"mime_type": mime_type},
            ),
            "mime_type": mime_type,
        },
        move=True,
    )
    return {
        "ok": True,
        "handle": snap["handle"],
        "stored_name": snap["meta"].get("stored_name"),
        "path": snap["path"],
        "bytes": bytes_written,
        "mime_type": mime_type,
        "url": url,
    }


registry.register(
    ToolSpec(
        name="fetch_to_python_storage",
        description=(
            "Download an HTTP(S) URL into python_storage and return a "
            "handle. **Use this whenever you'd be tempted to inline a "
            "large file into context** — the bytes go on disk, your "
            "context window stays clean, and a `run_compute` script can "
            "read the file with the right Python library (pandas, "
            "pypdf, openpyxl, custom parser).\n"
            "\n"
            "Canonical decision rule:\n"
            "  • If you need to *read prose* from a small text source — "
            "use `web_fetch` (HTML/JSON/text) or `vre_get_file` /"
            "`vre_get_chunk_range` (indexed markdown).\n"
            "  • If you need to *process a file* — PDF you'll parse, "
            "spreadsheet you'll query, dataset you'll iterate, anything "
            "that's larger than ~50 KB or that you'd analyse with a "
            "Python library — use this tool, then `run_compute`.\n"
            "\n"
            "Typical chains:\n"
            "  Enterprise file → Python:\n"
            "    asset = vre_request_asset(file_id, 'original')\n"
            "    snap  = fetch_to_python_storage(\n"
            "                url=asset['urls']['file'],\n"
            "                name=file_path,         # from vre_search\n"
            "            )\n"
            "    run_compute(code=f\"rec = ctx.snapshot({snap['handle']!r}); ...\")\n"
            "\n"
            "  Open-web file → Python:\n"
            "    snap = fetch_to_python_storage(\n"
            "               url='https://example.com/data.csv',\n"
            "               name='data.csv',\n"
            "           )\n"
            "    run_compute(code=...)\n"
            "\n"
            "Behaviour:\n"
            "  • No auth headers added — the URL is the credential. "
            "Signed URLs from `vre_request_asset` carry their own HMAC; "
            "same is true for AWS pre-signed URLs, GitHub raw URLs with "
            "embedded tokens, etc.\n"
            "  • Redirects followed, TLS verified.\n"
            "  • https:// always; http:// allowed only for localhost.\n"
            "  • `name` is optional but recommended: pass the real "
            "source filename so the snapshot's meta.json reflects the "
            "canonical name and `run_compute` scripts can rely on the "
            "extension being right.\n"
            "  • `kind` defaults to 'downloaded_file'; override to group "
            "in list_python_storage (e.g. 'drive_file', 'mcp_asset').\n"
            "  • Max 1 GB per call; 5-minute timeout.\n"
            "\n"
            "Gated by the 'Web retrieval' Settings toggle (same as "
            "web_fetch). When disabled, returns `{ok: false, "
            "error: 'web_fetch_disabled'}`."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": (
                        "HTTPS URL to fetch (or http://localhost for local dev)."
                    ),
                },
                "name": {
                    "type": "string",
                    "description": (
                        "Filename to store the bytes under. Optional — "
                        "defaults to the basename of the URL path. Pass "
                        "the real source filename when you know it."
                    ),
                },
                "kind": {
                    "type": "string",
                    "description": (
                        "Tag for list_python_storage / dedup. Default: "
                        "'downloaded_file'."
                    ),
                },
            },
            "required": ["url"],
            "additionalProperties": False,
        },
        handler=_fetch_to_python_storage,
        side="server",
    )
)
