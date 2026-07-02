"""Bring a Chrome tab to the foreground by URL.

A bookmarklet's ``window.focus()`` is sandboxed — a background tab cannot
promote itself. So we drive Chrome from the native side via AppleScript,
matching the session's tracked URL (and title as a tiebreaker) against the
open tabs, switching to it, raising its window, and activating Chrome.

Chromium-family browsers expose an AppleScript dictionary; Safari/Firefox
differ, so this targets Chrome and its forks only. The first run triggers a
one-time macOS Automation (TCC) prompt to control the browser.
"""

from __future__ import annotations

import logging
import subprocess
import threading

log = logging.getLogger(__name__)

# Chromium-family apps that share Chrome's AppleScript dictionary, in the
# order we try them. The session doesn't record which browser it lives in,
# so we attempt each running one until a tab matches.
_BROWSERS = ("Google Chrome", "Brave Browser", "Microsoft Edge", "Chromium")

# argv: 1=app name, 2=exact URL, 3=normalized URL (no fragment), 4=title.
# Three passes, most specific first: exact URL, then normalized contains,
# then title. Each match sets the active tab, raises the window, and
# activates the app. Returns a short status string for logging.
_SCRIPT = r"""
on run argv
    set appName to item 1 of argv
    set exactURL to item 2 of argv
    set normURL to item 3 of argv
    set wantTitle to item 4 of argv
    if not (application appName is running) then return "not-running"
    -- ``using terms from`` loads Chrome's scripting dictionary at compile
    -- time (tab/URL/active tab index) while ``tell appName`` targets the
    -- actual app at runtime. Without it, a dynamic app name leaves those
    -- terms unparseable. Brave/Edge/Chromium share Chrome's dictionary.
    using terms from application "Google Chrome"
        tell application appName
            if (count of windows) is 0 then return "no-windows"
            repeat with w in every window
                set i to 0
                repeat with t in every tab of w
                    set i to i + 1
                    if (URL of t) is exactURL then
                        set active tab index of w to i
                        set index of w to 1
                        activate
                        return "exact"
                    end if
                end repeat
            end repeat
            if normURL is not "" then
                repeat with w in every window
                    set i to 0
                    repeat with t in every tab of w
                        set i to i + 1
                        if (URL of t) contains normURL then
                            set active tab index of w to i
                            set index of w to 1
                            activate
                            return "fuzzy"
                        end if
                    end repeat
                end repeat
            end if
            if wantTitle is not "" then
                repeat with w in every window
                    set i to 0
                    repeat with t in every tab of w
                        set i to i + 1
                        if (title of t) is wantTitle then
                            set active tab index of w to i
                            set index of w to 1
                            activate
                            return "title"
                        end if
                    end repeat
                end repeat
            end if
        end tell
    end using terms from
    return "no-match"
end run
"""


def _normalize(url: str | None) -> str:
    """Strip the fragment and a single trailing slash so SPA hash routes and
    cosmetic slash differences still match the open tab's URL."""
    if not url:
        return ""
    u = url.split("#", 1)[0]
    if u.endswith("/") and len(u) > 1:
        u = u[:-1]
    return u


def _run_osascript(app: str, url: str, title: str) -> str | None:
    """Run the activation script for one browser. Returns the status string,
    or None if osascript itself failed to run."""
    try:
        proc = subprocess.run(
            ["osascript", "-", app, url, _normalize(url), title or ""],
            input=_SCRIPT,
            capture_output=True,
            text=True,
            timeout=8,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("chrome_focus: osascript could not run for %s: %s", app, exc)
        return None
    out = (proc.stdout or "").strip()
    if proc.returncode != 0:
        # TCC denial, app-not-scriptable, etc. stderr is the useful part.
        log.info("chrome_focus: %s rc=%s %s", app, proc.returncode,
                 (proc.stderr or "").strip()[:200])
        return None
    return out


def focus_tab(url: str | None, title: str | None = None) -> bool:
    """Bring the browser tab showing ``url`` to the foreground.

    Tries each Chromium-family browser until one reports a match. Returns
    True if a tab was activated, False otherwise. Best-effort and non-fatal:
    never raises.
    """
    if not url:
        return False
    for app in _BROWSERS:
        result = _run_osascript(app, url, title or "")
        if result in ("exact", "fuzzy", "title"):
            log.info("chrome_focus: activated %s tab (%s) for %s", app, result, url)
            return True
    log.info("chrome_focus: no open tab matched %s", url)
    return False


def focus_tab_async(url: str | None, title: str | None = None) -> None:
    """Fire-and-forget ``focus_tab`` on a daemon thread so the UI callback
    that triggers it never blocks on osascript."""
    if not url:
        return
    threading.Thread(
        target=focus_tab,
        args=(url, title),
        name="chrome-focus",
        daemon=True,
    ).start()
