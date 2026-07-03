"""Render a cached HTML report to a text-based PDF with a pure-Python engine.

The report pane's *Download PDF* button hits ``/api/report/export-pdf``, which
calls :func:`render_report_pdf`. We convert the cached report HTML to PDF with
**xhtml2pdf** (ReportLab under the hood) — chosen deliberately:

* It is **pure-Python** (installs as wheels via the lazy installer, no compiler,
  no system libraries), so it works inside the frozen/notarized briefcase app.
  WeasyPrint would render richer CSS but depends on native pango/cairo/gobject
  libs that don't pip-install — the same "works in dev, fails in the bundle"
  trap that ruled out a headless-browser engine.
* The output is a **real text PDF** — text stays selectable and copy-pasteable
  (the whole point of the customer request), unlike an html2canvas screenshot.

Known limitation: xhtml2pdf does **not** execute JavaScript, so charts a report
draws client-side (Bokeh / Plotly / three.js) do not appear. Text, tables, CSS
(a practical subset), and ``<img>`` resources do. For chart-heavy reports the
in-app interactive view remains the source of truth; the PDF is for the textual
content.

Image resolution: ``data:`` URIs render inline; relative / loopback ``<img>``
srcs are fetched from the live backend over the plain-HTTP listener. External
URLs are left to the engine (and generally skipped) — we don't turn the export
into a server-side fetcher of arbitrary hosts.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import tempfile
from urllib.parse import urlparse

import httpx

from app.config import HOST, PLAINTEXT_PORT

logger = logging.getLogger(__name__)

_BASE = f"http://{HOST}:{PLAINTEXT_PORT}"


class ReportPdfError(RuntimeError):
    """Export failed — the message is safe to surface to the user."""


def _engine_available() -> bool:
    import importlib.util

    return importlib.util.find_spec("xhtml2pdf") is not None


def _html_to_pdf(html: str) -> bytes:
    """Convert an HTML string to PDF bytes (blocking; run in a thread).

    Resolves loopback/relative image srcs against the live backend via a
    ``link_callback``; cleans up any temp files it downloads.
    """
    from xhtml2pdf import pisa  # local import — installed lazily at first launch

    tmpfiles: list[str] = []

    def link_callback(uri: str, _rel: str) -> str:
        # data: URIs are handled natively by the engine.
        if uri.startswith("data:"):
            return uri
        if uri.startswith("/"):
            target = _BASE + uri
        elif uri.startswith(_BASE):
            target = uri
        else:
            # External / unknown scheme — don't fetch it server-side.
            return uri
        try:
            resp = httpx.get(target, timeout=15.0)
            resp.raise_for_status()
            suffix = os.path.splitext(urlparse(target).path)[1] or ".bin"
            fd, path = tempfile.mkstemp(suffix=suffix)
            with os.fdopen(fd, "wb") as fh:
                fh.write(resp.content)
            tmpfiles.append(path)
            return path
        except Exception:  # noqa: BLE001 — a missing image shouldn't kill the export
            logger.warning("report_pdf: could not resolve image %s", uri)
            return uri

    out = io.BytesIO()
    try:
        status = pisa.CreatePDF(src=html, dest=out, encoding="utf-8", link_callback=link_callback)
    finally:
        for path in tmpfiles:
            try:
                os.unlink(path)
            except OSError:
                pass

    if status.err:
        raise ReportPdfError(
            "PDF conversion failed — the report's HTML/CSS may use features the "
            "export engine can't handle."
        )
    return out.getvalue()


async def render_report_pdf(slug: str, render_id: str) -> bytes:
    """Return PDF bytes for the cached report ``(slug, render_id)``.

    Raises :class:`ReportPdfError` (user-facing message) if the engine isn't
    installed yet, the report is no longer cached, or conversion fails.
    """
    if not _engine_available():
        raise ReportPdfError(
            "The PDF export engine (xhtml2pdf) isn't installed yet — it installs "
            "on first launch. Try again shortly, or reinstall from the tray menu."
        )

    from app.reports.renderers.html import get_cached

    html = get_cached(slug, render_id)
    if html is None:
        raise ReportPdfError(
            "This report isn't cached anymore — re-run the script, then export."
        )

    # pisa is synchronous/CPU-bound — keep it off the event loop.
    return await asyncio.to_thread(_html_to_pdf, html)
