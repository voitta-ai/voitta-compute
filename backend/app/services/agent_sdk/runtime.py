"""Drive one Claude Agent SDK turn and map its events to Chainlit primitives.

Each user turn is a single ``query()`` call against the Claude Code engine.
Multi-turn continuity is ``resume=<session_id>`` (continue-only — no fork),
which keeps the engine's session id stable so the history dropdown can list
and reopen it. The new/continued session id is captured from the terminal
``ResultMessage`` and returned to the caller, which stamps it on the session
and thread for the next turn.

Tools are the registry suite, bridged in-process (see :mod:`.bridge`), plus a
small allowlist of engine built-ins (``Bash``) gated by ``can_use_tool``; the
rest of the engine's native filesystem/web tools are denied, so Voitta's tools
stay the primary surface.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import chainlit as cl
from chainlit.context import context_var as cl_context_var, get_context as cl_get_context

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


# Wall-clock ceiling for one brain turn. Agentic loops — especially now that the
# engine can run Bash — can otherwise run indefinitely (a command waiting on
# stdin, a runaway define/run/probe loop). An unbounded turn holds the engine
# subprocess *and* keeps the event loop it streams on busy, which is the "dead
# session" that makes the whole app look wedged. On expiry we close the SDK
# generator (terminating the engine subprocess) and surface a clean error.
# Override with VOITTA_BRAIN_TURN_TIMEOUT_S (seconds).
try:
    _TURN_TIMEOUT_S = float(os.environ.get("VOITTA_BRAIN_TURN_TIMEOUT_S", "600"))
except ValueError:
    _TURN_TIMEOUT_S = 600.0

# How long an AskUserQuestion waits for the human before giving up. Generous by
# design — and on expiry the tool is DENIED with "no response", never silently
# auto-answered (the CLI's brief auto-continue-after-60s experiment showed why:
# a question the model asks is a gate, not a suggestion). While a question is
# pending the turn deadline above is extended so it can't kill the wait.
try:
    _ASK_TIMEOUT_S = float(os.environ.get("VOITTA_ASK_USER_TIMEOUT_S", "1800"))
except ValueError:
    _ASK_TIMEOUT_S = 1800.0


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


# Engine built-in tools the brain may use *alongside* the bridged Voitta suite.
# Bash is enabled deliberately: the engine reaches for it naturally, and letting
# it run a command in its pinned per-user workspace cwd is better UX than a hard
# refusal. Read is enabled because it is the engine's only way to VIEW images —
# chat attachments are persisted to the project's uploads tree and handed over
# as file paths (see chainlit_app._persist_attachments_for_engine); with Bash
# already allowed, Read grants no filesystem access Bash didn't have. The rest
# of the engine's native tools (write/edit/web/…) stay denied, so the Voitta
# MCP tools remain the primary surface.
_ALLOWED_ENGINE_TOOLS: tuple[str, ...] = ("Bash", "Read")

# Engine tools that are allowed but never pass through unattended — each one is
# intercepted in ``can_use_tool`` and satisfied by our own UI flow.
_INTERACTIVE_ENGINE_TOOLS: tuple[str, ...] = ("AskUserQuestion",)


def _fmt_answer(val: Any) -> str:
    """Human-readable form of one answer for the transcript summary line."""
    if isinstance(val, list):
        return ", ".join(str(v) for v in val)
    return str(val) if val is not None else "—"


async def _ask_user_question(
    tool_input: dict,
    *,
    cl_ctx: Any,
    wait_state: dict,
    deadline: list,
) -> Any:
    """Bridge one AskUserQuestion call to the chat UI and wait for the human.

    Runs inside an SDK-spawned task, so the Chainlit context is re-bound
    explicitly from ``cl_ctx`` (contextvar propagation through the SDK's task
    chain is an implementation detail we don't lean on). The reply travels
    over Chainlit's *element* ask channel — unlike the action channel it
    passes the response dict through verbatim (no ``name``/``label`` key
    access server-side), so the card can return arbitrary
    ``{answers, annotations, response, cancelled}`` shapes.

    While waiting, the turn's wall-clock deadline (``deadline[0]``, the active
    ``asyncio.Timeout``) is pushed out so a human reading a question can't
    trip the runaway-turn ceiling; it is restored to the normal ceiling on
    every exit path.
    """
    questions = tool_input.get("questions")
    if not isinstance(questions, list) or not questions:
        return PermissionResultDeny(message="AskUserQuestion: malformed questions payload")
    if cl_ctx is None:
        return PermissionResultDeny(
            message="No interactive user is attached to this session; do not ask questions here"
        )

    loop = asyncio.get_running_loop()
    cm = deadline[0]
    if cm is not None:
        try:
            cm.reschedule(loop.time() + _ASK_TIMEOUT_S + 60.0)
        except Exception:
            logger.exception("ask: deadline extend failed")
    wait_state["asking"] = True
    token = cl_context_var.set(cl_ctx)
    try:
        first_q = str((questions[0] or {}).get("question", "")).strip()
        headline = f"❓ {first_q}" if len(questions) == 1 and first_q else "❓ Claude has a question"
        element = cl.CustomElement(name="AskUserQuestion", props={"questions": questions})
        ask = cl.AskElementMessage(content=headline, element=element, timeout=int(_ASK_TIMEOUT_S))
        res = await ask.send()

        async def _finish(content: str) -> None:
            ask.content = content
            try:
                await ask.update()
            except Exception:
                logger.exception("ask: transcript update failed")

        if res is None:  # Chainlit timeout → returns None (ask_timeout emitted)
            await _finish(f"{headline}\n\n*(no response — timed out)*")
            return PermissionResultDeny(
                message=(
                    f"The user did not respond within {int(_ASK_TIMEOUT_S // 60)} minutes. "
                    "Do not assume an answer; proceed only with what you already know, "
                    "or end the turn so the user can follow up."
                )
            )
        data = dict(res) if isinstance(res, dict) else {}
        if data.get("cancelled"):
            await _finish(f"{headline}\n\n*(dismissed by the user)*")
            return PermissionResultDeny(message="User declined to answer.")

        # Freeform path: the user typed into the composer instead of picking
        # options. The engine renders this as "The user responded: …".
        response = data.get("response")
        if isinstance(response, str) and response.strip():
            await _finish(f"{headline}\n\n> {response.strip()}")
            return PermissionResultAllow(
                updated_input={"questions": questions, "response": response.strip()}
            )

        answers = data.get("answers")
        if not isinstance(answers, dict) or not answers:
            await _finish(f"{headline}\n\n*(dismissed by the user)*")
            return PermissionResultDeny(message="User dismissed the question without answering.")

        updated: dict[str, Any] = {"questions": questions, "answers": answers}
        annotations = data.get("annotations")
        if isinstance(annotations, dict) and annotations:
            updated["annotations"] = annotations

        lines = []
        for q in questions:
            if not isinstance(q, dict):
                continue
            label = str(q.get("header") or q.get("question") or "?").strip()
            lines.append(f"- **{label}**: {_fmt_answer(answers.get(str(q.get('question', ''))))}")
        await _finish("❓ Answered:\n" + "\n".join(lines))
        return PermissionResultAllow(updated_input=updated)
    except Exception as exc:  # noqa: BLE001 — a UI failure must not kill the turn
        logger.exception("AskUserQuestion handling failed")
        return PermissionResultDeny(message=f"The question could not be shown to the user: {exc}")
    finally:
        cl_context_var.reset(token)
        wait_state["asking"] = False
        if cm is not None:
            try:
                cm.reschedule(loop.time() + _TURN_TIMEOUT_S)
            except Exception:
                pass


def _make_can_use_tool(*, cl_ctx: Any, wait_state: dict, deadline: list):
    """Per-turn ``can_use_tool``: intercept AskUserQuestion into the chat UI,
    allow the bridged Voitta tools plus the engine-builtin allowlist, deny the
    rest of the engine's native tools.
    """

    async def _can_use_tool(tool_name: str, tool_input: dict, _ctx) -> Any:
        if tool_name in _INTERACTIVE_ENGINE_TOOLS:
            return await _ask_user_question(
                tool_input, cl_ctx=cl_ctx, wait_state=wait_state, deadline=deadline
            )
        if tool_name.startswith(f"mcp__{MCP_SERVER_NAME}__") or tool_name in _ALLOWED_ENGINE_TOOLS:
            return PermissionResultAllow()
        return PermissionResultDeny(message=f"{tool_name} is not available in this assistant")

    return _can_use_tool


def _build_options(
    *, system: str, model: str | None, resume: str | None, ctx: ToolCtx, can_use_tool: Any
) -> ClaudeAgentOptions:
    server, allowed = build_tool_server(ctx)
    return ClaudeAgentOptions(
        cwd=str(workspace_dir()),
        env=subprocess_env(),
        mcp_servers={MCP_SERVER_NAME: server},
        allowed_tools=[*allowed, *_ALLOWED_ENGINE_TOOLS, *_INTERACTIVE_ENGINE_TOOLS],
        can_use_tool=can_use_tool,
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

    # AskUserQuestion plumbing: the Chainlit context captured here is re-bound
    # inside the SDK-spawned callback task; ``wait_state`` flips the ticker to
    # its "waiting" face; ``deadline`` carries the asyncio.Timeout so the ask
    # handler can extend/restore the turn ceiling around a human-paced wait.
    try:
        cl_ctx: Any = cl_get_context()
    except Exception:
        cl_ctx = None
    wait_state: dict[str, bool] = {"asking": False}
    deadline: list[Any] = [None]
    options = _build_options(
        system=system, model=model, resume=resume_session_id, ctx=ctx,
        can_use_tool=_make_can_use_tool(cl_ctx=cl_ctx, wait_state=wait_state, deadline=deadline),
    )

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
                if wait_state["asking"]:
                    status.output = f"❓ Waiting for your answer… · {elapsed}s{tail}"
                else:
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
    # Hold the generator explicitly so we can guarantee it's closed on every
    # exit path — closing it tears down the SDK transport + engine subprocess,
    # so a stuck turn can't linger.
    agen = query(prompt=user_prompt_stream(user_text), options=options)
    timed_out = False
    logger.info(
        "agent_sdk turn start: resume=%s model=%s prompt_chars=%d",
        resume_session_id or "-", model or DEFAULT_MODEL, len(user_text),
    )
    try:
        # asyncio.timeout (3.11+) fires between/after awaits — an agentic loop
        # yields control at each engine round-trip, so the deadline is honoured
        # even mid-turn. (A single synchronous block inside a tool would not be
        # preempted; the heavy tools — e.g. run_script — are already thread
        # off-loaded, so in practice the turn stays interruptible.)
        async with asyncio.timeout(_TURN_TIMEOUT_S) as _turn_deadline:
            # Hand the Timeout to the ask handler so a pending question can
            # push the ceiling out (and restore it) — see _ask_user_question.
            deadline[0] = _turn_deadline
            async for message in agen:
                if isinstance(message, AssistantMessage):
                    tokens += _usage_tokens(getattr(message, "usage", None))
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            # Assistant prose — preambles between tool calls AND
                            # the final answer. Each contiguous run is its own
                            # bubble; a tool call closes the current bubble so the
                            # next run starts fresh (nothing is merged or eaten).
                            if not block.text:
                                continue
                            if streaming_msg is None:
                                streaming_msg = cl.Message(content="")
                                await streaming_msg.send()
                            await streaming_msg.stream_token(block.text)
                        elif isinstance(block, ThinkingBlock):
                            # Reasoning isn't shown (summarised/omitted by
                            # default); the ticker already conveys "busy".
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
                elif isinstance(message, SystemMessage):
                    # The engine's init event announces the session id at turn
                    # START. Latch it as the user's active session right away so
                    # the history dropdown can title an in-flight conversation —
                    # waiting for the ResultMessage meant multi-minute first
                    # turns showed the default label the whole time.
                    sid = (getattr(message, "data", None) or {}).get("session_id")
                    if sid and sid != session_id:
                        session_id = sid
                        logger.info("agent_sdk turn: session id %s", sid)
                        try:
                            from app.services.agent_sdk.selection import set_active
                            set_active(ctx.email, sid)
                        except Exception:
                            logger.exception("set_active at init failed")
                elif isinstance(message, ResultMessage):
                    result_msg = message
                    if message.session_id:
                        session_id = message.session_id

        if result_msg is not None and result_msg.is_error:
            _raise_for_result(result_msg)
    except (TimeoutError, asyncio.TimeoutError):
        # Turn exceeded the wall-clock ceiling — treat as a clean, non-fatal end
        # rather than a crash. The finally block closes the generator (killing
        # the engine subprocess); we surface a message below and keep the session
        # id so the user can continue.
        timed_out = True
        logger.warning("agent_sdk turn timed out after %.0fs", _TURN_TIMEOUT_S)
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
        # Always tear the engine down — aclose() propagates GeneratorExit into
        # the SDK's query loop, which terminates the subprocess transport. This
        # is what stops a stuck/interactive turn from lingering as a "dead
        # session" that holds the event loop.
        try:
            await agen.aclose()
        except Exception:
            pass
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

    logger.info(
        "agent_sdk turn end: session=%s tokens=%d elapsed=%.0fs timed_out=%s",
        session_id or "-", tokens, time.monotonic() - t0, timed_out,
    )
    if timed_out:
        mins = int(_TURN_TIMEOUT_S // 60)
        await cl.Message(
            content=(
                f"⏱️ This turn ran longer than {mins} min and was stopped so it "
                "couldn't hang the app. This often means a command was waiting "
                "for input or a step looped. Send another message to continue — "
                "the conversation is preserved."
            ),
        ).send()
        return TurnResult(session_id=session_id, is_error=True)

    return TurnResult(session_id=session_id, is_error=False)


def _raise_for_result(result_msg: ResultMessage) -> None:
    """Raise the right typed error for an error ``ResultMessage`` (never returns)."""
    if _is_auth_failure(result_msg):
        raise AgentSdkAuthError(detail=str(result_msg.result or result_msg.errors or ""))
    raise AgentSdkError(str(result_msg.result or result_msg.errors or "agent turn failed"))
