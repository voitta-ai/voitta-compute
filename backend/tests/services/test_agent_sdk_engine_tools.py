"""Which engine built-in tools the subscription brain grants and withholds.

Three mechanisms, and only some are real boundaries:

* ``allowed_tools`` — ``_ALLOWED_ENGINE_TOOLS`` spliced in by ``_build_options``.
  An entry here AUTO-APPROVES: the SDK never consults ``can_use_tool`` for it.
* ``disallowed_tools`` — ``_DISALLOWED_ENGINE_TOOLS``. The only gate that binds
  subagents: a subagent's tool calls never reach ``can_use_tool`` at all, and
  with the callback as the sole denial a subagent wrote a file to disk
  unobserved (verified live). This is the security boundary.
* ``can_use_tool`` — reached only for tools in neither list. Second layer.

Getting the split wrong either strands a turn (the model is told a visible tool
is unavailable) or hands a mutating tool to a context we cannot see.
"""

from __future__ import annotations

import pytest

from app.services.agent_sdk import runtime
from app.services.agent_sdk.runtime import (
    _ALLOWED_ENGINE_TOOLS,
    _DISALLOWED_ENGINE_TOOLS,
    _build_options,
    _make_can_use_tool,
)
from app.tools.registry import ToolCtx

MUTATING = ["Write", "Edit", "MultiEdit", "NotebookEdit"]
PUBLISHING = ["Artifact"]
FAN_OUT = ["Agent", "Task", "Workflow", "ScheduleWakeup"]


def _options(host: str | None = None):
    return _build_options(
        system="s",
        model=None,
        resume=None,
        ctx=ToolCtx(session_id="s", host=host, email="t@example.com", extras={}),
        can_use_tool=_make_can_use_tool(),
    )


# -- the grant ----------------------------------------------------------------

@pytest.mark.parametrize("tool", ["Bash", "Read", "WebSearch", "WebFetch"])
def test_tool_is_granted(tool: str) -> None:
    assert tool in _ALLOWED_ENGINE_TOOLS
    assert tool in _options().allowed_tools


def test_no_tool_is_both_allowed_and_disallowed() -> None:
    assert not set(_ALLOWED_ENGINE_TOOLS) & set(_DISALLOWED_ENGINE_TOOLS)


# -- the boundary -------------------------------------------------------------

@pytest.mark.parametrize("tool", MUTATING + PUBLISHING + FAN_OUT)
def test_tool_is_structurally_disallowed(tool: str) -> None:
    """``disallowed_tools`` is what subagents obey; the callback is not."""
    assert tool in _DISALLOWED_ENGINE_TOOLS
    opts = _options()
    assert tool in opts.disallowed_tools
    assert tool not in opts.allowed_tools


def test_fan_out_is_closed_so_only_the_main_agent_can_ask() -> None:
    """The ask tool cannot tell a subagent from the main agent (no caller
    identity reaches a tool handler). The guarantee comes from there being no
    subagent to ask from — every fan-out tool must be withheld, not just one:
    when ``Agent`` alone was blocked the model reached for ``Workflow``."""
    for tool in FAN_OUT:
        assert tool in _options().disallowed_tools


def test_voitta_mcp_tools_are_still_exposed_and_include_ask() -> None:
    import app.tools.load  # noqa: F401  — side-effect: registers built-in tools

    allowed = list(_options().allowed_tools)
    prefix = f"mcp__{runtime.MCP_SERVER_NAME}__"
    mcp = [t for t in allowed if t.startswith(prefix)]
    assert len(mcp) >= 10, f"expected the Voitta tool surface, got {len(mcp)}"
    assert f"{prefix}ask_user_question" in mcp
    engine = [t for t in allowed if not t.startswith("mcp__")]
    assert set(engine) == set(_ALLOWED_ENGINE_TOOLS)


def test_engine_ask_user_question_is_blocked_in_favour_of_ours() -> None:
    """The engine's own AskUserQuestion is an interactive-TUI tool that does
    nothing headless. The model reaches for that name first; blocking it
    structurally sends it straight to the Voitta tool."""
    opts = _options()
    assert "AskUserQuestion" not in opts.allowed_tools
    assert "AskUserQuestion" in opts.disallowed_tools


# -- the second layer ---------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("tool", MUTATING + PUBLISHING + FAN_OUT + ["SomeFutureTool"])
async def test_callback_denies_anything_not_granted(tool: str) -> None:
    from claude_agent_sdk import PermissionResultDeny

    res = await _make_can_use_tool()(tool, {}, None)
    assert isinstance(res, PermissionResultDeny)
    assert res.message == f"{tool} is not available in this assistant"


@pytest.mark.asyncio
async def test_callback_allows_bridged_voitta_tools() -> None:
    from claude_agent_sdk import PermissionResultAllow

    name = f"mcp__{runtime.MCP_SERVER_NAME}__rag_query"
    assert isinstance(await _make_can_use_tool()(name, {}, None), PermissionResultAllow)
