"""HTML renderer — the only renderer.

The script's ``build(ctx)`` returns a string. That string IS the
report body. The renderer:

  1. Asserts the value is a non-empty string.
  2. Synthesises ``<!doctype html>`` + ``<html>`` + ``<head>`` /
     ``<body>`` if missing.
  3. Injects the screenshot shim (``_html_to_image.js`` + the
     measure/reflow/screenshot ``_panel_shim.js``) into ``<head>``,
     plus ``<meta>`` tags carrying slug + render_id.
  4. Caches the assembled document keyed by ``(slug, render_id)``.

The FE iframe mounts ``/api/html-report?id=<slug>&render_id=<rid>``
which reads from the cache. Same-origin, so the shim attaches.
"""

from __future__ import annotations

import logging
import re
import threading
from collections import OrderedDict

from app.reports.schemas import HtmlPayload

logger = logging.getLogger(__name__)

_CACHE_LIMIT = 64
_cache: "OrderedDict[tuple[str, str], str]" = OrderedDict()
_cache_lock = threading.Lock()


def _shim_script_tags() -> str:
    return (
        '<script src="/api/_html_to_image.js"></script>\n'
        '<script src="/api/_panel_shim.js"></script>'
    )


_HEAD_OPEN_RE = re.compile(r"<head(\s[^>]*)?>", re.IGNORECASE)
_HEAD_CLOSE_RE = re.compile(r"</head\s*>", re.IGNORECASE)
_HTML_OPEN_RE = re.compile(r"<html(\s[^>]*)?>", re.IGNORECASE)
_DOCTYPE_RE = re.compile(r"<!doctype\s+[^>]+>", re.IGNORECASE)


def _inject_shim(html: str, *, slug: str, render_id: str) -> str:
    """Inject the screenshot shim into ``<head>``, synthesising parts
    of the document scaffold if missing."""
    shim_with_meta = (
        f'<meta name="voitta-slug" content="{slug}">\n'
        f'<meta name="voitta-render-id" content="{render_id}">\n'
        '<link rel="icon" type="image/svg+xml" href="/favicon.svg">\n'
        + _shim_script_tags()
    )
    has_html = bool(_HTML_OPEN_RE.search(html))
    has_head_open = bool(_HEAD_OPEN_RE.search(html))
    has_head_close = bool(_HEAD_CLOSE_RE.search(html))

    if has_head_close:
        return _HEAD_CLOSE_RE.sub(
            shim_with_meta + "\n</head>", html, count=1,
        )
    if has_head_open:
        return _HEAD_OPEN_RE.sub(
            lambda m: m.group(0) + "\n" + shim_with_meta + "\n</head>",
            html, count=1,
        )
    if has_html:
        return _HTML_OPEN_RE.sub(
            lambda m: m.group(0) + f"\n<head>\n{shim_with_meta}\n</head>",
            html, count=1,
        )
    # Fragment / pure text — wrap in a full scaffold.
    return (
        "<!doctype html>\n<html>\n<head>\n"
        f"{shim_with_meta}\n</head>\n<body>\n{html}\n</body>\n</html>"
    )


def _ensure_doctype(html: str) -> str:
    if _DOCTYPE_RE.search(html):
        return html
    return "<!doctype html>\n" + html


def render_html(value: object, *, slug: str, render_id: str) -> HtmlPayload:
    """Accept a raw HTML string, assemble + cache, return the iframe URL."""
    if not isinstance(value, str):
        raise TypeError(
            f"build() must return a string (HTML). Got {type(value).__name__}."
        )
    if not value.strip():
        raise ValueError("build() returned an empty string")

    assembled = _inject_shim(value, slug=slug, render_id=render_id)
    assembled = _ensure_doctype(assembled)

    key = (slug, render_id)
    with _cache_lock:
        _cache[key] = assembled
        _cache.move_to_end(key)
        while len(_cache) > _CACHE_LIMIT:
            _cache.popitem(last=False)

    return HtmlPayload(
        url=f"/api/html-report?id={slug}&render_id={render_id}",
        title=None,
    )


def get_cached(slug: str, render_id: str) -> str | None:
    with _cache_lock:
        return _cache.get((slug, render_id))
