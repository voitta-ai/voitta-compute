"""Bundled-app entrypoint. py2app calls this; it then hands off to
``app.desktop.main()``. Kept as a thin shim because the env-var
override for the data dir MUST happen before any ``app.*`` import —
``app.config`` freezes ``PROJECT_ROOT`` at module load.

Path layout when frozen:
  • Bundle (read-only):
      Voitta.app/Contents/Resources/
        ├── frontend_dist/widget.js          (bundled at build time)
        ├── default_certs/127.0.0.1+1.pem    (bundled at build time)
        └── lib/python3.11/site-packages/app/...

  • User data (writable):
      ~/Library/Application Support/Voitta/
        ├── frontend/dist/widget.js          (seeded from bundle on
        │                                     first launch, refreshed
        │                                     each launch so a `.app`
        │                                     update propagates)
        ├── backend/certs/                   (same — seeded on first
        │                                     launch only; user-trusted
        │                                     certs persist)
        ├── python_storage/
        │   ├── cache/                       (download snapshots)
        │   └── {compute,reports,flows}/     (LLM-authored scripts)
        └── voitta.log
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def _is_frozen() -> bool:
    """Detect whether we're running inside a packaged .app.

    Both briefcase and py2app set ``sys.frozen`` (briefcase as a side
    effect of running under its stub launcher; py2app explicitly).
    Briefcase additionally puts the executable inside
    ``…/Contents/MacOS/`` so we can fall back to a path heuristic.
    """
    if getattr(sys, "frozen", False):
        return True
    if os.environ.get("VOITTA_FROZEN"):
        return True
    # Briefcase's stub launcher leaves us with sys.executable pointing
    # somewhere inside Voitta.app/Contents/MacOS/.
    exe = Path(sys.executable).resolve()
    return ".app/Contents/MacOS" in str(exe)


def _bundle_resources_dir() -> Path:
    """Locate the read-only resources we ship with the bundle —
    ``frontend_dist/`` and ``default_certs/`` — regardless of which
    bundler built the .app.

    Resolution order:
      1. ``voitta.__file__`` parent — briefcase puts the source under
         ``Contents/Resources/app/voitta/`` and we ship resources as
         ``…/voitta/resources/`` (auto-included via package data).
      2. ``RESOURCEPATH`` env var — set by py2app's boot stub.
      3. ``sys.executable.parent.parent/Resources`` — last-resort
         heuristic for non-frozen test harnesses.
    """
    try:
        import voitta  # type: ignore[import-not-found]
        pkg_resources = Path(voitta.__file__).resolve().parent / "resources"
        if pkg_resources.is_dir():
            return pkg_resources
    except Exception:  # noqa: BLE001
        pass
    rp = os.environ.get("RESOURCEPATH")
    if rp:
        return Path(rp)
    return Path(sys.executable).resolve().parent.parent / "Resources"


def _user_data_dir() -> Path:
    # Matches the .app's formal_name in pyproject.toml. macOS convention
    # is to use the human-readable bundle name here, even with spaces —
    # Finder hides ~/Library/Application Support/ from users by default
    # and it's accessed via Path / shell where space-quoting is routine.
    return Path.home() / "Library" / "Application Support" / "Voitta Bookmarklet"


def _seed(src: Path, dst: Path, *, overwrite: bool) -> None:
    if not src.is_dir():
        return
    dst.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        target = dst / item.name
        if target.exists() and not overwrite:
            continue
        if item.is_dir():
            shutil.copytree(item, target, dirs_exist_ok=True)
        else:
            shutil.copy2(item, target)


def _prepare_user_data_dir() -> Path:
    user_root = _user_data_dir()
    user_root.mkdir(parents=True, exist_ok=True)

    res = _bundle_resources_dir()
    # Refresh frontend bundle on every launch — a .app upgrade should
    # ship the newer widget.js.
    _seed(res / "frontend_dist", user_root / "frontend" / "dist", overwrite=True)
    # Certs are NOT seeded from the bundle. The dev-machine's mkcert
    # pair would be useless on the recipient's machine (their trust
    # store doesn't know our CA). Instead, on first launch
    # ``app.certs.provision_if_missing`` shells out to the bundled
    # mkcert binary (see ``resources/bin/mkcert``) which writes a
    # fresh CA + leaf cert into the user's own keychain.

    # python_storage + scripts dirs need to exist before app.main is
    # imported (config / services walk them at module load).
    (user_root / "python_storage").mkdir(parents=True, exist_ok=True)
    (user_root / "scripts" / "compute").mkdir(parents=True, exist_ok=True)
    (user_root / "scripts" / "reports").mkdir(parents=True, exist_ok=True)
    # User-installed pip packages live under userbase/ (created by
    # ``main()`` once it knows the Python version). The .app's signed
    # bundle is read-only — modifying it would invalidate Gatekeeper's
    # signature — so pip writes here instead, with PIP_PREFIX pointing
    # at userbase/. main() also prepends the user site-packages to
    # sys.path so anything installed there is importable on the next
    # launch (or via importlib.invalidate_caches() in the same session).
    return user_root


def _is_multiprocessing_child() -> bool:
    """True when we've been re-entered by ``multiprocessing`` spawn-
    mode worker creation. macOS defaults to spawn, which re-runs
    ``__main__`` in every worker; without an early bail-out the worker
    would instantiate a second VoittaMenuBarApp and trigger an
    'address already in use' bind on uvicorn.

    Detection (any one is enough):
      • ``MULTIPROCESSING_FORKING_DISABLE`` env var (set by spawn
        bootstrap before re-importing).
      • Argv contains the ``--multiprocessing-fork`` sentinel.
      • ``multiprocessing.parent_process()`` is non-None — only the
        true main has no parent.
    """
    if os.environ.get("MULTIPROCESSING_FORKING_DISABLE"):
        return True
    if any("--multiprocessing-fork" in a for a in sys.argv):
        return True
    try:
        import multiprocessing
        if multiprocessing.parent_process() is not None:
            return True
    except Exception:
        pass
    return False


def _launch_diag(stage: str) -> None:
    """Same shape as voitta/__main__.py's diagnostic logger — append
    a JSON record to ``voitta-launch.log``. Duplicated here (not
    imported) because importing ``voitta.__main__`` would re-run its
    top-level code in this process."""
    try:
        import datetime, json
        env_root = os.environ.get("VOITTA_PROJECT_ROOT")
        if env_root:
            base = env_root
        else:
            base = os.path.expanduser(
                "~/Library/Application Support/Voitta Bookmarklet"
            )
        os.makedirs(base, exist_ok=True)
        record = {
            "ts": datetime.datetime.now().isoformat(timespec="milliseconds"),
            "stage": stage,
            "pid": os.getpid(),
            "ppid": os.getppid(),
            "argv": sys.argv,
        }
        with open(os.path.join(base, "voitta-launch.log"), "a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        pass


def main() -> int:
    _launch_diag("desktop_launcher_main_entered")
    if _is_multiprocessing_child():
        _launch_diag("desktop_launcher_caught_mp_child_BAILING")
        # Don't start uvicorn or rumps in a worker — let the
        # ``multiprocessing.freeze_support()`` call in ``__main__``
        # handle the bootstrap. Returning 0 lets the spawn protocol
        # tear down the child if it ever reaches us.
        return 0

    # ``--localhost`` flag: skip the API-key gate. Default for the .app
    # is "on" (loopback-only listener, single user) so the user is not
    # prompted on every launch. To run the .app in LAN-shared mode,
    # launch from Terminal: ``open -a 'Voitta Bookmarklet' --args --no-localhost``
    # (or set VOITTA_LOCALHOST_MODE=0 in the environment).
    if "--no-localhost" in sys.argv:
        os.environ["VOITTA_LOCALHOST_MODE"] = "0"
    elif "--localhost" in sys.argv:
        os.environ["VOITTA_LOCALHOST_MODE"] = "1"
    else:
        # Frozen .app default = on. Source-checkout default = on too,
        # so test runs don't break unexpectedly.
        os.environ.setdefault("VOITTA_LOCALHOST_MODE", "1")
    if _is_frozen():
        user_root = _prepare_user_data_dir()
        os.environ["VOITTA_PROJECT_ROOT"] = str(user_root)

        # Make pip installs land in the user data dir, not in the
        # signed bundle. We use PIP_PREFIX (not PIP_TARGET) because
        # --target is a flat dir that pip treats as isolated — it
        # IGNORES sys.path when resolving deps, so every transitive
        # like numpy/jinja2/packaging gets duplicated even though
        # the bundle already has them. --prefix uses the standard
        # ``<prefix>/lib/pythonX.Y/site-packages`` layout and pip's
        # resolver consults sys.path against existing installs there,
        # so deps that ship in the bundle are recognised as satisfied
        # and skipped (~300 MB savings on first-run install).
        py_dir = f"python{sys.version_info.major}.{sys.version_info.minor}"
        user_prefix = user_root / "userbase"
        user_site = user_prefix / "lib" / py_dir / "site-packages"
        user_site.mkdir(parents=True, exist_ok=True)
        os.environ["PIP_PREFIX"] = str(user_prefix)
        # Insert user site near the front of sys.path so user installs
        # can override bundled versions if needed AND so the resolver
        # sees them as installed during subsequent pip runs.
        sp = str(user_site)
        if sp not in sys.path:
            sys.path.insert(0, sp)

        # Also redirect stdout/stderr to a log file — Console.app picks
        # this up via .app conventions, and double-click users can't
        # see terminal output.
        log_path = user_root / "voitta.log"
        try:
            # Force UTF-8: when py2app's launcher reroutes stdout to a
            # file, Python defaults the encoding to ASCII and any em-dash
            # / emoji in our log messages crashes with UnicodeEncodeError.
            log_fp = log_path.open("a", buffering=1, encoding="utf-8")
            sys.stdout = log_fp
            sys.stderr = log_fp
        except OSError:
            pass

    # Defer the import until after env-var setup.
    from app.desktop import main as desktop_main
    return desktop_main()


if __name__ == "__main__":
    sys.exit(main())
