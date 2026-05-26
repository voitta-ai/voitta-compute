import pytest

# Tools self-register on import; importing the package wires the registry.
import app.tools.server.scripts  # noqa: F401
from app.reports import store
from app.tools.registry import ToolCtx, registry


@pytest.mark.asyncio
async def test_define_writes_and_smoketests(scripts_root):
    res = await registry.dispatch(
        "define_script",
        {"name": "demo", "code": "def build(ctx):\n    return 'hello'\n"},
        ToolCtx(),
    )
    assert res.ok and res.result["ok"]
    assert store.exists("demo")
    assert "hello" in store.read_code("demo")


@pytest.mark.asyncio
async def test_define_rejects_bad_slug(scripts_root):
    res = await registry.dispatch(
        "define_script",
        {"name": "BAD/slug", "code": "def build(ctx): pass\n"},
        ToolCtx(),
    )
    assert res.ok
    assert not res.result["ok"]
    assert "slug" in res.result["error"].lower()


@pytest.mark.asyncio
async def test_define_rejects_bad_code_no_disk_write(scripts_root):
    res = await registry.dispatch(
        "define_script",
        {"name": "boom", "code": "def build(ctx):\n    raise RuntimeError('x')\n"},
        ToolCtx(),
    )
    assert res.ok and not res.result["ok"]
    assert "RuntimeError" in res.result["error"]
    # No file should be on disk.
    assert not store.exists("boom")


@pytest.mark.asyncio
async def test_define_rejects_duplicate(scripts_root):
    await registry.dispatch(
        "define_script",
        {"name": "dup", "code": "def build(ctx): pass\n"},
        ToolCtx(),
    )
    res = await registry.dispatch(
        "define_script",
        {"name": "dup", "code": "def build(ctx): return 1\n"},
        ToolCtx(),
    )
    assert res.ok and not res.result["ok"]
    assert "already exists" in res.result["error"]


@pytest.mark.asyncio
async def test_define_rejects_missing_build(scripts_root):
    res = await registry.dispatch(
        "define_script",
        {"name": "nobuild", "code": "x = 1\n"},
        ToolCtx(),
    )
    assert res.ok and not res.result["ok"]
    assert "build" in res.result["error"]
