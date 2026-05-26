import pytest

import app.tools.server.scripts  # noqa: F401
from app.reports import store
from app.tools.registry import ToolCtx, registry


async def _define(name: str, code: str) -> None:
    res = await registry.dispatch("define_script", {"name": name, "code": code}, ToolCtx())
    assert res.ok and res.result["ok"]


@pytest.mark.asyncio
async def test_edit_applies_single_replace(scripts_root):
    await _define("x", "def build(ctx):\n    return 1\n")
    res = await registry.dispatch(
        "edit_script",
        {"name": "x", "edits": [{"find": "return 1", "replace": "return 2"}]},
        ToolCtx(),
    )
    assert res.ok and res.result["ok"]
    assert "return 2" in store.read_code("x")


@pytest.mark.asyncio
async def test_edit_rejects_unmatched_find(scripts_root):
    await _define("x", "def build(ctx): return 1\n")
    res = await registry.dispatch(
        "edit_script",
        {"name": "x", "edits": [{"find": "NOPE", "replace": "X"}]},
        ToolCtx(),
    )
    assert res.ok and not res.result["ok"]
    assert "not present" in res.result["error"]


@pytest.mark.asyncio
async def test_edit_rejects_duplicate_match(scripts_root):
    await _define(
        "x",
        "def build(ctx):\n    a = 1\n    b = 1\n    return a + b\n",
    )
    res = await registry.dispatch(
        "edit_script",
        {"name": "x", "edits": [{"find": "= 1", "replace": "= 2"}]},
        ToolCtx(),
    )
    assert res.ok and not res.result["ok"]
    assert "matches 2 times" in res.result["error"]


@pytest.mark.asyncio
async def test_edit_rejects_if_smoke_test_fails(scripts_root):
    await _define("x", "def build(ctx):\n    return 1\n")
    before = store.read_code("x")
    res = await registry.dispatch(
        "edit_script",
        {
            "name": "x",
            "edits": [{"find": "return 1", "replace": "raise ValueError('bad')"}],
        },
        ToolCtx(),
    )
    assert res.ok and not res.result["ok"]
    # Source unchanged on disk.
    assert store.read_code("x") == before


@pytest.mark.asyncio
async def test_edit_applies_multiple_in_order(scripts_root):
    await _define("x", "def build(ctx):\n    A = 1\n    B = 2\n    return A + B\n")
    res = await registry.dispatch(
        "edit_script",
        {
            "name": "x",
            "edits": [
                {"find": "A = 1", "replace": "A = 10"},
                {"find": "B = 2", "replace": "B = 20"},
            ],
        },
        ToolCtx(),
    )
    assert res.ok and res.result["ok"]
    src = store.read_code("x")
    assert "A = 10" in src and "B = 20" in src
