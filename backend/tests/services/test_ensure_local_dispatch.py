"""Tests for ensure_local dispatch: registration, scheme routing, error cases."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.services import ensure_local as _mod
from app.services.ensure_local import EnsureLocalError, ensure_local, register
from app.services.refs import Ref


@pytest.fixture(autouse=True)
def _clean_resolvers():
    """Isolate resolver registry between tests."""
    original = dict(_mod._resolvers)
    yield
    _mod._resolvers.clear()
    _mod._resolvers.update(original)


def test_unknown_scheme_raises():
    with pytest.raises(EnsureLocalError, match="no ensure_local resolver"):
        ensure_local("unknown://folder/file.txt")


def test_invalid_ref_raises():
    with pytest.raises(EnsureLocalError, match="invalid ref"):
        ensure_local("not-a-ref-at-all")


def test_dispatch_calls_resolver(tmp_path):
    expected = tmp_path / "result.txt"
    expected.touch()

    async def _resolver(ref: Ref) -> Path:
        assert ref.scheme == "mock"
        assert ref.authority == "folder"
        assert ref.path == "file.txt"
        return expected

    register("mock", _resolver)
    result = ensure_local("mock://folder/file.txt")
    assert result == str(expected)


def test_resolver_result_returned_as_str(tmp_path):
    p = tmp_path / "out.bin"
    p.touch()

    async def _resolver(ref: Ref) -> Path:
        return p

    register("mock2", _resolver)
    result = ensure_local("mock2://f/out.bin")
    assert isinstance(result, str)
    assert result == str(p)


def test_register_replaces_existing(tmp_path, caplog):
    p1 = tmp_path / "a.txt"
    p1.touch()
    p2 = tmp_path / "b.txt"
    p2.touch()

    async def _r1(ref: Ref) -> Path:
        return p1

    async def _r2(ref: Ref) -> Path:
        return p2

    register("dup", _r1)
    register("dup", _r2)  # replaces r1
    result = ensure_local("dup://f/x")
    assert result == str(p2)


def test_resolver_timeout_raises(monkeypatch):
    import concurrent.futures

    async def _slow(ref: Ref) -> Path:
        await asyncio.sleep(9999)
        return Path("/never")

    register("slow", _slow)

    original_rctf = asyncio.run_coroutine_threadsafe

    def _fake_rctf(coro, loop):
        fut = original_rctf(coro, loop)

        class _TimeoutFut:
            def result(self, timeout=None):
                raise concurrent.futures.TimeoutError

        coro.close()
        return _TimeoutFut()

    # Only exercise the timeout path — no real loop needed here.
    # We patch run_coroutine_threadsafe so we don't actually sleep.
    import app.services.ensure_local as mod
    monkeypatch.setattr(mod.asyncio, "run_coroutine_threadsafe", _fake_rctf)

    fake_loop = AsyncMock()
    fake_loop.is_running.return_value = True

    with pytest.raises(EnsureLocalError, match="timed out"):
        ensure_local("slow://f/x", loop=fake_loop)
