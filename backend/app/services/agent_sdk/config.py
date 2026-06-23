"""Per-user paths, subprocess env, and engine-availability probe for the
Claude Agent SDK brain.

Two invariants this module enforces, both load-bearing:

* **Stable per-user working directory.** Claude Code keys its session store
  by an encoded form of the cwd (``<CLAUDE_CONFIG_DIR>/projects/<encoded-cwd>``).
  If the cwd drifts between turns, prior sessions disappear from
  ``list_sessions()``. We pin a single deterministic dir per user so history
  is always listable and resumable.

* **No ``ANTHROPIC_API_KEY`` in the subprocess env.** The API key outranks the
  subscription OAuth token in Claude Code's credential precedence — a stray key
  silently bypasses the token the user pasted. :func:`subprocess_env` strips it.

Both ``CLAUDE_CONFIG_DIR`` and the cwd resolve under the current user's data
root (via the ``current_user`` contextvar), so server mode isolates each user's
credentials *and* session store with no extra plumbing.
"""

from __future__ import annotations

import os
import shutil
from functools import lru_cache
from pathlib import Path

from app.services.current_user import user_data_root

# The selector value that picks this brain. Intercepted in the chat layer
# before the normalised provider factory is ever consulted, so it never has
# to be a member of ``ProviderId``.
BRAIN_PROVIDER = "claude_code"
BRAIN_LABEL = "Claude (subscription)"

# Default model when the user hasn't pinned one for this brain. ``None`` lets
# the Claude Code engine pick its own default; we set a concrete current model
# so behaviour is predictable across engine versions.
DEFAULT_MODEL = "claude-opus-4-8"

# In-process MCP server name; tools surface to the engine as
# ``mcp__<MCP_SERVER_NAME>__<tool>``.
MCP_SERVER_NAME = "voitta"


def _brain_root() -> Path:
    """Per-user root for everything this brain stores on disk."""
    return Path(user_data_root()) / "claude_code"


def config_dir() -> Path:
    """Per-user ``CLAUDE_CONFIG_DIR`` (credentials + session transcripts)."""
    p = _brain_root() / "config"
    p.mkdir(parents=True, exist_ok=True)
    return p


def workspace_dir() -> Path:
    """Stable per-user cwd for the engine subprocess.

    Pinned and deterministic — see the module docstring for why this must not
    drift between turns.
    """
    p = _brain_root() / "workspace"
    p.mkdir(parents=True, exist_ok=True)
    return p


def subprocess_env() -> dict[str, str]:
    """Env for the engine subprocess: inherit the parent, point
    ``CLAUDE_CONFIG_DIR`` at the per-user dir, inject the stored subscription
    token as ``CLAUDE_CODE_OAUTH_TOKEN``, and strip ``ANTHROPIC_API_KEY`` so the
    subscription token wins (the API key would otherwise outrank it)."""
    env = dict(os.environ)
    env["CLAUDE_CONFIG_DIR"] = str(config_dir())
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("ANTHROPIC_AUTH_TOKEN", None)
    # Lazy import avoids a config<->credentials import cycle.
    from app.services.agent_sdk.credentials import load_token

    token = load_token()
    if token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    else:
        env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
    return env


@lru_cache(maxsize=1)
def _cli_path_cached() -> str | None:
    """Locate the Claude Code CLI binary once.

    Checks ``$PATH`` and the common user-local install location. Cached because
    it's hit on every availability check and turn; the binary doesn't move
    within a process lifetime.
    """
    found = shutil.which("claude")
    if found:
        return found
    candidate = Path.home() / ".local" / "bin" / "claude"
    if candidate.exists():
        return str(candidate)
    return None


def cli_path() -> str | None:
    return _cli_path_cached()


def is_available() -> bool:
    """True if the brain can actually run: the Claude Code engine binary is
    on disk **and** the ``claude_agent_sdk`` Python driver is importable.

    Cheap (path lookup + spec check) so it's safe to call on the settings
    request that decides whether to offer the brain in the selector (Phase 4).
    The module check is uncached because app.installer installs the SDK after
    first boot — availability must flip to True without a restart.
    """
    import importlib.util

    if _cli_path_cached() is None:
        return False
    try:
        return importlib.util.find_spec("claude_agent_sdk") is not None
    except Exception:
        return False
