"""Which engine built-in tools the subscription brain grants.

Two mechanisms decide this, and only one of them is live per tool:

* ``allowed_tools`` — ``_build_options`` splices ``_ALLOWED_ENGINE_TOOLS`` in,
  and an entry there AUTO-APPROVES the tool: the SDK never consults
  ``can_use_tool`` for it (it warns as much — ``CanUseToolShadowedWarning``).
  This is the real grant, so it is tested through ``_build_options``, not by
  poking the callback.
* ``can_use_tool`` — only reached for tools *absent* from ``allowed_tools``,
  where it supplies the denial. That path is live: the "<tool> is not available
  in this assistant" message users saw for WebSearch/WebFetch came from it.

Getting the split wrong either strands a turn (the model is told a tool it can
plainly see is unavailable) or hands it a mutating tool we meant to withhold.
"""

from __future__ import annotations

import pytest

from app.services.agent_sdk import runtime
from app.services.agent_sdk.runtime import (
    _ALLOWED_ENGINE_TOOLS,
    _INTERACTIVE_ENGINE_TOOLS,
    _build_options,
    _make_can_use_tool,
)
from app.tools.registry import ToolCtx

WITHHELD = ["Write", "Edit", "NotebookEdit", "MultiEdit"]


def _can_use():
    return _make_can_use_tool(cl_ctx=None, wait_state={}, deadline=[None])


def _options(host: str | None = None):
    return _build_options(
        system="s",
        model=None,
        resume=None,
        ctx=ToolCtx(session_id="s", host=host, email="t@example.com", extras={}),
        can_use_tool=_can_use(),
    )


# -- the grant tuple ----------------------------------------------------------

@pytest.mark.parametrize("tool", ["Bash", "Read", "WebSearch", "WebFetch"])
def test_tool_is_granted(tool: str) -> None:
    assert tool in _ALLOWED_ENGINE_TOOLS


@pytest.mark.parametrize("tool", WITHHELD)
def test_mutating_engine_tools_stay_withheld(tool: str) -> None:
    """State changes go through the Voitta MCP surface, not the engine's own."""
    assert tool not in _ALLOWED_ENGINE_TOOLS


# -- the live mechanism: what reaches ClaudeAgentOptions ----------------------

@pytest.mark.parametrize("tool", ["Bash", "Read", "WebSearch", "WebFetch"])
def test_granted_tools_reach_allowed_tools(tool: str) -> None:
    """The auto-approval path — this is what actually enables the tool.

    Asserting on the callback instead would test shadowed code: the SDK skips
    ``can_use_tool`` for anything listed here.
    """
    assert tool in _options().allowed_tools


@pytest.mark.parametrize("tool", WITHHELD)
def test_withheld_tools_never_reach_allowed_tools(tool: str) -> None:
    assert tool not in _options().allowed_tools


def test_voitta_mcp_tools_are_still_exposed() -> None:
    """The engine grant must not crowd out the plugin surface.

    Regression guard: ``allowed_tools`` is one flat list, so a mistake in the
    splice could drop the ``mcp__voitta__*`` entries and silently reduce the
    assistant to engine built-ins.
    """
    import app.tools.load  # noqa: F401  — side-effect: registers built-in tools

    allowed = list(_options().allowed_tools)
    mcp = [t for t in allowed if t.startswith(f"mcp__{runtime.MCP_SERVER_NAME}__")]
    assert len(mcp) >= 10, f"expected the Voitta tool surface, got {len(mcp)}"
    engine = [t for t in allowed if not t.startswith("mcp__")]
    assert set(engine) == set(_ALLOWED_ENGINE_TOOLS) | set(_INTERACTIVE_ENGINE_TOOLS)


# -- the live callback path: denial of anything not granted -------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("tool", WITHHELD)
async def test_callback_denies_withheld_tools(tool: str) -> None:
    """Reached because these are absent from ``allowed_tools``.

    The message is the one users hit for WebSearch/WebFetch before they were
    granted, which is what proves this branch executes in production.
    """
    from claude_agent_sdk import PermissionResultDeny

    res = await _can_use()(tool, {}, None)
    assert isinstance(res, PermissionResultDeny)
    assert res.message == f"{tool} is not available in this assistant"


@pytest.mark.asyncio
async def test_callback_allows_bridged_voitta_tools() -> None:
    """Fallback for callers that narrow ``allowed_tools``; matched by prefix."""
    from claude_agent_sdk import PermissionResultAllow

    name = f"mcp__{runtime.MCP_SERVER_NAME}__rag_query"
    assert isinstance(await _can_use()(name, {}, None), PermissionResultAllow)
