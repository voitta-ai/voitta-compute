"""Drive page-context handler — URL/title parsing without a real browser.

The handler delegates "what URL is the user on?" to the ``get_url``
browser primitive via the bridge. We monkey-patch ``call_browser`` so
the rest of the parsing logic runs in-process.
"""

from __future__ import annotations

import asyncio

import pytest

from app.tools.providers.drive import context as drive_context
from app.tools.registry import ToolCtx


def _run_with_url(monkeypatch: pytest.MonkeyPatch, href: str, title: str = ""):
    async def fake_get_url(name, args, ctx, *, timeout_ms=None):
        assert name == "get_url"
        return {"href": href, "title": title}

    monkeypatch.setattr(drive_context, "call_browser", fake_get_url)
    return asyncio.run(
        drive_context._drive_page_context({}, ToolCtx(session_id="s"))
    )


def test_my_drive_root(monkeypatch):
    out = _run_with_url(
        monkeypatch,
        "https://drive.google.com/drive/my-drive",
        "My Drive - Google Drive",
    )
    assert out["ok"] is True
    assert out["view"] == "my-drive"


def test_folder_view(monkeypatch):
    out = _run_with_url(
        monkeypatch,
        "https://drive.google.com/drive/folders/1abcDEF234ghi-jkl",
        "MyFolder - Google Drive",
    )
    assert out["view"] == "folder"
    assert out["folder_id"] == "1abcDEF234ghi-jkl"
    assert out["folder_name"] == "MyFolder"


def test_file_preview(monkeypatch):
    out = _run_with_url(
        monkeypatch,
        "https://drive.google.com/file/d/0Bxabc123/view",
        "report.pdf - Google Drive",
    )
    assert out["view"] == "file"
    assert out["file_id"] == "0Bxabc123"
    assert out["file_name"] == "report.pdf"


def test_search_query_extracted(monkeypatch):
    out = _run_with_url(
        monkeypatch,
        "https://drive.google.com/drive/search?q=quarterly+report",
        "Search - Google Drive",
    )
    assert out["view"] == "search"
    assert out["search_query"] == "quarterly report"


def test_account_index_lifted(monkeypatch):
    out = _run_with_url(
        monkeypatch,
        "https://drive.google.com/drive/u/2/my-drive",
        "My Drive - Google Drive",
    )
    assert out["view"] == "my-drive"
    assert out["account_index"] == 2


def test_unknown_path(monkeypatch):
    out = _run_with_url(
        monkeypatch,
        "https://drive.google.com/some/wild/path",
        "Whatever",
    )
    assert out["view"] == "unknown"
