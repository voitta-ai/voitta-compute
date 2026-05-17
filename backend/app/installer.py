"""First-launch installer for heavy Python packages.

These are pulled out of the bundle to keep the .app at ~280 MB instead
of ~870 MB. On first launch ``app.desktop.main`` calls :func:`is_complete`;
if False it shows :class:`app.install_window.InstallWindow` and runs
:func:`install_all` on a worker thread.

State persists via ``install_state.json`` next to the user
site-packages dir, so a partial install (network drop mid-way) resumes
on the next launch instead of redoing everything.

The state file records ``app_version`` and ``installed: {name: spec}``.
On launch:
  • app_version mismatch (binary upgraded since last successful
    install) → ``is_complete()`` returns False and ``install_all``
    wipes ``userbase/`` for a clean reinstall. python_storage/ /
    scripts/ / settings are NOT touched.
  • Pip-spec change for an individual package (e.g. ``scipy>=1.11`` →
    ``scipy>=1.13``) → only that package is reinstalled.
  • Otherwise → fast path; import probe alone decides.
"""

from __future__ import annotations

import contextlib
import importlib
import io
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Callable

# (import-name, pip-spec).
#
# - The import name is what we probe to detect "already installed."
# - The pip-spec is what we hand to ``pip install``.
# - Order matters: pip resolves later packages against what's already
#   on disk from earlier ones, avoiding redundant downloads. Roughly
#   bottom-up by dep tree (scipy/pandas before things that depend on
#   them; bokeh before panel; chromadb last because it pulls the
#   biggest grpc/onnxruntime stack).
_CORE_HEAVY_PACKAGES: list[tuple[str, str]] = [
    ("scipy",         "scipy>=1.11"),
    ("pandas",        "pandas"),
    ("matplotlib",    "matplotlib>=3.8"),
    ("plotly",        "plotly>=5.20"),
    ("bokeh",         "bokeh"),
    ("panel",         "panel>=1.5"),
    ("holoviews",     "holoviews>=1.20"),
    ("hvplot",        "hvplot>=0.10"),
    ("bokeh_fastapi", "bokeh-fastapi>=0.0.5"),
    ("tables",        "tables>=3.9"),
    ("netCDF4",       "netCDF4>=1.6"),
    ("h5netcdf",      "h5netcdf>=1.3"),
    ("hdf5plugin",    "hdf5plugin>=4.3"),
    ("xarray",        "xarray>=2024.1"),
    ("chromadb",      "chromadb>=0.5.20"),
]


def _plugin_dependencies() -> list[tuple[str, str]]:
    """Walk every plugin's manifest.json and collect its
    ``python_dependencies`` entries.

    Plugins declare deps as ``[{"import": "pyarrow", "spec":
    "pyarrow>=15.0"}, ...]``. We dedupe by import name so two plugins
    asking for the same package don't double-install.
    """
    import json as _json

    seen: dict[str, str] = {}  # import_name -> spec
    # Two layouts: source-checkout /plugins, and bundle's
    # src/voitta/resources/plugins. Same as the discovery loader.
    candidate_dirs: list[Path] = []
    here = Path(__file__).resolve()
    repo_root = here.parents[2]
    for d in (repo_root / "plugins",):
        if d.is_dir():
            candidate_dirs.append(d)
    try:
        import voitta as _voitta
        bundled = Path(_voitta.__file__).resolve().parent / "resources" / "plugins"
        if bundled.is_dir() and bundled not in candidate_dirs:
            candidate_dirs.append(bundled)
    except Exception:
        pass

    for plugins_root in candidate_dirs:
        for plugin_dir in plugins_root.iterdir():
            if not plugin_dir.is_dir() or plugin_dir.name.startswith("."):
                continue
            mf = plugin_dir / "manifest.json"
            if not mf.is_file():
                continue
            try:
                manifest = _json.loads(mf.read_text())
            except Exception:
                continue
            for d in manifest.get("python_dependencies") or []:
                if not isinstance(d, dict):
                    continue
                name = d.get("import")
                spec = d.get("spec") or name
                if isinstance(name, str) and isinstance(spec, str):
                    seen.setdefault(name, spec)
    return list(seen.items())


# Plugins extend the install set without editing core. Order: core
# packages first (heavier deps the plugin packages will resolve
# against), then plugin extras.
HEAVY_PACKAGES: list[tuple[str, str]] = _CORE_HEAVY_PACKAGES + _plugin_dependencies()

# Cute rotating commentary for the progress window — one line per
# heavy package, with a fallback for anything not in this map. Keeps
# users entertained during the ~5 min download. The spec is the
# pip-install string (e.g. "scipy>=1.11"), so we key off the import
# name where possible.
PACKAGE_BLURBS: dict[str, str] = {
    "scipy":         "scipy: every numerical operation you'll ever need…",
    "pandas":        "pandas: tables, frames, the entire data-science Sunday school…",
    "matplotlib":    "matplotlib: classic plotting library, may take a moment…",
    "plotly":        "plotly: interactive 3D + WebGL charts, great for big meshes…",
    "bokeh":         "bokeh: interactive plots — hover, zoom, the works…",
    "panel":         "panel: the dashboard framework that makes reports clickable…",
    "holoviews":     "holoviews: high-level plot grammar, sits on top of bokeh…",
    "hvplot":        "hvplot: one .hvplot() call instead of fifty matplotlib lines…",
    "bokeh_fastapi": "bokeh-fastapi: glue so Panel reports run inside our backend…",
    "tables":        "tables (PyTables): HDF5 fluent API for big arrays…",
    "netCDF4":       "netCDF4: scientific data format, lives next to HDF5…",
    "h5netcdf":      "h5netcdf: pure-h5py netCDF reader, no extra C deps…",
    "hdf5plugin":    "hdf5plugin: extra compression codecs (Blosc, BitShuffle)…",
    "xarray":        "xarray: labelled n-dimensional arrays — pandas for ndarrays…",
    "chromadb":      "chromadb: vector database for the RAG semantic index…",
}


# Progress callback signature: (current_count, total, status_label, log_line_or_None)
ProgressCb = Callable[[int, int, str, "str | None"], None]


# When the installer fails, ``install_all`` stashes a multi-line
# diagnostic here. ``app.desktop`` reads it after `_worker` returns
# False to feed real detail into the user-visible alert (failing
# package, pip exit code, last lines of pip's stderr) instead of the
# previous "Some required Python packages could not be installed."
# placeholder. Module-level state is fine — install_all is only ever
# called from one worker thread.
last_failure_detail: str = ""


def _user_site() -> Path:
    """Where pip --prefix lays out installed site-packages.

    Mirrors the resolution the launcher does in desktop_launcher.py.
    The state file lives next to ``userbase/`` (not inside it) so it
    survives a manual purge of installed packages.
    """
    prefix = os.environ.get("PIP_PREFIX")
    py_dir = f"python{sys.version_info.major}.{sys.version_info.minor}"
    if prefix:
        return Path(prefix) / "lib" / py_dir / "site-packages"
    return (
        Path.home()
        / "Library"
        / "Application Support"
        / "Voitta Bookmarklet"
        / "userbase"
        / "lib"
        / py_dir
        / "site-packages"
    )


def _user_data_root() -> Path:
    """``<user_data>`` — the directory that contains ``userbase/``,
    ``rag/``, ``python_storage/``, ``scripts/``, etc.

    Path math: ``_user_site()`` ends at
    ``userbase/lib/pythonX.Y/site-packages``; four ``parent``s back is
    the user data root.
    """
    return _user_site().parent.parent.parent.parent


def _state_path() -> Path:
    # Sits at the data root (NOT inside userbase) so a userbase wipe
    # doesn't lose partial-install resume state.
    return _user_data_root() / "install_state.json"


def _deploy_stamp_path() -> Path:
    """``<user_data>/.deployed_version`` — last app version whose
    install + RAG-build completed successfully. ``ensure_fresh_deploy``
    compares it to the current version on every boot. Lives at the
    data root so a manual ``rm -rf userbase/`` doesn't strand it."""
    return _user_data_root() / ".deployed_version"


def current_app_version() -> str:
    """Best-effort current app version. Bundle: importlib.metadata.
    Source: pyproject.toml. ``"unknown"`` if both fail."""
    try:
        from importlib.metadata import version, PackageNotFoundError
        try:
            return version("voitta")
        except PackageNotFoundError:
            pass
    except Exception:  # noqa: BLE001
        pass
    here = Path(__file__).resolve()
    for parent in (here, *here.parents):
        pp = parent / "pyproject.toml"
        if not pp.is_file():
            continue
        try:
            import tomllib
            with open(pp, "rb") as f:
                data = tomllib.load(f)
            v = data.get("project", {}).get("version")
            if isinstance(v, str) and v:
                return v
        except Exception:  # noqa: BLE001
            pass
        break
    return "unknown"


def ensure_fresh_deploy(log) -> None:
    """Single decision point: did the binary change since last successful
    install? If yes, wipe ``userbase/`` and ``rag/`` so first-run install
    + RAG-build rerun cleanly.

    Called once at boot (``app.desktop.main``) BEFORE any
    ``is_complete()`` / ``is_built()`` check. Stamp is written by
    ``mark_deploy_complete()`` only after both phases succeed.

    python_storage/, scripts/, settings.json, certs, and logs are NOT
    touched here — only the two regenerable trees.
    """
    user_root = _user_data_root()
    stamp = _deploy_stamp_path()
    current = current_app_version()
    stored = stamp.read_text(encoding="utf-8").strip() if stamp.is_file() else None
    if stored == current:
        return
    log.info(
        "deploy: stamp=%r current=%r — wiping userbase/ + rag/ + backend/certs/",
        stored, current,
    )
    for sub in ("userbase", "rag"):
        p = user_root / sub
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
    # Wipe certs too: existing pairs may have been seeded by an older
    # bundle (dev-machine signed → untrusted on recipient) or just be
    # stale. ``app.certs.provision_if_missing`` regenerates against
    # the bundled mkcert so the user gets a trusted local cert.
    certs_dir = user_root / "backend" / "certs"
    if certs_dir.is_dir():
        shutil.rmtree(certs_dir, ignore_errors=True)
    # Recreate the user site dir so PIP_PREFIX has a target.
    _user_site().mkdir(parents=True, exist_ok=True)


def mark_deploy_complete() -> None:
    """Stamp the current version after install_all + rag_build.build
    both succeed. Failing either phase leaves the stamp stale so the
    next launch wipes-and-retries automatically."""
    stamp = _deploy_stamp_path()
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text(current_app_version(), encoding="utf-8")


def is_complete() -> bool:
    """All heavy packages importable in the current sys.path?
    Boot-time ``ensure_fresh_deploy()`` already wiped userbase/ on a
    version bump, so this probe is sufficient."""
    for import_name, _ in HEAVY_PACKAGES:
        try:
            importlib.import_module(import_name)
        except ImportError:
            return False
    return True


def installed_set() -> set[str]:
    """Names recorded as installed in the on-disk state file. Used to
    resume after partial installs (network drop mid-way)."""
    p = _state_path()
    if not p.exists():
        return set()
    try:
        data = json.loads(p.read_text())
        inst = data.get("installed") if isinstance(data, dict) else None
        if isinstance(inst, list):
            return set(inst)
        return set()
    except Exception:  # noqa: BLE001
        return set()


def _save_state(installed: set[str]) -> None:
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(
        {"installed": sorted(installed), "ts": time.time()},
        indent=2,
    ))


def status_summary() -> dict:
    """Cheap snapshot for the Settings menu.

    Returns ``{installed, missing, total, last_error}``. ``installed`` and
    ``missing`` are lists of import names; ``last_error`` is None when
    ``last_failure_detail`` was never populated this run.
    """
    state = installed_set()
    expected = [name for name, _ in HEAVY_PACKAGES]
    installed = [n for n in expected if n in state]
    missing = [n for n in expected if n not in state]
    return {
        "installed": installed,
        "missing": missing,
        "total": len(expected),
        "ok": not missing,
        "last_error": last_failure_detail or None,
    }


def install_all(progress_cb: ProgressCb) -> bool:
    """Install every package whose import probe fails AND whose name
    isn't already in the state file.

    ``progress_cb(i, total, label, log_line)`` is called twice per
    package — once with ``label="Installing X…"`` before the pip run,
    once with ``label="Installed X"`` after success. ``log_line`` is
    None for the post-install ping.

    Returns True on full success, False on the first pip non-zero exit.
    """
    # State file purpose: resume after partial-install (network drop).
    # Whole-deployment wipes are handled at boot by
    # ``ensure_fresh_deploy()``, so all we do here is skip packages
    # already recorded as done.
    state = installed_set()
    todo: list[tuple[str, str]] = []
    for import_name, spec in HEAVY_PACKAGES:
        if import_name in state:
            continue
        try:
            importlib.import_module(import_name)
            state.add(import_name)
            continue
        except ImportError:
            pass
        todo.append((import_name, spec))

    _save_state(state)
    if not todo:
        return True

    total = len(todo)

    # Importing pip eagerly — done once. Each pip_main call is fully
    # self-contained but the module-load cost is non-trivial.
    from pip._internal.cli.main import main as pip_main

    global last_failure_detail
    last_failure_detail = ""

    # Pre-flight: cheap TCP probe to PyPI before we kick off pip. On a
    # live connection this is a 100ms round-trip; on an offline machine
    # it fails in <1s vs. pip's own ~30s socket-timeout cycle. The
    # error message is also actionable ("you're offline") in a way
    # pip's tail isn't.
    if not _can_reach_pypi():
        last_failure_detail = (
            "Could not reach pypi.org over the network.\n\n"
            "Voitta needs the internet to download its required\n"
            "Python packages on first launch. Connect to a network\n"
            "and relaunch the app — the installer resumes from\n"
            "where it stopped, no full restart needed."
        )
        progress_cb(0, len(todo), "Offline — cannot reach pypi.org", "!!! offline")
        print("=== pre-flight: pypi.org unreachable ===", file=sys.stderr)
        return False

    for i, (import_name, spec) in enumerate(todo):
        blurb = PACKAGE_BLURBS.get(import_name, f"Installing {import_name}…")
        progress_cb(i, total, blurb, f">>> pip install {spec}")
        # PIP_PREFIX is set by desktop_launcher to route installs into
        # ``<user_root>/userbase``. With --prefix (vs --target), pip's
        # resolver consults sys.path so deps already shipped in the
        # bundle aren't re-installed. --no-warn-script-location quiets
        # a noisy message about <userbase>/bin not being on PATH; we
        # don't need user-installed scripts on PATH, only importable
        # modules. We deliberately drop --quiet so failure output is
        # captured below.
        args = [
            "install",
            "--no-warn-script-location",
            spec,
        ]

        # Capture pip's stdout + stderr in-memory while it runs. Pip
        # writes via Python-level ``print`` and ``logging`` (which
        # streams through ``sys.stderr``), so a contextlib redirect
        # gets us most of the useful diagnostic output. The fallback
        # to ``sys.__stdout__`` keeps the launch log readable if pip
        # decides to dump a 5K wheel-resolution trace per package.
        out_buf = io.StringIO()
        err_buf = io.StringIO()
        # Pip's internal main() *can* call sys.exit() on certain edge
        # cases (resolver impasse, network kill, sub-runs of build
        # backends). SystemExit would unwind out of this thread without
        # any traceback printed, leaving the install state pointing at
        # the previous package and the user with a silent abort.
        # Catch + convert to a numeric rc, and treat any other Exception
        # the same way so we never lose the failure to a dead thread.
        try:
            with (
                contextlib.redirect_stdout(out_buf),
                contextlib.redirect_stderr(err_buf),
            ):
                rc = pip_main(args)
        except SystemExit as exc:
            rc = exc.code if isinstance(exc.code, int) else 1
        except Exception as exc:  # noqa: BLE001
            # Mirror buffered output to the log file (sys.stderr is
            # restored at this point — desktop_launcher rebinds it to
            # voitta.log on first-run startup, the redirect_stderr
            # context manager above is what's been active inside the
            # try). This is purely diagnostic: it doesn't affect the
            # user-visible flow but it lets us read the full pip
            # transcript out of voitta.log when a remote user reports
            # a failure.
            print(f"=== pip install {spec} (rc=exception) ===", file=sys.stderr)
            print(out_buf.getvalue(), file=sys.stderr)
            print(err_buf.getvalue(), file=sys.stderr)
            tail = _tail_lines(err_buf.getvalue() or out_buf.getvalue() or str(exc), 12)
            last_failure_detail = (
                f"Failed at: {import_name} ({spec})\n"
                f"Reason: {type(exc).__name__}: {exc}\n\n"
                f"Last pip output:\n{tail}"
            )
            progress_cb(
                i, total,
                f"pip crashed on {import_name}: {type(exc).__name__}",
                f"!!! pip crashed: {type(exc).__name__}: {exc}",
            )
            return False

        if rc != 0:
            # Same mirror as the exception path above — full pip
            # transcript into voitta.log so a remote failure is
            # diagnosable from the log alone.
            print(f"=== pip install {spec} (rc={rc}) ===", file=sys.stderr)
            print(out_buf.getvalue(), file=sys.stderr)
            print(err_buf.getvalue(), file=sys.stderr)
            tail = _tail_lines(err_buf.getvalue() or out_buf.getvalue(), 12)
            last_failure_detail = (
                f"Failed at: {import_name} ({spec})\n"
                f"pip exit code: {rc}\n\n"
                f"Last pip output:\n{tail}"
            )
            progress_cb(
                i, total,
                f"Failed: {import_name} (pip exit {rc})",
                f"!!! {import_name} install failed (rc={rc})\n{tail}",
            )
            return False
        # Success: log a short marker (one line, not the full transcript
        # — successful pip output is noisy and not useful in voitta.log).
        print(f"=== pip install {spec} OK ===", file=sys.stderr)
        state.add(import_name)
        _save_state(state)
        importlib.invalidate_caches()
        progress_cb(i + 1, total, f"Installed {import_name}", None)
    return True


def _can_reach_pypi(timeout_s: float = 3.0) -> bool:
    """TCP-connect probe to ``pypi.org:443``.

    A bare ``socket.create_connection`` is a much faster offline check
    than pip's full HTTPS handshake + index lookup, and it doesn't
    need any extra deps. We only fail-fast when the connection is
    *immediately* refused / DNS-unresolvable — transient flakiness
    falls through to pip's own retry, which is what we want.
    """
    import socket
    try:
        with socket.create_connection(("pypi.org", 443), timeout=timeout_s):
            return True
    except (OSError, socket.error):
        return False


def _tail_lines(text: str, n: int) -> str:
    """Last ``n`` non-empty lines, joined with newlines.

    pip's noisy output (`Looking in indexes…`, repeated `Collecting`
    lines, mostly-empty progress bars) is not what the user needs to
    see in a small alert dialog — only the actual error block at the
    very end is. ``n=12`` is enough for a typical "ERROR: Could not
    find a version that satisfies the requirement X" with one or two
    suggestion lines.
    """
    if not text:
        return "(no output)"
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    return "\n".join(lines[-n:])
