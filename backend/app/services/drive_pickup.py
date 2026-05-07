"""Hacky Drive download fallback: drive the user's logged-in browser to
trigger an HTML download, then move the resulting file out of the user's
Downloads folder into ``python_storage``.

Used only when:
  * Google OAuth is **not** connected (the proper API path is unavailable)
  * The user has explicitly opted in via the ``driveDownloadViaPickup``
    setting in the widget's Settings panel.

Both gates are checked at tool registration time (see
``providers/drive/tools.py``); this module assumes the caller has already
decided the pickup path is appropriate.

The pickup is racy by nature — we cannot tell which file in ``~/Downloads``
came from *our* trigger vs. the user downloading something else in the
same window. Mitigations:

  * Snapshot the directory listing **before** opening the new tab so we
    only consider files that didn't exist at trigger time.
  * Skip Chrome / Firefox in-flight markers (``*.crdownload`` / ``*.part``)
    — wait for the final filename instead.
  * Prefer files whose name contains ``name_hint`` (when supplied).
  * Hard-cap the wait to ``timeout_s`` (default 60 s) so a failed
    download doesn't pin a snapshot forever.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable


logger = logging.getLogger(__name__)

LogFn = Callable[[str], None]


def _noop_log(_msg: str) -> None:
    pass


# Markers used by Chrome (.crdownload) and Firefox (.part) for in-flight
# downloads. We don't want to grab these — wait for the final filename.
_PARTIAL_SUFFIXES = (".crdownload", ".part", ".tmp", ".download")

# Filename prefixes used by browsers/OS for in-flight downloads BEFORE
# the final filename is decided. Chrome on macOS writes to
# `.com.google.Chrome.<random>` (no extension) until it knows the
# Content-Disposition filename, then renames; Safari uses
# `<name>.download/`. Either way, files matching these prefixes are
# never the final artifact and must be skipped.
_PARTIAL_PREFIXES = (".com.google.Chrome.", ".com.apple.")

# Files we never pick up regardless of timing — clutter that browsers
# / OS write into the same folder for unrelated reasons.
_IGNORE_NAMES = (".DS_Store", "Thumbs.db", "desktop.ini")

# Polling cadence. Tight enough that a 1–2 s download still feels
# responsive; loose enough that idle waits don't burn CPU.
_POLL_INTERVAL_S = 0.25

# Default quiescence window. Bumped from 1s after observing Chrome
# adopt-too-early bugs: Chrome can pause >1s between flushes on slow
# connections, and some browser configurations write directly to the
# final filename without a `.crdownload` suffix. 3s is still responsive
# for normal-speed downloads, robust against the bad cases.
_DEFAULT_QUIESCENCE_S = 3.0


def _has_sibling_partial(target: Path) -> bool:
    """True if a partial-download sibling (``<target>.crdownload`` etc.)
    exists for ``target``. Used to refuse adoption while Chrome is
    still actively writing — covers cases where the browser writes
    chunks to the final filename in parallel with the .crdownload
    marker (rare but observed)."""
    base = target.name
    parent = target.parent
    for suffix in _PARTIAL_SUFFIXES:
        if (parent / (base + suffix)).exists():
            return True
    return False


def expand_dir(path_str: str) -> Path:
    """Resolve a user-supplied path, expanding ``~`` and env vars."""
    return Path(os.path.expandvars(os.path.expanduser(path_str))).resolve()


def snapshot_listing(dir_path: Path) -> dict[str, float]:
    """Return ``{filename: mtime}`` for every regular file currently in
    ``dir_path``. Used as the "before" baseline so we can spot files that
    appeared *after* we triggered the download. Subdirectories are
    ignored (browsers always download to the top level)."""
    if not dir_path.is_dir():
        return {}
    out: dict[str, float] = {}
    for entry in dir_path.iterdir():
        if not entry.is_file():
            continue
        if entry.name in _IGNORE_NAMES:
            continue
        try:
            out[entry.name] = entry.stat().st_mtime
        except OSError:
            continue
    return out


def _is_partial(name: str) -> bool:
    lower = name.lower()
    if any(lower.endswith(s) for s in _PARTIAL_SUFFIXES):
        return True
    if any(name.startswith(p) for p in _PARTIAL_PREFIXES):
        return True
    return False


def _score_match(name: str, hint: str | None) -> int:
    """Higher = better fit. Used to break ties when multiple new files
    appear during the wait window."""
    if not hint:
        return 0
    h = hint.lower()
    n = name.lower()
    if n == h:
        return 100
    if h in n:
        return 50
    # Token overlap (split on common separators) catches a partial hint
    # like "report 2022" vs the original "report 2022-10-18 final.dat".
    h_tokens = {t for t in h.replace(".", " ").replace("_", " ").split() if t}
    n_tokens = {t for t in n.replace(".", " ").replace("_", " ").split() if t}
    return len(h_tokens & n_tokens)


def find_recent_matching(
    dir_path: Path,
    name_hint: str,
    *,
    max_age_s: float = 600.0,
    min_score: int = 1,
) -> list[Path]:
    """Return files in ``dir_path`` modified in the last ``max_age_s``
    seconds whose filename has a positive token-overlap score with
    ``name_hint``, sorted best-first (higher score, then newer mtime).

    Used as the timeout fallback in :func:`drive_pickup_to_python_storage`
    so a file the user already downloaded (manually, or via a previous
    failed trigger) still gets picked up. The 10-minute window is long
    enough to catch the "user fiddled with permissions for a few minutes
    then retried" case but short enough to ignore yesterday's downloads.
    """
    if not dir_path.is_dir():
        return []
    cutoff = time.time() - max_age_s
    scored: list[tuple[float, int, Path]] = []
    for entry in dir_path.iterdir():
        if not entry.is_file():
            continue
        if entry.name in _IGNORE_NAMES or _is_partial(entry.name):
            continue
        try:
            stat = entry.stat()
        except OSError:
            continue
        if stat.st_mtime < cutoff:
            continue
        score = _score_match(entry.name, name_hint)
        if score < min_score:
            continue
        scored.append((stat.st_mtime, score, entry))
    # Newest first; fall back to score on identical mtime. The exact-name
    # original is usually the *oldest* of a Chrome dedup set
    # (`foo.dat`, `foo (1).dat`, `foo (2).dat`) — the user almost always
    # wants the newest one (`foo (2).dat`).
    scored.sort(key=lambda t: (-t[0], -t[1]))
    return [p for _, _, p in scored]


async def wait_for_new_file(
    dir_path: Path,
    baseline: dict[str, float],
    *,
    timeout_s: float,
    name_hint: str | None = None,
    quiescence_s: float = _DEFAULT_QUIESCENCE_S,
    log: LogFn = _noop_log,
) -> Path | None:
    """Poll ``dir_path`` until a new file appears AND has been quiescent
    (size + mtime stable) for ``quiescence_s``. Returns the path or
    ``None`` on timeout.

    The quiescence check matters because the browser may write the file
    in chunks; if we move it mid-write we get a truncated copy. Watching
    for the file size to stop changing is more portable than poking at
    the OS-specific in-flight markers.
    """
    started = time.monotonic()
    deadline = started + timeout_s
    last_seen: dict[str, tuple[int, float]] = {}  # name → (size, first_seen_ts)

    # State the watcher narrates to the log: which partials it's seeing,
    # which candidates it's tracking, and how long it's been waiting. The
    # heartbeat fires every PROGRESS_INTERVAL_S and reports the current
    # picture so a hung download isn't a black hole.
    PROGRESS_INTERVAL_S = 5.0
    next_progress = started + PROGRESS_INTERVAL_S
    seen_partials: set[str] = set()           # logged once each
    seen_deferred_siblings: set[str] = set()  # logged once each
    seen_candidates: set[str] = set()         # logged on first sighting

    log(
        f"watch start: dir={dir_path!s} "
        f"baseline={len(baseline)} files, "
        f"timeout={timeout_s:.0f}s, quiescence={quiescence_s:.1f}s, "
        f"name_hint={name_hint!r}"
    )

    while time.monotonic() < deadline:
        now = time.monotonic()
        candidates: list[tuple[int, str, Path]] = []  # (score, name, path)
        partials_now: list[str] = []
        deferred_now: list[str] = []
        tracked_now: list[tuple[str, int, float]] = []  # name, size, age

        try:
            entries = list(dir_path.iterdir())
        except FileNotFoundError:
            await asyncio.sleep(_POLL_INTERVAL_S)
            continue

        for entry in entries:
            if not entry.is_file():
                continue
            name = entry.name
            if name in _IGNORE_NAMES:
                continue
            if _is_partial(name):
                partials_now.append(name)
                # Log each partial once when we first notice it — gives
                # the user a clear "Chrome started writing" signal.
                if name not in seen_partials:
                    try:
                        sz = entry.stat().st_size
                    except OSError:
                        sz = -1
                    log(f"  saw partial: {name!r} ({sz} B)")
                    seen_partials.add(name)
                continue
            # If a partial-download sibling exists (`<name>.crdownload`
            # etc.) the browser is still actively writing — defer.
            if _has_sibling_partial(entry):
                deferred_now.append(name)
                if name not in seen_deferred_siblings:
                    log(
                        f"  deferring {name!r}: partial sibling exists, "
                        "browser is still writing"
                    )
                    seen_deferred_siblings.add(name)
                continue
            try:
                stat = entry.stat()
            except OSError:
                continue
            # Only consider files that weren't in the baseline (or were
            # touched after baseline mtime — covers the case where the
            # browser overwrote an existing same-named file).
            base_mtime = baseline.get(name)
            if base_mtime is not None and stat.st_mtime <= base_mtime + 0.5:
                continue

            size = stat.st_size
            prev = last_seen.get(name)
            if prev is None or prev[0] != size:
                # Size changed (or first sighting) — reset quiescence timer.
                if name not in seen_candidates:
                    log(f"  saw new candidate: {name!r} ({size} B)")
                    seen_candidates.add(name)
                last_seen[name] = (size, now)
                continue
            tracked_now.append((name, size, now - prev[1]))
            if now - prev[1] >= quiescence_s and size > 0:
                score = _score_match(name, name_hint)
                candidates.append((score, name, entry))

        if candidates:
            candidates.sort(key=lambda t: (-t[0], t[1]))  # best score, then name
            picked = candidates[0][2]
            elapsed = time.monotonic() - started
            log(
                f"watch hit: picked {picked.name!r} after {elapsed:.1f}s "
                f"({len(candidates)} candidate(s))"
            )
            return picked

        # Periodic heartbeat: tells the user *why* we're still waiting.
        if now >= next_progress:
            elapsed = now - started
            line = (
                f"  waiting {elapsed:.0f}s/{timeout_s:.0f}s — "
                f"partials:{len(partials_now)} "
                f"deferred:{len(deferred_now)} "
                f"tracking:{len(tracked_now)}"
            )
            if tracked_now:
                line += " (" + ", ".join(
                    f"{n}@{sz}B age={age:.1f}s"
                    for n, sz, age in tracked_now[:3]
                ) + ")"
            log(line)
            next_progress = now + PROGRESS_INTERVAL_S

        await asyncio.sleep(_POLL_INTERVAL_S)

    log(f"watch timeout after {timeout_s:.0f}s — no eligible file")
    return None


def drive_uc_download_url(file_id: str) -> str:
    """Public Drive URL that, when opened in the user's browser tab,
    triggers a normal HTML download via the user's existing Google
    session cookies. ``confirm=t`` skips the virus-scan interstitial
    for files > ~100 MB."""
    return (
        f"https://drive.google.com/uc?export=download&id={file_id}&confirm=t"
    )
