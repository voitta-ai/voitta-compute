"""Per-user storage and validation of the Claude subscription OAuth token.

The token (``sk-ant-oat01-…``, minted by ``claude setup-token``) is a long-lived
subscription credential. It is stored **only** in a 0600 file under the user's
``CLAUDE_CONFIG_DIR`` and injected into the engine subprocess as
``CLAUDE_CODE_OAUTH_TOKEN`` (see :func:`app.services.agent_sdk.config.subprocess_env`).
It is never written to a chat message, a Chainlit step, or the conversation DB —
it arrives over the ``call_fn`` ACK and lands here directly.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from app.services.agent_sdk.config import config_dir, subprocess_env, workspace_dir

logger = logging.getLogger(__name__)

_TOKEN_FILENAME = "voitta_oauth_token"


def _token_path() -> Path:
    return config_dir() / _TOKEN_FILENAME


def has_token() -> bool:
    p = _token_path()
    return p.exists() and bool(p.read_text(encoding="ascii").strip())


def load_token() -> str | None:
    try:
        p = _token_path()
        if not p.exists():
            return None
        val = p.read_text(encoding="ascii").strip()
        return val or None
    except Exception:
        logger.exception("failed to read agent-sdk token")
        return None


def store_token(token: str) -> None:
    p = _token_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(token.strip(), encoding="ascii")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


def clear_token() -> None:
    try:
        _token_path().unlink(missing_ok=True)
    except Exception:
        logger.exception("failed to clear agent-sdk token")


async def validate_token() -> bool:
    """Spawn a minimal engine turn to confirm the stored token authenticates.

    Returns False on auth failure (so the caller can clear it and re-prompt) and
    on any other error — a failed probe should never leave a bad token in place.
    """
    if not has_token():
        return False

    from claude_agent_sdk import (
        ClaudeAgentOptions,
        ResultMessage,
        query,
    )
    from claude_agent_sdk import CLINotFoundError  # type: ignore

    from app.services.agent_sdk.runtime import (
        _AUTH_HINTS,
        _can_use_tool,
        _is_auth_failure,
        user_prompt_stream,
    )

    options = ClaudeAgentOptions(
        cwd=str(workspace_dir()),
        env=subprocess_env(),
        allowed_tools=[],
        can_use_tool=_can_use_tool,
        setting_sources=None,
        max_turns=1,
        system_prompt="Reply with the single word: ok",
    )
    try:
        async for message in query(prompt=user_prompt_stream("ping"), options=options):
            if isinstance(message, ResultMessage):
                if message.is_error:
                    return not _is_auth_failure(message)
                return True
    except CLINotFoundError:
        return False
    except Exception as exc:  # noqa: BLE001
        logger.info("token probe failed: %s", exc)
        if any(h in str(exc).lower() for h in _AUTH_HINTS):
            return False
        return False
    # No ResultMessage seen — treat as inconclusive failure.
    return False
