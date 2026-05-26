"""Tests for the raw-HTML renderer — the sole report path."""

from __future__ import annotations

import pytest

from app.reports.renderers.html import (
    get_cached,
    render_html,
)
from app.reports.schemas import HtmlPayload


def test_render_full_doc_injects_shim() -> None:
    html = "<!doctype html><html><head><title>x</title></head><body>hi</body></html>"
    payload = render_html(html, slug="t", render_id="r1")
    assert isinstance(payload, HtmlPayload)
    assert "/api/html-report?id=t&render_id=r1" in payload.url
    body = get_cached("t", "r1")
    assert body is not None
    assert "_panel_shim.js" in body
    assert "_html_to_image.js" in body
    assert 'name="voitta-slug"' in body
    assert 'content="t"' in body


def test_render_no_head_synthesises_one() -> None:
    html = "<html><body>hi</body></html>"
    render_html(html, slug="t", render_id="r2")
    body = get_cached("t", "r2")
    assert body is not None
    assert "<head>" in body
    assert "</head>" in body
    assert "_panel_shim.js" in body


def test_render_fragment_wraps_full_doc() -> None:
    # Plain string starting with a tag — wrapped into doctype/html/head/body.
    html = "<h1>title</h1>"
    render_html(html, slug="t", render_id="r3")
    body = get_cached("t", "r3")
    assert body is not None
    assert "<!doctype html>" in body.lower()
    assert "<head>" in body
    assert "_panel_shim.js" in body


def test_render_rejects_non_string() -> None:
    with pytest.raises(TypeError, match="string"):
        render_html({"not": "html"}, slug="t", render_id="r4")  # type: ignore[arg-type]


def test_render_rejects_empty() -> None:
    with pytest.raises(ValueError, match="empty"):
        render_html("   \n  ", slug="t", render_id="r5")


def test_render_ensures_doctype() -> None:
    html = "<html><head></head><body>hi</body></html>"
    render_html(html, slug="t", render_id="r6")
    body = get_cached("t", "r6")
    assert body is not None
    assert body.lower().lstrip().startswith("<!doctype")
