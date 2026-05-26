"""Tests for the VRE resolver folder cache and path→file_id lookup."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

import voitta_enterprise.resolver as res
from app.services.refs import parse


FOLDERS_RESPONSE = {
    "result": [
        {"id": 10, "display_name": "Stella NFS", "source_type": "filesystem",
         "files_total": 5, "files_indexed": 5, "active": True, "shared": False},
        {"id": 11, "display_name": "BCG Articles", "source_type": "filesystem",
         "files_total": 50, "files_indexed": 50, "active": True, "shared": True},
    ]
}

DIR_RESPONSE = {
    "result": [
        {"kind": "file", "name": "report.pdf", "path": "Stella NFS/docs/report.pdf",
         "folder_id": 10, "file_id": 999, "state": "indexed",
         "size_bytes": 12345, "source_kind": "pdf"},
        {"kind": "folder", "name": "sub", "path": "Stella NFS/docs/sub/",
         "folder_id": 10, "file_id": None, "state": None,
         "size_bytes": None, "source_kind": None},
    ]
}


@pytest.fixture(autouse=True)
def _reset_cache():
    """Clear the module-level folder cache between tests."""
    res._FOLDER_CACHE.clear()
    yield
    res._FOLDER_CACHE.clear()


# ---------------------------------------------------------------------------
# _resolve_folder_id
# ---------------------------------------------------------------------------


def test_folder_cache_hit():
    res._FOLDER_CACHE["stella nfs"] = 10

    async def _run():
        with patch.object(res, "_fetch_folder_cache", new=AsyncMock()) as mock_fetch:
            fid = await res._resolve_folder_id("Stella NFS")
            mock_fetch.assert_not_called()
            return fid

    assert asyncio.run(_run()) == 10


def test_folder_cache_miss_fetches_once():
    async def _run():
        mock_fetch = AsyncMock(return_value={"stella nfs": 10, "bcg articles": 11})
        with patch.object(res, "_fetch_folder_cache", new=mock_fetch):
            fid = await res._resolve_folder_id("Stella NFS")
            assert mock_fetch.call_count == 1
            # Second call hits cache — no extra fetch
            fid2 = await res._resolve_folder_id("Stella NFS")
            assert mock_fetch.call_count == 1
            return fid, fid2

    f1, f2 = asyncio.run(_run())
    assert f1 == f2 == 10


def test_folder_not_found_raises():
    async def _run():
        mock_fetch = AsyncMock(return_value={"bcg articles": 11})
        with patch.object(res, "_fetch_folder_cache", new=mock_fetch):
            await res._resolve_folder_id("Missing Folder")

    with pytest.raises(RuntimeError, match="not found"):
        asyncio.run(_run())


def test_fetch_folder_cache_parses_response():
    async def _run():
        mock_mcp = AsyncMock(return_value=FOLDERS_RESPONSE)
        with patch.object(res, "_mcp_call", new=mock_mcp):
            cache = await res._fetch_folder_cache()
            return cache

    cache = asyncio.run(_run())
    assert cache == {"stella nfs": 10, "bcg articles": 11}


# ---------------------------------------------------------------------------
# _resolve_file_id
# ---------------------------------------------------------------------------


def test_resolve_file_id_found():
    async def _run():
        mock_mcp = AsyncMock(return_value=DIR_RESPONSE)
        with patch.object(res, "_mcp_call", new=mock_mcp):
            fid = await res._resolve_file_id("Stella NFS", "docs/report.pdf")
            return fid

    assert asyncio.run(_run()) == 999


def test_resolve_file_id_not_found_raises():
    async def _run():
        mock_mcp = AsyncMock(return_value={"result": []})
        with patch.object(res, "_mcp_call", new=mock_mcp):
            await res._resolve_file_id("Stella NFS", "docs/missing.pdf")

    with pytest.raises(RuntimeError, match="not found"):
        asyncio.run(_run())


def test_resolve_file_id_calls_correct_prefix():
    """The list call must use parent dir as prefix, not full path."""
    async def _run():
        calls = []

        async def _mock_mcp(tool, args):
            calls.append(args.get("prefix"))
            return DIR_RESPONSE

        with patch.object(res, "_mcp_call", new=_mock_mcp):
            await res._resolve_file_id("Stella NFS", "docs/report.pdf")
        return calls

    prefixes = asyncio.run(_run())
    assert prefixes == ["Stella NFS/docs"]
