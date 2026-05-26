"""Stub of the source repo's drive_pickup service.

The real service powers the no-OAuth Google Drive download fallback:
it expands the user-configured Downloads dir, watches it for new
files, drives the browser to a Drive URL, and reconciles. Not ported
yet — the lone Drive tool that uses this is also gated by a settings
flag that defaults to False, so it stays hidden from the LLM in this
build.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


class NotConfigured(RuntimeError):
    pass


def expand_dir(path_str: str) -> Path:
    """Resolve ``~`` and env vars. Doesn't require the real service."""
    import os
    return Path(os.path.expanduser(os.path.expandvars(path_str)))


def drive_uc_download_url(file_id: str, account_index: int = 0) -> str:
    """Same URL format the real service uses — pure derivation."""
    base = f"https://drive.google.com/uc?id={file_id}&export=download"
    if account_index:
        base = f"https://drive.google.com/u/{account_index}/uc?id={file_id}&export=download"
    return base


def snapshot_listing(_dl_dir: Path) -> dict[str, float]:
    raise NotConfigured("drive_pickup isn't wired in this build")


async def wait_for_new_file(*_args: Any, **_kwargs: Any) -> Path:
    raise NotConfigured("drive_pickup isn't wired in this build")


async def find_recent_matching(*_args: Any, **_kwargs: Any) -> Path | None:
    return None


def register_modal(*_args: Any, **_kwargs: Any) -> None:
    return None


def is_cancelled(_session_id: str) -> bool:
    return False


def clear_cancel(_session_id: str) -> None:
    return None
