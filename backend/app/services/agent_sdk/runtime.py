"""Drive one Claude Agent SDK turn and map its events to Chainlit primitives.

Each user turn is a single ``query()`` call against the Claude Code engine.
Multi-turn continuity is ``resume=<session_id>`` (continue-only — no fork),
which keeps the engine's session id stable so the history dropdown can list
and reopen it. The new/continued session id is captured from the terminal
``ResultMessage`` and returned to the caller, which stamps it on the session
and thread for the next turn.

Tools are the registry suite, bridged in-process (see :mod:`.bridge`); the
engine's own filesystem/bash tools are denied via ``can_use_tool`` so the brain
acts only through Voitta's tools.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import chainlit as cl

# The Claude Agent SDK is installed at runtime by app.installer (like the other
# heavy LLM deps), so it may be absent at module-import time on a fresh launch.
# Import defensively: a missing SDK must not break app boot — the names below
# are only dereferenced inside run_agent_sdk_turn, which is gated behind
# is_available() and guards on ``query is None`` first.
try:
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        PermissionResultAllow,
        PermissionResultDeny,
        ResultMessage,
        SystemMessage,
        TextBlock,
        ThinkingBlock,
        ToolResultBlock,
        ToolUseBlock,
        UserMessage,
        query,
    )
    from claude_agent_sdk import CLINotFoundError  # type: ignore
except ImportError:  # SDK not installed yet
    AssistantMessage = ClaudeAgentOptions = PermissionResultAllow = None  # type: ignore
    PermissionResultDeny = ResultMessage = SystemMessage = TextBlock = None  # type: ignore
    ThinkingBlock = ToolResultBlock = ToolUseBlock = UserMessage = query = None  # type: ignore

    class CLINotFoundError(Exception):  # type: ignore
        """Placeholder so the except clause is valid when the SDK is absent."""

from app.services.agent_sdk.bridge import build_tool_server
from app.services.agent_sdk.config import (
    DEFAULT_MODEL,
    MCP_SERVER_NAME,
    subprocess_env,
    workspace_dir,
)
from app.services.agent_sdk.errors import AgentSdkAuthError, AgentSdkError, AgentSdkUnavailable
from app.tools.registry import ToolCtx

logger = logging.getLogger(__name__)

_AUTH_HINTS = (
    "invalid api key",
    "authentication",
    "unauthorized",
    "not logged in",
    "log in",
    "login",
    "oauth",
    "credit balance",
    "please run /login",
    "setup-token",
    "expired",
)


@dataclass
class TurnResult:
    session_id: str | None
    is_error: bool = False


def _truncate(text: str, limit: int = 32_000) -> str:
    return text if len(text) <= limit else text[:limit] + f"\n…[truncated: {len(text)} bytes]"


def _tool_result_text(content: Any) -> str:
    """Flatten a ToolResultBlock.content (str | list[block]) to display text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for blk in content:
            if isinstance(blk, dict):
                if blk.get("type") == "text":
                    parts.append(str(blk.get("text", "")))
                elif blk.get("type") == "image":
                    parts.append("[image]")
                else:
                    parts.append(str(blk))
            else:
                parts.append(str(blk))
        return "\n".join(parts)
    return str(content)


def _usage_tokens(usage: Any) -> int:
    """Sum the token counts in an SDK usage dict (0 if absent/odd-shaped)."""
    if not isinstance(usage, dict):
        return 0
    total = 0
    for k in ("input_tokens", "output_tokens",
              "cache_read_input_tokens", "cache_creation_input_tokens"):
        v = usage.get(k)
        if isinstance(v, (int, float)):
            total += int(v)
    return total


def _is_auth_failure(msg: ResultMessage) -> bool:
    if getattr(msg, "api_error_status", None) in (401, 403):
        return True
    blob = " ".join(
        str(x).lower()
        for x in (
            getattr(msg, "subtype", None),
            getattr(msg, "result", None),
            getattr(msg, "errors", None),
        )
        if x
    )
    return any(h in blob for h in _AUTH_HINTS)


async def user_prompt_stream(text: str) -> AsyncIterator[dict[str, Any]]:
    """One-shot streaming-input prompt.

    Streaming-input mode (an ``AsyncIterable`` prompt) is required whenever a
    ``can_use_tool`` callback is set — the SDK rejects a plain string. We yield
    exactly one user message and finish, which closes the input stream so the
    engine completes the turn.
    """
    yield {"type": "user", "message": {"role": "user", "content": text}}


async def _can_use_tool(tool_name: str, _input: dict, _ctx) -> Any:
    """Allow only the bridged Voitta tools; deny the engine's built-ins.

    This is what confines the brain to Voitta's tool surface — the engine's
    own bash/file/web tools never run.
    """
    if tool_name.startswith(f"mcp__{MCP_SERVER_NAME}__"):
        return PermissionResultAllow()
    return PermissionResultDeny(message=f"{tool_name} is not available in this assistant")


def _build_options(
    *, system: str, model: str | None, resume: str | None, ctx: ToolCtx
) -> ClaudeAgentOptions:
    server, allowed = build_tool_server(ctx)
    return ClaudeAgentOptions(
        cwd=str(workspace_dir()),
        env=subprocess_env(),
        mcp_servers={MCP_SERVER_NAME: server},
        allowed_tools=allowed,
        can_use_tool=_can_use_tool,
        system_prompt=system or None,
        model=model or DEFAULT_MODEL,
        resume=resume,
        # Do not load ~/.claude or project .claude config — keep the brain's
        # behaviour fully defined by our system prompt + tool surface.
        setting_sources=None,
        permission_mode="default",
    )


async def run_agent_sdk_turn(
    *,
    user_text: str,
    system: str,
    model: str | None,
    resume_session_id: str | None,
    ctx: ToolCtx,
) -> TurnResult:
    """Run one turn; stream output to Chainlit; return the session id.

    Raises :class:`AgentSdkUnavailable` if the engine isn't installed and
    :class:`AgentSdkAuthError` if the subscription token is missing/expired/
    rejected — the caller maps those to the disabled-brain and onboarding
    paths respectively.
    """
    if query is None:
        raise AgentSdkUnavailable("claude-agent-sdk is not installed")
    options = _build_options(system=system, model=model, resume=resume_session_id, ctx=ctx)

    streaming_msg: cl.Message | None = None
    steps: dict[str, cl.Step] = {}
    session_id: str | None = resume_session_id
    result_msg: ResultMessage | None = None
    tokens = 0  # accumulated across AssistantMessage.usage — shown live

    # One slick, self-animating status line — the only "busy" element. A
    # background ticker spins it and ticks the elapsed/token counters once a
    # second, so the turn stays lively even during the silent thinking gaps
    # between events. It owns the status step exclusively (the main loop only
    # mutates `tokens`), so there's no second writer and no pile of brown
    # half-updated lines. Removed entirely when the turn ends — no footer.
    status = cl.Step(name="Claude Code", type="run")
    status.output = "⠋ Working…"
    await status.send()

    _SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    t0 = time.monotonic()

    async def _ticker() -> None:
        i = 0
        try:
            while True:
                elapsed = int(time.monotonic() - t0)
                tail = f" · {tokens:,} tokens" if tokens else ""
                status.output = f"{_SPIN[i % len(_SPIN)]} Working… · {elapsed}s{tail}"
                await status.update()
                i += 1
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass

    async def _flush_text() -> None:
        nonlocal streaming_msg
        if streaming_msg is not None:
            await streaming_msg.update()
            streaming_msg = None

    ticker = asyncio.create_task(_ticker())
    try:
        async for message in query(prompt=user_prompt_stream(user_text), options=options):
            if isinstance(message, AssistantMessage):
                tokens += _usage_tokens(getattr(message, "usage", None))
                for block in message.content:
                    if isinstance(block, TextBlock):
                        # Assistant prose — preambles between tool calls AND the
                        # final answer. Each contiguous run is its own bubble;
                        # a tool call closes the current bubble so the next run
                        # starts fresh (nothing is merged or eaten).
                        if not block.text:
                            continue
                        if streaming_msg is None:
                            streaming_msg = cl.Message(content="")
                            await streaming_msg.send()
                        await streaming_msg.stream_token(block.text)
                    elif isinstance(block, ThinkingBlock):
                        # Reasoning isn't shown (summarised/omitted by default);
                        # the ticker already conveys "busy".
                        continue
                    elif isinstance(block, ToolUseBlock):
                        await _flush_text()
                        name = (block.name or "").removeprefix(f"mcp__{MCP_SERVER_NAME}__")
                        step = cl.Step(name=name or "tool", type="tool")
                        try:
                            import json as _json
                            step.input = _truncate(_json.dumps(block.input, ensure_ascii=False, default=str))
                        except Exception:
                            step.input = str(block.input)
                        await step.send()
                        steps[block.id] = step
            elif isinstance(message, UserMessage):
                # Tool results the engine fed back — attach to their steps.
                content = message.content
                blocks = content if isinstance(content, list) else []
                for block in blocks:
                    if isinstance(block, ToolResultBlock):
                        step = steps.get(block.tool_use_id)
                        if step is not None:
                            step.output = _truncate(_tool_result_text(block.content))
                            if block.is_error:
                                step.is_error = True
                            await step.update()
            elif isinstance(message, ResultMessage):
                result_msg = message
                if message.session_id:
                    session_id = message.session_id

        if result_msg is not None and result_msg.is_error:
            _raise_for_result(result_msg)
    except CLINotFoundError as exc:
        raise AgentSdkUnavailable(str(exc)) from exc
    except AgentSdkError:
        raise
    except Exception as exc:  # noqa: BLE001 — classify then re-raise
        # The SDK yields an error ``ResultMessage`` and *then* raises a generic
        # "returned an error result" exception on the next iteration. The
        # structured result classifies far more reliably than the exception
        # text, so prefer it when we captured one.
        if result_msg is not None and result_msg.is_error:
            _raise_for_result(result_msg)
        text = str(exc).lower()
        if any(h in text for h in _AUTH_HINTS):
            raise AgentSdkAuthError(detail=str(exc)) from exc
        raise AgentSdkError(str(exc)) from exc
    finally:
        ticker.cancel()
        try:
            await ticker
        except Exception:
            pass
        await _flush_text()
        # The status line is pure entertainment — drop it when the turn ends.
        try:
            await status.remove()
        except Exception:
            pass

    return TurnResult(session_id=session_id, is_error=False)


def _raise_for_result(result_msg: ResultMessage) -> None:
    """Raise the right typed error for an error ``ResultMessage`` (never returns)."""
    if _is_auth_failure(result_msg):
        raise AgentSdkAuthError(detail=str(result_msg.result or result_msg.errors or ""))
    raise AgentSdkError(str(result_msg.result or result_msg.errors or "agent turn failed"))
