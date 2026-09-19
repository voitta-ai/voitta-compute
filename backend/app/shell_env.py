"""Recover the user's real ``PATH`` when launched from Finder/Dock.

A GUI app started by launchd inherits launchd's environment, not a shell's.
No login shell runs, so ``.zprofile``/``.zshrc`` never execute and ``PATH``
stays at the bare default::

    /usr/bin:/bin:/usr/sbin:/sbin

Nothing is lost along the way — it was never there. The effect is that every
tool the user installed outside ``/usr/bin`` is invisible to us and to any
subprocess we spawn: pyenv shims, Homebrew, nvm, ``~/.local/bin``. Most
visibly, ``python3`` resolves to macOS's own 3.9, which cannot load the
cp312 wheels in our userbase, and the agent's Bash tool hits that instead of
the user's real interpreter.

So: run the login shell once at startup and splice its ``PATH`` into ours.
This is the same trick VS Code uses (``terminal.integrated.inheritEnv``).

Only ``PATH`` is taken. Importing the shell's whole environment would clobber
the variables the launcher deliberately set — ``VOITTA_PROJECT_ROOT``,
``PIP_PREFIX``, ``CLAUDE_CONFIG_DIR`` — and silently redirect installs back
into the bundle.

Set ``VOITTA_NO_SHELL_PATH=1`` to skip it, for a shell whose rc files hang or
misbehave.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# launchd's default for a GUI session. If PATH holds nothing beyond these,
# no shell has contributed to it and it is worth asking one.
_LAUNCHD_DEFAULT = {"/usr/bin", "/bin", "/usr/sbin", "/sbin"}

# A login+interactive shell sources the rc file where PATH usually lives
# (pyenv/nvm/conda init lands in .zshrc, not .zprofile), but interactive mode
# is also what can hang or print banners — hence the timeout and the plain
# login-only retry.
_TIMEOUT_S = 8.0


def _login_shell() -> str | None:
    shell = (os.environ.get("SHELL") or "").strip()
    if shell and Path(shell).exists():
        return shell
    # launchd normally sets SHELL from the user record; fall back to it.
    try:
        import pwd

        shell = pwd.getpwuid(os.getuid()).pw_shell
        if shell and Path(shell).exists():
            return shell
    except Exception:
        pass
    return "/bin/zsh" if Path("/bin/zsh").exists() else None


def _ask_shell(shell: str, args: list[str]) -> str | None:
    """Run the shell and return whatever it prints for PATH, or None.

    The command is a bare external invocation — no quoting, no substitution —
    so it parses identically in zsh, bash, fish, dash and ksh.
    """
    try:
        proc = subprocess.run(
            [shell, *args, "/usr/bin/printenv PATH"],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_S,
            stdin=subprocess.DEVNULL,
            # Deliberately no env tinkering. Setting TERM=dumb or CI=1 to
            # quieten banners is tempting, but dotfiles routinely gate their
            # setup on exactly those ("[ -z "$CI" ] && eval $(pyenv init -)"),
            # which would suppress the init we are here to capture. Noise is
            # cheaper to parse than a silently truncated PATH.
        )
    except subprocess.TimeoutExpired:
        logger.warning("shell PATH probe timed out (%s %s)", shell, " ".join(args))
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("shell PATH probe failed (%s): %s", shell, exc)
        return None

    # rc files print banners, version notices, direnv chatter. The PATH is the
    # last line that looks like one.
    for line in reversed((proc.stdout or "").splitlines()):
        line = line.strip()
        if line.startswith("/") and Path(line.split(":", 1)[0]).is_dir():
            return line
    return None


def shell_path() -> str | None:
    """The user's ``PATH`` as their login shell reports it."""
    shell = _login_shell()
    if not shell:
        return None
    # Login+interactive first (sources .zshrc, where pyenv/nvm init lives),
    # then login-only if that hangs or an interactive rc refuses to run.
    for args in (["-l", "-i", "-c"], ["-l", "-c"]):
        found = _ask_shell(shell, args)
        if found:
            return found
    return None


def augment_path(force: bool = False) -> bool:
    """Merge the login shell's ``PATH`` into ``os.environ``.

    Returns True if ``PATH`` changed. Idempotent, and a no-op when PATH
    already carries non-system entries — i.e. when launched from a terminal,
    which is both the common dev case and the one where the shell has already
    done this work.

    Shell entries go first so the user's toolchain wins over the system's:
    that is the whole point (pyenv's 3.12 ahead of ``/usr/bin/python3`` 3.9).
    Existing entries are kept, appended, so nothing we rely on disappears.
    """
    if os.environ.get("VOITTA_NO_SHELL_PATH"):
        logger.info("shell PATH inheritance disabled via VOITTA_NO_SHELL_PATH")
        return False

    current = [p for p in (os.environ.get("PATH") or "").split(":") if p]
    if not force and any(p not in _LAUNCHD_DEFAULT for p in current):
        logger.debug("PATH already populated (%d entries) — not probing", len(current))
        return False

    found = shell_path()
    if not found:
        logger.warning(
            "could not read PATH from the login shell; staying on %s",
            os.environ.get("PATH", ""),
        )
        return False

    merged: list[str] = []
    for entry in [*found.split(":"), *current]:
        # Drop entries that no longer exist so a stale rc cannot inject
        # phantom directories ahead of the real ones.
        if entry and entry not in merged and Path(entry).is_dir():
            merged.append(entry)
    if not merged:
        return False

    new_path = ":".join(merged)
    if new_path == os.environ.get("PATH"):
        return False
    os.environ["PATH"] = new_path

    # shutil.which() caches nothing, but our own callers do — notably
    # agent_sdk.config._cli_path_cached. Clear anything already decided from
    # the old PATH so the widened one takes effect.
    _invalidate_path_caches()

    gained = [p for p in merged if p not in current]
    logger.info(
        "PATH widened from the login shell: %d entries (+%d), python3 -> %s",
        len(merged), len(gained), shutil.which("python3") or "not found",
    )
    return True


def _invalidate_path_caches() -> None:
    """Drop cached lookups that were resolved against the narrow PATH.

    Deliberately reaches through ``sys.modules`` instead of importing.
    ``app.services.agent_sdk.config`` pulls in chainlit and claude_agent_sdk,
    and this runs at startup — *before* ``ensure_fresh_deploy`` wipes
    ``userbase/`` on a version bump. Importing here would leave those two in
    ``sys.modules`` after their files were deleted, and the installer's probe
    (``importlib.import_module``) would then see the phantom, call them
    installed and skip them — leaving uvicorn unable to import
    ``chainlit.server``. If the module has not been imported yet there is no
    cache to clear: it will resolve against the widened PATH on first use.
    """
    mod = sys.modules.get("app.services.agent_sdk.config")
    cached = getattr(mod, "_cli_path_cached", None) if mod else None
    if cached is not None:
        try:
            cached.cache_clear()
        except Exception:
            pass
