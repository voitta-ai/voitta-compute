"""Recovering the user's PATH from their login shell.

Launched from Finder/Dock the app inherits launchd's bare
``/usr/bin:/bin:/usr/sbin:/sbin`` — no login shell runs, so pyenv, Homebrew
and ``~/.local/bin`` are invisible and ``python3`` is macOS's 3.9.

These tests pin the merge rules and the safety valves. The shell itself is
mocked: spawning a real one would be slow and would depend on whatever rc
files the machine running the tests happens to have.
"""

from __future__ import annotations

import subprocess

import pytest

from app import shell_env

LAUNCHD = "/usr/bin:/bin:/usr/sbin:/sbin"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("VOITTA_NO_SHELL_PATH", raising=False)
    monkeypatch.setenv("PATH", LAUNCHD)


def _dirs(monkeypatch: pytest.MonkeyPatch, existing: set[str]) -> None:
    """Treat exactly ``existing`` as real directories."""
    monkeypatch.setattr(
        shell_env.Path, "is_dir", lambda self: str(self) in existing
    )


# -- when it runs -------------------------------------------------------------

def test_skips_when_path_already_has_non_system_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Launched from a terminal the shell has already done this work."""
    monkeypatch.setenv("PATH", f"/opt/homebrew/bin:{LAUNCHD}")
    monkeypatch.setattr(
        shell_env, "shell_path", lambda: pytest.fail("should not probe")
    )
    assert shell_env.augment_path() is False


def test_force_probes_even_with_rich_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", f"/opt/homebrew/bin:{LAUNCHD}")
    monkeypatch.setattr(shell_env, "shell_path", lambda: "/new/bin")
    _dirs(monkeypatch, {"/new/bin", "/opt/homebrew/bin", *LAUNCHD.split(":")})
    assert shell_env.augment_path(force=True) is True
    assert shell_env.os.environ["PATH"].startswith("/new/bin:")


def test_opt_out_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOITTA_NO_SHELL_PATH", "1")
    monkeypatch.setattr(
        shell_env, "shell_path", lambda: pytest.fail("should not probe")
    )
    assert shell_env.augment_path() is False
    assert shell_env.os.environ["PATH"] == LAUNCHD


def test_probe_failure_leaves_path_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shell_env, "shell_path", lambda: None)
    assert shell_env.augment_path() is False
    assert shell_env.os.environ["PATH"] == LAUNCHD


# -- how it merges ------------------------------------------------------------

def test_shell_entries_win_over_system(monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole point: pyenv's python3 must outrank /usr/bin/python3."""
    shell = "/Users/me/.pyenv/shims:/opt/homebrew/bin:/usr/bin:/bin"
    monkeypatch.setattr(shell_env, "shell_path", lambda: shell)
    _dirs(monkeypatch, set(shell.split(":")) | set(LAUNCHD.split(":")))

    assert shell_env.augment_path() is True
    entries = shell_env.os.environ["PATH"].split(":")
    assert entries[0] == "/Users/me/.pyenv/shims"
    assert entries.index("/Users/me/.pyenv/shims") < entries.index("/usr/bin")


def test_existing_entries_are_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing we already relied on may disappear — /usr/bin/git lives there."""
    monkeypatch.setattr(shell_env, "shell_path", lambda: "/opt/homebrew/bin")
    _dirs(monkeypatch, {"/opt/homebrew/bin", *LAUNCHD.split(":")})

    shell_env.augment_path()
    entries = shell_env.os.environ["PATH"].split(":")
    for system in LAUNCHD.split(":"):
        assert system in entries


def test_duplicates_collapse(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shell_env, "shell_path", lambda: f"/opt/homebrew/bin:{LAUNCHD}")
    _dirs(monkeypatch, {"/opt/homebrew/bin", *LAUNCHD.split(":")})

    shell_env.augment_path()
    entries = shell_env.os.environ["PATH"].split(":")
    assert len(entries) == len(set(entries))


def test_nonexistent_dirs_are_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stale rc must not inject phantom dirs ahead of the real ones."""
    monkeypatch.setattr(
        shell_env, "shell_path", lambda: "/gone/bin:/opt/homebrew/bin"
    )
    _dirs(monkeypatch, {"/opt/homebrew/bin", *LAUNCHD.split(":")})

    shell_env.augment_path()
    entries = shell_env.os.environ["PATH"].split(":")
    assert "/gone/bin" not in entries
    assert entries[0] == "/opt/homebrew/bin"


def test_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shell_env, "shell_path", lambda: f"/opt/homebrew/bin:{LAUNCHD}")
    _dirs(monkeypatch, {"/opt/homebrew/bin", *LAUNCHD.split(":")})

    assert shell_env.augment_path() is True
    first = shell_env.os.environ["PATH"]
    # Second call sees a rich PATH and declines to probe at all.
    assert shell_env.augment_path() is False
    assert shell_env.os.environ["PATH"] == first


# -- parsing the shell's output ----------------------------------------------

def _fake_run(stdout: str):
    def run(*_a, **_k):
        return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")

    return run


def test_parses_path_from_noisy_rc(monkeypatch: pytest.MonkeyPatch) -> None:
    """rc files print banners, version notices, direnv chatter."""
    noisy = (
        "Last login: Tue Sep 16\n"
        "direnv: loading ~/.envrc\n"
        "nvm: using node v22\n"
        "/opt/homebrew/bin:/usr/bin\n"
    )
    monkeypatch.setattr(shell_env.subprocess, "run", _fake_run(noisy))
    _dirs(monkeypatch, {"/opt/homebrew/bin", "/usr/bin"})
    assert shell_env._ask_shell("/bin/zsh", ["-l", "-i", "-c"]) == "/opt/homebrew/bin:/usr/bin"


def test_output_without_a_path_line(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shell_env.subprocess, "run", _fake_run("command not found\n"))
    _dirs(monkeypatch, set())
    assert shell_env._ask_shell("/bin/zsh", ["-l", "-i", "-c"]) is None


def test_timeout_is_survivable(monkeypatch: pytest.MonkeyPatch) -> None:
    """An rc file that hangs must not block startup."""
    def boom(*_a, **_k):
        raise subprocess.TimeoutExpired(cmd="zsh", timeout=8.0)

    monkeypatch.setattr(shell_env.subprocess, "run", boom)
    assert shell_env._ask_shell("/bin/zsh", ["-l", "-i", "-c"]) is None


def test_falls_back_to_login_only_when_interactive_hangs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Interactive sources .zshrc (where pyenv lives) but is also what hangs."""
    seen: list[list[str]] = []

    def ask(_shell: str, args: list[str]):
        seen.append(args)
        return "/opt/homebrew/bin" if "-i" not in args else None

    monkeypatch.setattr(shell_env, "_ask_shell", ask)
    monkeypatch.setenv("SHELL", "/bin/zsh")
    assert shell_env.shell_path() == "/opt/homebrew/bin"
    assert seen == [["-l", "-i", "-c"], ["-l", "-c"]]
