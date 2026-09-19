"""The installer must not trust ``sys.modules`` across a userbase wipe.

Regression for a real incident. ``ensure_fresh_deploy`` wipes ``userbase/`` on
a version bump, but anything imported earlier in the same process stays in
``sys.modules``. An early import of ``app.services.agent_sdk.config`` (added by
the shell-PATH work) pulled chainlit and claude_agent_sdk in *before* the wipe.
``install_all``'s probe then imported them straight from cache, marked both
installed and skipped them — so uvicorn came up without chainlit on disk and
died importing ``chainlit.server``. The backend never bound its port.

Two defences, one test file:

* ``_is_phantom`` — the installer notices a cached module whose files are gone.
* ``shell_env._invalidate_path_caches`` — never imports the heavy chain at all.
"""

from __future__ import annotations

import sys
import types

import pytest

from app import shell_env
from app.installer import _is_phantom


# -- _is_phantom --------------------------------------------------------------

def test_module_with_live_file_is_real(tmp_path) -> None:
    f = tmp_path / "chainlit.py"
    f.write_text("")
    mod = types.ModuleType("chainlit")
    mod.__file__ = str(f)
    assert _is_phantom(mod) is False


def test_module_whose_file_was_wiped_is_phantom(tmp_path) -> None:
    """Exactly the incident: cached module, files deleted by the wipe."""
    f = tmp_path / "chainlit.py"
    mod = types.ModuleType("chainlit")
    mod.__file__ = str(f)  # never created — stands in for the wiped userbase
    assert _is_phantom(mod) is True


def test_package_with_live_path_is_real(tmp_path) -> None:
    mod = types.ModuleType("chainlit")
    mod.__file__ = None
    mod.__path__ = [str(tmp_path)]
    assert _is_phantom(mod) is False


def test_namespace_package_with_dead_path_is_phantom(tmp_path) -> None:
    mod = types.ModuleType("chainlit")
    mod.__file__ = None
    mod.__path__ = [str(tmp_path / "gone")]
    assert _is_phantom(mod) is True


def test_builtin_style_module_is_trusted() -> None:
    """No __file__ and no __path__ (builtins) — not a wiped userbase package."""
    mod = types.ModuleType("sys")
    assert _is_phantom(mod) is False


# -- the import that caused it ------------------------------------------------

def test_invalidate_path_caches_does_not_import_the_heavy_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It must reach through sys.modules, never import.

    Importing ``agent_sdk.config`` here would re-seed chainlit and
    claude_agent_sdk into ``sys.modules`` before the wipe — the original bug.
    """
    monkeypatch.delitem(sys.modules, "app.services.agent_sdk.config", raising=False)

    def forbidden(name, *a, **k):
        if "agent_sdk" in name:
            pytest.fail(f"_invalidate_path_caches imported {name}")
        return real_import(name, *a, **k)

    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) \
        else __builtins__.__import__
    monkeypatch.setattr("builtins.__import__", forbidden)

    shell_env._invalidate_path_caches()  # must not raise, must not import

    assert "app.services.agent_sdk.config" not in sys.modules


def test_invalidate_path_caches_clears_when_already_imported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the module *is* loaded, the stale which() result is dropped."""
    calls: list[int] = []
    fake = types.ModuleType("app.services.agent_sdk.config")
    fake._cli_path_cached = types.SimpleNamespace(
        cache_clear=lambda: calls.append(1)
    )
    monkeypatch.setitem(sys.modules, "app.services.agent_sdk.config", fake)

    shell_env._invalidate_path_caches()
    assert calls == [1]
