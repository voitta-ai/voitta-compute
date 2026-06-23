"""Typed errors for the Claude Agent SDK brain.

These let the chat layer distinguish *why* a turn failed so it can react:
``AgentSdkAuthError`` triggers the in-chat token-onboarding flow (Phase 2),
``AgentSdkUnavailable`` means the Claude Code engine isn't installed (the
brain should be hidden / disabled — Phase 4), and ``AgentSdkError`` is the
catch-all surfaced to the user verbatim.
"""

from __future__ import annotations


class AgentSdkError(RuntimeError):
    """Base class for Agent SDK brain failures."""


class AgentSdkUnavailable(AgentSdkError):
    """The Claude Code engine (CLI/Node) is not installed or not runnable."""


class AgentSdkAuthError(AgentSdkError):
    """The subscription token is missing, expired, or rejected.

    Carries an optional ``detail`` for logging; the chat layer shows the
    onboarding card rather than this message.
    """

    def __init__(self, message: str = "", *, detail: str = "") -> None:
        super().__init__(message or "Claude subscription not authenticated")
        self.detail = detail
