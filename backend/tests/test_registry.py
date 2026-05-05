"""ToolRegistry — registration, host gating, visibility gating, dispatch."""

from __future__ import annotations

import asyncio

import pytest

from app.tools.registry import ToolCtx, ToolRegistry, ToolSpec


def _spec(name: str, *, host_pattern: str | None = None,
          visibility_check=None, handler=None) -> ToolSpec:
    async def default_handler(args, ctx):
        return {"name": name, "args": args}
    return ToolSpec(
        name=name,
        description=f"{name} stub",
        input_schema={"type": "object", "additionalProperties": False},
        handler=handler or default_handler,
        host_pattern=host_pattern,
        visibility_check=visibility_check,
    )


def test_register_and_names():
    r = ToolRegistry()
    r.register(_spec("a"))
    r.register(_spec("b"))
    assert sorted(r.names()) == ["a", "b"]


def test_duplicate_registration_rejected():
    r = ToolRegistry()
    r.register(_spec("dup"))
    with pytest.raises(ValueError):
        r.register(_spec("dup"))


def test_visible_for_host_no_pattern_always_visible():
    r = ToolRegistry()
    r.register(_spec("plain"))
    assert [s.name for s in r.visible_for_host(None)] == ["plain"]
    assert [s.name for s in r.visible_for_host("drive.google.com")] == ["plain"]


def test_visible_for_host_strict_suffix_match():
    r = ToolRegistry()
    r.register(_spec("drive_x", host_pattern="drive.google.com"))

    # Exact host match.
    assert [s.name for s in r.visible_for_host("drive.google.com")] == ["drive_x"]
    # Subdomain match.
    assert [s.name for s in r.visible_for_host("foo.drive.google.com")] == ["drive_x"]
    # Port stripped before match.
    assert [s.name for s in r.visible_for_host("drive.google.com:443")] == ["drive_x"]
    # Non-match.
    assert r.visible_for_host("example.com") == []
    # No host yet → host-gated tool hidden.
    assert r.visible_for_host(None) == []


def test_visible_for_host_rejects_dotted_suffix_attack():
    """`pat='drive.google.com'` must NOT match `drive.google.com.evil.com`."""
    r = ToolRegistry()
    r.register(_spec("drive_x", host_pattern="drive.google.com"))
    assert r.visible_for_host("drive.google.com.evil.com") == []


def test_visibility_check_runtime_gate():
    flag = {"on": False}
    r = ToolRegistry()
    r.register(_spec("gated", visibility_check=lambda: flag["on"]))

    assert r.visible_for_host(None) == []
    flag["on"] = True
    assert [s.name for s in r.visible_for_host(None)] == ["gated"]


def test_visibility_check_exception_treated_as_hidden():
    def bad():
        raise RuntimeError("oauth check blew up")

    r = ToolRegistry()
    r.register(_spec("brittle", visibility_check=bad))
    assert r.visible_for_host(None) == []


def test_dispatch_unknown_tool_returns_error():
    r = ToolRegistry()
    res = asyncio.run(r.dispatch("nope", {}, ToolCtx()))
    assert res.ok is False
    assert res.error and res.error["kind"] == "unknown_tool"


def test_dispatch_handler_exception_captured():
    async def boom(args, ctx):
        raise ValueError("kaboom")

    r = ToolRegistry()
    r.register(_spec("explode", handler=boom))
    res = asyncio.run(r.dispatch("explode", {}, ToolCtx()))
    assert res.ok is False
    assert res.error == {"kind": "ValueError", "message": "kaboom"}
    assert res.latency_ms >= 0


def test_dispatch_success_envelope():
    r = ToolRegistry()
    r.register(_spec("ok"))
    res = asyncio.run(r.dispatch("ok", {"k": 1}, ToolCtx()))
    assert res.ok is True
    assert res.result == {"name": "ok", "args": {"k": 1}}
