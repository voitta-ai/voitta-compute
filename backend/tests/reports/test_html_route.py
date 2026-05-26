"""Tests for the /api/html-report GET route — raw-HTML flow."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.reports.renderers.html import render_html


@pytest.fixture
def client() -> TestClient:
    from app.main import app

    return TestClient(app)


def test_html_route_cache_hit(client: TestClient) -> None:
    render_html(
        "<!doctype html><html><head><title>Routed</title></head>"
        "<body>hello</body></html>",
        slug="route_demo",
        render_id="rid1",
    )
    resp = client.get(
        "/api/html-report", params={"id": "route_demo", "render_id": "rid1"},
    )
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "<title>Routed</title>" in resp.text
    assert "hello" in resp.text
    # Shim injection happened
    assert "_panel_shim.js" in resp.text


def test_html_route_cache_miss_returns_404(client: TestClient) -> None:
    resp = client.get(
        "/api/html-report", params={"id": "no_such", "render_id": "no_such"},
    )
    assert resp.status_code == 404


def test_html_route_bad_slug_returns_400(client: TestClient) -> None:
    resp = client.get(
        "/api/html-report", params={"id": "Bad Slug!", "render_id": "x"},
    )
    assert resp.status_code == 400
