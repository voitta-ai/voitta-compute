"""Per-tool host gating via ``ToolSpec.host_pattern``."""

from __future__ import annotations

import pytest

from app.tools.registry import ToolCtx, ToolRegistry, ToolSpec


async def _noop(_args: dict, _ctx: ToolCtx) -> dict:
    return {}


@pytest.fixture
def reg() -> ToolRegistry:
    return ToolRegistry()


def _spec(name: str, host_pattern=None, visibility_check=None) -> ToolSpec:
    return ToolSpec(
        name=name,
        description="",
        input_schema={"type": "object", "properties": {}},
        handler=_noop,
        side="server",
        host_pattern=host_pattern,
        visibility_check=visibility_check,
    )


def test_none_pattern_always_visible(reg: ToolRegistry) -> None:
    reg.register(_spec("core_tool"))
    assert [t["name"] for t in reg.schemas_for_host("ebay.com")] == ["core_tool"]
    assert [t["name"] for t in reg.schemas_for_host(None)] == ["core_tool"]


def test_string_pattern_suffix_matches(reg: ToolRegistry) -> None:
    reg.register(_spec("ebay_only", host_pattern="ebay.com"))
    assert [t["name"] for t in reg.schemas_for_host("www.ebay.com")] == ["ebay_only"]
    assert [t["name"] for t in reg.schemas_for_host("ebay.com")] == ["ebay_only"]
    assert reg.schemas_for_host("drive.google.com") == []
    assert reg.schemas_for_host(None) == []


def test_string_pattern_does_not_match_evil_suffix(reg: ToolRegistry) -> None:
    """``ebay.com`` must NOT match ``ebay.com.evil.com`` — strict suffix on
    a hostname boundary, not a substring."""
    reg.register(_spec("ebay_only", host_pattern="ebay.com"))
    assert reg.schemas_for_host("ebay.com.evil.com") == []


def test_list_pattern_or_match(reg: ToolRegistry) -> None:
    reg.register(_spec("multi", host_pattern=["ebay.com", "drive.google.com"]))
    assert [t["name"] for t in reg.schemas_for_host("www.ebay.com")] == ["multi"]
    assert [t["name"] for t in reg.schemas_for_host("drive.google.com")] == ["multi"]
    assert reg.schemas_for_host("example.com") == []


def test_port_stripped_from_host(reg: ToolRegistry) -> None:
    reg.register(_spec("ebay_only", host_pattern="ebay.com"))
    assert [t["name"] for t in reg.schemas_for_host("www.ebay.com:443")] == ["ebay_only"]


def test_visibility_check_hides_tool(reg: ToolRegistry) -> None:
    reg.register(_spec("gated", visibility_check=lambda: False))
    reg.register(_spec("ungated"))
    assert [t["name"] for t in reg.schemas_for_host(None)] == ["ungated"]


def test_visibility_check_raising_hides_tool(reg: ToolRegistry) -> None:
    def boom() -> bool:
        raise RuntimeError("oops")

    reg.register(_spec("gated", visibility_check=boom))
    assert reg.schemas_for_host(None) == []
