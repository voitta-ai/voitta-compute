"""The Claude-subscription token probe must always reach a verdict.

The probe spawns a real engine turn. Before it was bounded, a stalled engine —
or a stream that ended without a ``ResultMessage`` — left onboarding awaiting it
forever: the "⏳ Validating your Claude token…" message never resolved and the
user's original turn was never resumed.

These tests pin the three outcomes, and in particular that a timeout is
*inconclusive* (token kept) rather than an auth failure (token cleared).
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator

import pytest

from app.services.agent_sdk import credentials


@pytest.fixture
def token_dir(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """Point the token file at a temp dir and store a token there."""
    monkeypatch.setattr(credentials, "config_dir", lambda: tmp_path)
    credentials.store_token("sk-ant-oat01-test")
    return tmp_path


def _fake_query(gen_factory) -> Any:
    """Install ``gen_factory`` as claude_agent_sdk.query for the probe."""
    def _query(*, prompt: Any, options: Any) -> AsyncIterator[Any]:
        # Drain the prompt stream so it can't be reported as never-consumed.
        return gen_factory()
    return _query


@pytest.fixture
def patch_sdk(monkeypatch: pytest.MonkeyPatch):
    """Swap claude_agent_sdk.query for a scripted async generator."""
    import claude_agent_sdk

    def _install(gen_factory) -> None:
        monkeypatch.setattr(claude_agent_sdk, "query", _fake_query(gen_factory))

    return _install


def _result(is_error: bool, subtype: str = "success", text: str = "ok"):
    """A minimally-populated ResultMessage."""
    from claude_agent_sdk import ResultMessage

    return ResultMessage(
        subtype=subtype,
        duration_ms=1,
        duration_api_ms=1,
        is_error=is_error,
        num_turns=1,
        session_id="probe",
        total_cost_usd=0.0,
        result=text,
    )


def test_runtime_helpers_the_probe_imports_still_exist():
    """The probe imports runtime helpers lazily, inside the function body.

    A refactor that renames one is invisible until a user actually onboards —
    exactly how ``_can_use_tool`` became a per-turn closure while the probe kept
    importing it at module level, breaking every validation for months.
    """
    from app.services.agent_sdk import runtime

    for name in ("_AUTH_HINTS", "_is_auth_failure", "user_prompt_stream"):
        assert hasattr(runtime, name), f"credentials.validate_token imports runtime.{name}"

    import claude_agent_sdk

    for name in ("ClaudeAgentOptions", "PermissionResultDeny", "ResultMessage", "query",
                 "CLINotFoundError"):
        assert hasattr(claude_agent_sdk, name), f"validate_token imports claude_agent_sdk.{name}"


def test_no_token_is_auth_failed(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(credentials, "config_dir", lambda: tmp_path)
    assert asyncio.run(credentials.validate_token()) == "auth_failed"


def test_successful_result_is_ok(token_dir, patch_sdk):
    async def gen():
        yield _result(is_error=False)

    patch_sdk(gen)
    assert asyncio.run(credentials.validate_token()) == "ok"


def test_auth_error_result_is_auth_failed(token_dir, patch_sdk):
    async def gen():
        yield _result(is_error=True, subtype="error", text="Invalid API key · Please run /login")

    patch_sdk(gen)
    assert asyncio.run(credentials.validate_token()) == "auth_failed"


def test_non_auth_error_result_is_inconclusive(token_dir, patch_sdk):
    async def gen():
        yield _result(is_error=True, subtype="error", text="disk full")

    patch_sdk(gen)
    assert asyncio.run(credentials.validate_token()) == "inconclusive"


def test_stream_ends_without_result_is_inconclusive(token_dir, patch_sdk):
    async def gen():
        return
        yield  # pragma: no cover - makes this an async generator

    patch_sdk(gen)
    assert asyncio.run(credentials.validate_token()) == "inconclusive"


def test_stalled_engine_times_out_and_keeps_token(
    token_dir, patch_sdk, monkeypatch: pytest.MonkeyPatch
):
    """The regression: a probe that never yields must NOT hang, and must leave
    the token in place — it is probably valid, and the resumed turn re-prompts
    if it isn't."""
    monkeypatch.setattr(credentials, "_PROBE_TIMEOUT_S", 0.25)
    closed = asyncio.Event()

    async def gen():
        try:
            await asyncio.sleep(3600)  # engine that never answers
            yield _result(is_error=False)
        finally:
            closed.set()

    patch_sdk(gen)

    async def run():
        verdict = await asyncio.wait_for(credentials.validate_token(), 5.0)
        return verdict, closed.is_set()

    verdict, was_closed = asyncio.run(run())
    assert verdict == "inconclusive"
    # The engine generator is torn down, not left running behind the probe.
    assert was_closed
    # Critically: the token survives an inconclusive probe.
    assert credentials.has_token()
