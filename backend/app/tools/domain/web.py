"""Web retrieval tool — GET an arbitrary URL and return readable text.

Server-side (no browser primitive — we want the request to look like a
plain Chrome navigation against the remote, not be cross-origin'd from
whatever page the bookmarklet happens to be running on). Best-effort
stealth, deliberately stopping short of a headless browser:

  • Realistic Chrome User-Agent + matching ``Sec-Ch-Ua*`` client hints,
    Accept / Accept-Language, ``Sec-Fetch-*`` set as for a top-level
    navigation, ``Upgrade-Insecure-Requests``.
  • Per-process cookie jar shared across calls, so a server that drops
    a session cookie on first visit sees the same session on the next.
  • Tiny inter-request pacing per host (200-500 ms) when calls land in
    the same burst, so we don't look like a scraper hammering the host.
  • TLS verification stays ON. We don't fingerprint-spoof, don't proxy,
    don't run JS, don't solve CAPTCHAs — sites that demand any of those
    will refuse and we surface the status as-is.

Output is text-only. Supported content types:

  • ``text/html``           → boilerplate stripped (no script/style/nav
                              chrome) into paragraph-flow text + title.
  • ``text/*`` / xml         → returned as-is (re-decoded if needed).
  • ``application/json``     → pretty-printed JSON inline.
  • ``application/pdf``      → text extracted via ``pypdf`` (no layout).
  • anything else (images,
    archives, …)             → refused with ``error: "binary_content"``.

Gated by ``user_settings.web_fetch_enabled()`` (defaults True). Toggle
in the Settings panel.
"""

from __future__ import annotations

import asyncio
import io
import json as _json
import random
import re
import time
from html import unescape
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlparse

import httpx

from app.services.user_settings import web_fetch_enabled
from app.tools.registry import ToolCtx, ToolSpec, registry


# ---- look-legit headers ---------------------------------------------------

# Pinned to a recent stable Chrome on macOS. Update the version trio
# (UA + Sec-Ch-Ua + Sec-Ch-Ua-Platform) together when bumping; mismatched
# values are themselves a fingerprint.
_CHROME_MAJOR = "131"
_BROWSER_HEADERS: dict[str, str] = {
    "User-Agent": (
        f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        f"AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{_CHROME_MAJOR}.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8,"
        "application/signed-exchange;v=b3;q=0.7"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    # Only advertise encodings httpx can decode without extra packages.
    # Real Chrome also sends br/zstd; servers don't seem to balk if those
    # are absent, and advertising them would mean failed decodes when the
    # server takes us up on it.
    "Accept-Encoding": "gzip, deflate",
    "Sec-Ch-Ua": (
        f'"Google Chrome";v="{_CHROME_MAJOR}", '
        f'"Chromium";v="{_CHROME_MAJOR}", '
        '"Not_A Brand";v="24"'
    ),
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"macOS"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "Cache-Control": "max-age=0",
}

# Process-scoped cookie jar — shared across all web_fetch calls so a site
# that issues a session cookie on first visit sees us as the same client.
_JAR = httpx.Cookies()

# Per-host last-call timestamp for inter-call pacing (see ``_maybe_pace``).
_LAST_CALL: dict[str, float] = {}

# How fresh a "burst" is. If we hit a host within this window of the
# previous call, sleep a small jittered delay before the next request.
_BURST_WINDOW_S = 1.5

# Default raw-byte cap. Generous — the LLM-facing truncation kicks in
# later on the *post-strip* text, not the raw bytes.
_DEFAULT_MAX_BYTES = 2_000_000

# Cap on the post-strip text we hand back inline. Avoids one giant page
# blowing the chat context. The LLM gets ``truncated: true`` when this
# fires so it knows the page goes on.
_INLINE_TEXT_CAP = 200_000


async def _maybe_pace(host: str) -> None:
    now = time.monotonic()
    last = _LAST_CALL.get(host, 0.0)
    if now - last < _BURST_WINDOW_S:
        await asyncio.sleep(0.2 + random.random() * 0.3)
    _LAST_CALL[host] = time.monotonic()


# ---- HTML → readable text -------------------------------------------------

# Tags whose subtree we want gone — chrome / scripts / styles / hidden bits.
_SKIP_SUBTREE = {
    "script", "style", "noscript", "svg", "head",
    "iframe", "template", "form", "button", "select", "textarea",
}

# Tags that should produce a paragraph break in the flattened text.
_BLOCK_TAGS = {
    "p", "div", "br", "li", "tr", "section", "article", "header", "footer",
    "aside", "nav", "main", "blockquote", "pre",
    "h1", "h2", "h3", "h4", "h5", "h6", "hr",
    "ul", "ol", "table",
}


class _HtmlToText(HTMLParser):
    """Minimal HTML → plain-text flattener. Skips script/style/etc and
    inserts paragraph breaks at block-level tags. Captures ``<title>``
    separately so we can return it as a top-level field."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title_parts: list[str] = []
        self.skip_depth = 0
        self.in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            # title sits inside <head> (which is in _SKIP_SUBTREE); we capture
            # title text separately via in_title rather than emitting it.
            self.in_title = True
            return
        if tag in _SKIP_SUBTREE:
            self.skip_depth += 1
            return
        if self.skip_depth:
            return
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")
        if tag == "li":
            self.parts.append("• ")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self.in_title = False
            return
        if tag in _SKIP_SUBTREE:
            if self.skip_depth:
                self.skip_depth -= 1
            return
        if self.skip_depth:
            return
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title_parts.append(data)
            return
        if self.skip_depth:
            return
        self.parts.append(data)


def _html_to_text(html: str) -> tuple[str | None, str]:
    # Drop comments first — HTMLParser otherwise routes their contents
    # through handle_data when they sit inside a non-skip subtree.
    html = re.sub(r"<!--.*?-->", "", html, flags=re.DOTALL)
    parser = _HtmlToText()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        # Malformed HTML — return whatever we got so far.
        pass
    title = unescape("".join(parser.title_parts)).strip() or None
    text = unescape("".join(parser.parts))
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return title, text.strip()


def _pdf_to_text(data: bytes) -> tuple[str, int]:
    """Return ``(text, num_pages)``. ``pypdf`` is pure-Python and
    layout-naive — fine for "tell me what's in this PDF" but won't
    preserve column / table structure."""
    try:
        import pypdf
    except ImportError as exc:  # pragma: no cover — surfaced as tool error
        raise RuntimeError(
            "pypdf not installed; run `pip install pypdf` in the backend venv"
        ) from exc
    reader = pypdf.PdfReader(io.BytesIO(data))
    pages: list[str] = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception:
            pages.append("")
    return "\n\n".join(pages).strip(), len(reader.pages)


def _decode_text(data: bytes, content_type: str) -> str:
    # Pick charset from Content-Type if present; otherwise let Python
    # try utf-8 and fall back to latin-1 (always succeeds, never raises).
    charset = "utf-8"
    m = re.search(r"charset=([^;\s]+)", content_type, flags=re.IGNORECASE)
    if m:
        charset = m.group(1).strip().strip("\"'")
    try:
        return data.decode(charset, errors="replace")
    except LookupError:
        return data.decode("utf-8", errors="replace")


def _truncate(text: str, cap: int) -> tuple[str, bool]:
    if len(text) <= cap:
        return text, False
    return text[:cap], True


# ---- handler --------------------------------------------------------------


async def _web_fetch(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    url = (args.get("url") or "").strip()
    if not url:
        return {"ok": False, "error": "invalid_args", "message": "url required"}

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return {
            "ok": False,
            "error": "invalid_url",
            "message": "url must be an absolute http(s) URL",
            "url": url,
        }

    max_bytes = int(args.get("max_bytes") or _DEFAULT_MAX_BYTES)
    max_bytes = max(1024, min(5_000_000, max_bytes))

    started = time.time()
    await _maybe_pace(parsed.netloc)

    timeout = httpx.Timeout(30.0, connect=10.0)
    redirects: list[str] = []
    try:
        async with httpx.AsyncClient(
            cookies=_JAR,
            follow_redirects=True,
            max_redirects=5,
            timeout=timeout,
            headers=_BROWSER_HEADERS,
            # TLS verification stays on. Bookmarklet user can't easily
            # ship a custom CA bundle, and turning it off would silently
            # downgrade everyone's security.
            verify=True,
        ) as client:
            async with client.stream("GET", url) as r:
                for hist in r.history:
                    redirects.append(str(hist.url))
                final_url = str(r.url)
                status = r.status_code
                content_type = r.headers.get("content-type", "")
                chunks: list[bytes] = []
                total = 0
                truncated_bytes = False
                async for chunk in r.aiter_bytes(chunk_size=64 * 1024):
                    if total + len(chunk) > max_bytes:
                        chunks.append(chunk[: max_bytes - total])
                        total = max_bytes
                        truncated_bytes = True
                        break
                    chunks.append(chunk)
                    total += len(chunk)
                body = b"".join(chunks)
            # httpx copies the cookies arg into its own jar on client
            # init rather than holding the reference, so mutations
            # don't propagate. Sync the post-call cookies back into our
            # process-scoped jar so the next call sees them.
            _JAR.update(client.cookies)
    except httpx.TimeoutException as exc:
        return {
            "ok": False,
            "error": "timeout",
            "message": str(exc),
            "url": url,
        }
    except httpx.TooManyRedirects as exc:
        return {
            "ok": False,
            "error": "too_many_redirects",
            "message": str(exc),
            "url": url,
        }
    except httpx.RequestError as exc:
        return {
            "ok": False,
            "error": "request_error",
            "message": str(exc),
            "url": url,
        }

    elapsed_s = round(time.time() - started, 2)
    base: dict[str, Any] = {
        "url": final_url,
        "status": status,
        "content_type": content_type,
        "bytes": total,
        "truncated_bytes": truncated_bytes,
        "elapsed_s": elapsed_s,
    }
    if redirects:
        base["redirects"] = redirects

    if status >= 400:
        # Surface the body text (small) so the LLM can read e.g. an error
        # page, but flag ok=False.
        try:
            preview = _decode_text(body[:4096], content_type)
        except Exception:
            preview = ""
        return {
            **base,
            "ok": False,
            "error": "http_error",
            "preview": preview.strip()[:2000],
        }

    ct_lower = content_type.lower()

    # JSON
    if "application/json" in ct_lower or ct_lower.startswith("application/") and "+json" in ct_lower:
        text = _decode_text(body, content_type)
        try:
            parsed_json = _json.loads(text)
            pretty = _json.dumps(parsed_json, indent=2, ensure_ascii=False)
        except Exception:
            pretty = text  # fall through with raw text
        content, truncated_text = _truncate(pretty, _INLINE_TEXT_CAP)
        return {
            **base,
            "ok": True,
            "kind": "json",
            "content": content,
            "truncated": truncated_text,
        }

    # PDF
    if "application/pdf" in ct_lower:
        try:
            text, num_pages = _pdf_to_text(body)
        except Exception as exc:
            return {
                **base,
                "ok": False,
                "error": "pdf_extract_failed",
                "message": str(exc),
            }
        content, truncated_text = _truncate(text, _INLINE_TEXT_CAP)
        return {
            **base,
            "ok": True,
            "kind": "pdf",
            "pages": num_pages,
            "content": content,
            "truncated": truncated_text,
        }

    # HTML
    if "text/html" in ct_lower or "application/xhtml" in ct_lower:
        text = _decode_text(body, content_type)
        title, stripped = _html_to_text(text)
        content, truncated_text = _truncate(stripped, _INLINE_TEXT_CAP)
        return {
            **base,
            "ok": True,
            "kind": "html",
            "title": title,
            "content": content,
            "truncated": truncated_text,
        }

    # Other textual types (text/plain, text/csv, application/xml, …)
    if ct_lower.startswith("text/") or "xml" in ct_lower or "javascript" in ct_lower:
        text = _decode_text(body, content_type)
        content, truncated_text = _truncate(text, _INLINE_TEXT_CAP)
        return {
            **base,
            "ok": True,
            "kind": "text",
            "content": content,
            "truncated": truncated_text,
        }

    # Anything else is binary — refuse with a hint.
    return {
        **base,
        "ok": False,
        "error": "binary_content",
        "message": (
            f"Refusing to inline binary content-type {content_type!r}. "
            "web_fetch only returns readable text (HTML, plain text, "
            "JSON, PDF). For images / archives / other binaries, ask "
            "the user — there's no download-to-storage variant yet."
        ),
    }


registry.register(
    ToolSpec(
        name="web_fetch",
        description=(
            "Fetch a single URL by HTTP GET and return its readable text. "
            "Use for pulling articles, docs, JSON APIs, or PDFs from the "
            "open web.\n"
            "\n"
            "Returns text only — boilerplate-stripped from HTML (so you "
            "get article body + page title, not nav/footer/ads), pretty-"
            "printed for JSON, page-extracted for PDF. Other binaries "
            "(images, archives) are refused.\n"
            "\n"
            "Redirects are followed (up to 5). The request looks like a "
            "regular Chrome navigation (real User-Agent, client hints, "
            "Accept-Language, persistent cookies across calls). TLS "
            "verification stays on; sites that block non-browser clients "
            "or require JavaScript will return their challenge page and "
            "you'll see it in `content`.\n"
            "\n"
            "Response shape:\n"
            "  ok=true: {url, status, content_type, kind, content, "
            "title?, pages?, bytes, truncated, elapsed_s, redirects?}\n"
            "  ok=false: {error, message, status?, url, ...}\n"
            "\n"
            "`kind` is one of: html | json | text | pdf. `content` is "
            "capped at ~200 KB; `truncated: true` means the page goes on "
            "past what you got — narrow the URL, or accept a partial "
            "read."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "Absolute http(s) URL to fetch.",
                },
                "max_bytes": {
                    "type": "integer",
                    "minimum": 1024,
                    "maximum": 5_000_000,
                    "default": _DEFAULT_MAX_BYTES,
                    "description": (
                        "Cap on raw response bytes downloaded. Default "
                        "2 MB. Stripped/extracted text is further capped "
                        "at ~200 KB before being returned inline."
                    ),
                },
            },
            "required": ["url"],
            "additionalProperties": False,
        },
        handler=_web_fetch,
        side="server",
        visibility_check=web_fetch_enabled,
    )
)
