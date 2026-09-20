"""Offline zip export of a rendered HTML report.

The contract: what comes out of the zip is the author's document, minus
the platform's injection, plus any backend-local bytes it referenced —
so it opens from a USB stick with the app closed.
"""

from __future__ import annotations

import base64
import io
import zipfile

import pytest
from fastapi.testclient import TestClient

from app.reports import export
from app.reports.export import (
    ExportReport,
    build_zip,
    inline_local_refs,
    strip_platform_injection,
    zip_filename,
)
from app.reports.renderers.html import get_cached, render_html

AUTHORED = (
    "<!doctype html><html><head><title>Q3</title>"
    "<style>body{color:red}</style>"
    '<script>window.mine = 1;</script>'
    "</head><body><h1>Revenue</h1>"
    '<img src="data:image/png;base64,AAAA" alt="chart">'
    "</body></html>"
)


def _unzip(data: bytes) -> dict[str, str]:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return {n: zf.read(n).decode("utf-8") for n in zf.namelist()}


# -- stripping: exactly the injection, nothing the author wrote ------------

def test_strip_removes_every_injected_piece_and_nothing_else() -> None:
    render_html(AUTHORED, slug="exp_strip", render_id="r1")
    served = get_cached("exp_strip", "r1")
    assert served is not None
    # sanity: the renderer really did inject all four kinds
    assert "voitta-slug" in served and "_panel_shim.js" in served
    assert "_html_to_image.js" in served and "/favicon.svg" in served

    cleaned = strip_platform_injection(served)

    for gone in ("voitta-slug", "voitta-render-id", "_panel_shim.js",
                 "_html_to_image.js", "/favicon.svg"):
        assert gone not in cleaned
    # the author's own head content survives untouched
    assert "<title>Q3</title>" in cleaned
    assert "body{color:red}" in cleaned
    assert "window.mine = 1;" in cleaned
    assert 'src="data:image/png;base64,AAAA"' in cleaned


def test_strip_is_a_noop_on_a_document_without_injection() -> None:
    assert strip_platform_injection(AUTHORED) == AUTHORED


# -- inlining: backend-local bytes become data: URIs -------------------------

def test_snapshot_file_ref_is_inlined_as_data_uri(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    png = tmp_path / "chart.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    monkeypatch.setattr(
        "app.services.python_storage.get",
        lambda handle: {"path": str(tmp_path)} if handle == "h1" else None,
    )
    html = '<img src="/api/workspace/data/h1/files/chart.png?x=1"><a href=\'/api/workspace/data/h1/files/chart.png\'>dl</a>'
    rep = ExportReport()
    out = inline_local_refs(html, rep)

    expected = "data:image/png;base64," + base64.b64encode(png.read_bytes()).decode()
    assert out.count(expected) == 2                  # both attrs, both quote styles
    assert "/api/workspace/data" not in out
    assert rep.inlined == ["/api/workspace/data/h1/files/chart.png"] * 2
    assert rep.unresolved == []


def test_snapshot_ref_cannot_escape_its_directory(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The export must honour the serving route's containment guard."""
    (tmp_path / "secret").write_text("nope")
    snap = tmp_path / "snap"
    snap.mkdir()
    monkeypatch.setattr("app.services.python_storage.get", lambda h: {"path": str(snap)})
    rep = ExportReport()
    out = inline_local_refs('<img src="/api/workspace/data/h/files/../secret">', rep)
    assert "nope" not in out and "data:" not in out
    assert rep.unresolved == ["/api/workspace/data/h/files/../secret"]


def test_missing_local_ref_is_reported_not_fabricated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.python_storage.get", lambda h: None)
    rep = ExportReport()
    src = '<img src="/api/workspace/data/gone/files/x.png">'
    assert inline_local_refs(src, rep) == src
    assert rep.unresolved == ["/api/workspace/data/gone/files/x.png"]


def test_remote_refs_are_left_alone_but_listed() -> None:
    rep = ExportReport()
    html = '<script src="https://cdn.example.com/plotly.js"></script><img src="data:image/png;base64,AA">'
    assert inline_local_refs(html, rep) == html
    assert rep.remote == ["https://cdn.example.com/plotly.js"]


def test_oversized_local_asset_is_skipped(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    big = tmp_path / "big.bin"
    big.write_bytes(b"x" * 10)
    monkeypatch.setattr("app.services.python_storage.get", lambda h: {"path": str(tmp_path)})
    monkeypatch.setattr(export, "_MAX_INLINE_BYTES", 5)
    rep = ExportReport()
    out = inline_local_refs('<a href="/api/workspace/data/h/files/big.bin">', rep)
    assert "data:" not in out
    assert rep.unresolved and "too large" in rep.unresolved[0]


# -- the zip ------------------------------------------------------------------

def test_zip_has_index_and_readme_and_is_offline_clean() -> None:
    render_html(AUTHORED, slug="exp_zip", render_id="r2")
    data = build_zip(get_cached("exp_zip", "r2"), slug="exp_zip", render_id="r2", title="Q3 Revenue")
    files = _unzip(data)
    assert set(files) == {"index.html", "README.txt"}
    assert "/api/" not in files["index.html"]
    assert "<h1>Revenue</h1>" in files["index.html"]
    assert files["README.txt"].startswith("Q3 Revenue\n==========\n")
    assert "Script: exp_zip" in files["README.txt"]


def test_readme_warns_about_unresolved_refs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.python_storage.get", lambda h: None)
    data = build_zip('<img src="/api/workspace/data/gone/files/x.png">', slug="s", render_id="r")
    assert "WARNING" in _unzip(data)["README.txt"]


def test_zip_filename_is_safe_and_short() -> None:
    assert zip_filename("my report/../x", "abcdef0123456789") == "my-report-..-x-abcdef01.zip"
    assert zip_filename("", "abcdef01") == "report-abcdef01.zip"


# -- the route ------------------------------------------------------------------

@pytest.fixture
def client() -> TestClient:
    from app.main import app

    return TestClient(app)


def test_export_route_serves_attachment(client: TestClient) -> None:
    render_html(AUTHORED, slug="exp_route", render_id="r3")
    resp = client.get("/api/html-report/export", params={"id": "exp_route", "render_id": "r3"})
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    assert resp.headers["content-disposition"] == 'attachment; filename="exp_route-r3.zip"'
    assert "index.html" in _unzip(resp.content)


def test_export_route_404s_like_the_view_route_when_evicted(client: TestClient) -> None:
    resp = client.get("/api/html-report/export", params={"id": "never_rendered", "render_id": "zz"})
    assert resp.status_code == 404
    assert "re-run the script" in resp.json()["detail"]


def test_export_route_rejects_bad_slug(client: TestClient) -> None:
    resp = client.get("/api/html-report/export", params={"id": "../etc", "render_id": "r"})
    assert resp.status_code == 400
