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

Fonts: the stock PDF base-14 fonts (Helvetica/Times/Courier) are Latin-only, so
non-Latin text (Cyrillic, Greek, CJK, many accented chars) would render as tofu
boxes. :func:`_ensure_unicode_fonts` registers DejaVu (bundled by matplotlib, a
core dep) and repoints xhtml2pdf's fallback table onto it, so every font-family —
registered or not — resolves to a glyph-complete font.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import httpx

from app.config import HOST, PLAINTEXT_PORT

logger = logging.getLogger(__name__)

_BASE = f"http://{HOST}:{PLAINTEXT_PORT}"

# One-time guard: DejaVu registered with ReportLab + xhtml2pdf's fallback table
# remapped. See _ensure_unicode_fonts.
_fonts_ready = False


class ReportPdfError(RuntimeError):
    """Export failed — the message is safe to surface to the user."""


def _engine_available() -> bool:
    import importlib.util

    return importlib.util.find_spec("xhtml2pdf") is not None


# DejaVu variant filename → (reportlab name, is-bold, is-italic), per family.
# DejaVu ships full Cyrillic/Greek/Latin coverage; the stock PDF base-14 fonts
# (Helvetica/Times/Courier) are Latin-only, which is why unmapped non-Latin text
# renders as tofu boxes (□). matplotlib (a core dep) bundles these TTFs.
_DEJAVU_FAMILIES: dict[str, list[tuple[str, str, int, int]]] = {
    # reportlab family name : [(filename, variant reportlab name, bold, italic)]
    "DejaVuSans": [
        ("DejaVuSans.ttf", "DejaVuSans", 0, 0),
        ("DejaVuSans-Bold.ttf", "DejaVuSans-Bold", 1, 0),
        ("DejaVuSans-Oblique.ttf", "DejaVuSans-Oblique", 0, 1),
        ("DejaVuSans-BoldOblique.ttf", "DejaVuSans-BoldOblique", 1, 1),
    ],
    "DejaVuSerif": [
        ("DejaVuSerif.ttf", "DejaVuSerif", 0, 0),
        ("DejaVuSerif-Bold.ttf", "DejaVuSerif-Bold", 1, 0),
        ("DejaVuSerif-Italic.ttf", "DejaVuSerif-Italic", 0, 1),
        ("DejaVuSerif-BoldItalic.ttf", "DejaVuSerif-BoldItalic", 1, 1),
    ],
    "DejaVuSansMono": [
        ("DejaVuSansMono.ttf", "DejaVuSansMono", 0, 0),
        ("DejaVuSansMono-Bold.ttf", "DejaVuSansMono-Bold", 1, 0),
        ("DejaVuSansMono-Oblique.ttf", "DejaVuSansMono-Oblique", 0, 1),
        ("DejaVuSansMono-BoldOblique.ttf", "DejaVuSansMono-BoldOblique", 1, 1),
    ],
}

# Remap of xhtml2pdf's Latin-only fallback table onto the DejaVu families, so
# every CSS font-family (registered or not — unknown families fall back to
# "helvetica") resolves to a font that has the glyphs. Keys mirror
# xhtml2pdf.default.DEFAULT_FONT.
_FALLBACK_REMAP: dict[str, str] = {
    # sans
    "helvetica": "DejaVuSans", "helvetica-bold": "DejaVuSans-Bold",
    "helvetica-oblique": "DejaVuSans-Oblique",
    "helvetica-boldoblique": "DejaVuSans-BoldOblique",
    "arial": "DejaVuSans", "verdana": "DejaVuSans", "geneva": "DejaVuSans",
    "sansserif": "DejaVuSans", "sans": "DejaVuSans",
    # serif
    "times": "DejaVuSerif", "times-roman": "DejaVuSerif",
    "times-bold": "DejaVuSerif-Bold", "times-oblique": "DejaVuSerif-Italic",
    "times-boldoblique": "DejaVuSerif-BoldItalic",
    "times new roman": "DejaVuSerif", "georgia": "DejaVuSerif", "serif": "DejaVuSerif",
    # mono
    "courier": "DejaVuSansMono", "courier-bold": "DejaVuSansMono-Bold",
    "courier-oblique": "DejaVuSansMono-Oblique",
    "courier-boldoblique": "DejaVuSansMono-BoldOblique",
    "courier new": "DejaVuSansMono", "monospaced": "DejaVuSansMono",
    "monospace": "DejaVuSansMono", "mono": "DejaVuSansMono",
}


def _dejavu_dir() -> Path | None:
    """Directory of DejaVu TTFs — sourced from matplotlib (a core dep)."""
    try:
        import matplotlib

        d = Path(matplotlib.get_data_path()) / "fonts" / "ttf"
        return d if (d / "DejaVuSans.ttf").exists() else None
    except Exception:  # noqa: BLE001 — matplotlib missing → skip Unicode fonts
        return None


def _ensure_unicode_fonts() -> None:
    """Register DejaVu with ReportLab and repoint xhtml2pdf's fallback table.

    Idempotent and best-effort: if the fonts can't be found, non-Latin text will
    still tofu but the export otherwise works, so we log and move on rather than
    fail the whole export. Runs inside the export thread (before CreatePDF).
    """
    global _fonts_ready
    if _fonts_ready:
        return

    ttf_dir = _dejavu_dir()
    if ttf_dir is None:
        logger.warning("report_pdf: DejaVu fonts not found — non-Latin text may not render")
        return

    from reportlab.lib.fonts import addMapping
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from xhtml2pdf import default as x2p_default

    for family, variants in _DEJAVU_FAMILIES.items():
        for filename, rl_name, bold, italic in variants:
            path = ttf_dir / filename
            if not path.exists():
                continue
            try:
                pdfmetrics.registerFont(TTFont(rl_name, str(path)))
            except Exception:  # noqa: BLE001 — already registered / unreadable
                pass
            # Family mapping so ReportLab picks the right weight for <b>/<i>.
            addMapping(family, bold, italic, rl_name)

    # Repoint the Latin-only fallback table onto DejaVu. The context copies this
    # dict per render, so mutating the module map is enough for future exports.
    x2p_default.DEFAULT_FONT.update(_FALLBACK_REMAP)
    _fonts_ready = True
    logger.info("report_pdf: DejaVu Unicode fonts registered")


def _html_to_pdf(html: str) -> bytes:
    """Convert an HTML string to PDF bytes (blocking; run in a thread).

    Resolves loopback/relative image srcs against the live backend via a
    ``link_callback``; cleans up any temp files it downloads.
    """
    from xhtml2pdf import pisa  # local import — installed lazily at first launch

    _ensure_unicode_fonts()  # so Cyrillic/Greek/CJK/accented text isn't tofu

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
