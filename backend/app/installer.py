"""First-launch installer for heavy Python packages.

These are pulled out of the bundle to keep the .app smaller. On first
launch the desktop entry point calls :func:`is_complete`; if False it
shows the installer window (phase 1) and runs :func:`install_all` on
a worker thread.

State is tracked in ``<user_data>/install_state.json`` so a partial
install resumes on the next launch instead of redoing everything.

The state file records ``app_version`` and ``installed: [name, ...]``.
A version bump (new .app) wipes ``userbase/`` and ``rag/`` via
``ensure_fresh_deploy`` so pip and RAG both rerun cleanly.
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

# (import-name, pip-spec)
_CORE_HEAVY_PACKAGES: list[tuple[str, str]] = [
    ("fastmcp",    "fastmcp>=2.0"),
    ("chainlit",   "chainlit==2.11.1"),
    ("anthropic",  "anthropic>=0.39"),
    ("openai",     "openai>=1.50"),
    ("google.genai", "google-genai>=0.3"),
    ("numpy",      "numpy>=1.26"),
    ("PIL",        "pillow>=10.0"),
    ("pypdf",      "pypdf>=5.0"),
    ("bm25s",      "bm25s>=0.2.6"),
    ("Stemmer",    "PyStemmer>=2.2.0"),
    ("scipy",      "scipy>=1.11"),
    ("pandas",     "pandas"),
    ("matplotlib", "matplotlib>=3.8"),
    ("plotly",     "plotly>=5.20"),
    ("chromadb",   "chromadb>=0.5.20"),  # biggest: pulls onnxruntime + grpc
    ("rdflib",     "rdflib>=7.0"),
    ("networkx",   "networkx>=3.0"),
]


def _plugin_dependencies() -> list[tuple[str, str]]:
    """Walk plugin manifests and collect ``python_dependencies`` entries."""
    import json as _json

    seen: dict[str, str] = {}
    candidate_dirs: list[Path] = []
    here = Path(__file__).resolve()
    repo_root = here.parents[2]
    for d in (repo_root / "plugins",):
        if d.is_dir():
            candidate_dirs.append(d)
    try:
        import voitta_compute
        bundled = Path(voitta_compute.__file__).resolve().parent / "resources" / "plugins"
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


HEAVY_PACKAGES: list[tuple[str, str]] = _CORE_HEAVY_PACKAGES + _plugin_dependencies()

PACKAGE_BLURBS: dict[str, str] = {
    "chainlit":    "chainlit: chat UI framework…",
    "anthropic":   "anthropic: Claude API client…",
    "openai":      "openai: OpenAI API client…",
    "google.genai":"google-genai: Google AI client…",
    "numpy":       "numpy: numerical arrays…",
    "PIL":         "pillow: image processing…",
    "pypdf":       "pypdf: PDF reading…",
    "scipy":       "scipy: numerical computing — FFT, stats, linear algebra…",
    "pandas":      "pandas: data tables and time series…",
    "matplotlib":  "matplotlib: static and animated plots…",
    "plotly":      "plotly: interactive WebGL charts…",
    "chromadb":    "chromadb: vector database for RAG search (biggest download)…",
    "rdflib":      "rdflib: RDF knowledge graph — triples, SPARQL, OWL…",
    "networkx":    "networkx: graph algorithms and layout for flowcharts…",
}

ProgressCb = Callable[[int, int, str, "str | None"], None]

last_failure_detail: str = ""


def _user_site() -> Path:
    prefix = os.environ.get("PIP_PREFIX")
    py_dir = f"python{sys.version_info.major}.{sys.version_info.minor}"
    if prefix:
        return Path(prefix) / "lib" / py_dir / "site-packages"
    return (
        Path.home()
        / "Library" / "Application Support" / "Voitta Compute"
        / "userbase" / "lib" / py_dir / "site-packages"
    )


def _user_data_root() -> Path:
    # _user_site() = <user_data>/userbase/lib/pythonX.Y/site-packages
    # four parents up = <user_data>
    return _user_site().parent.parent.parent.parent


def _state_path() -> Path:
    return _user_data_root() / "install_state.json"


def _deploy_stamp_path() -> Path:
    return _user_data_root() / ".deployed_version"


def current_app_version() -> str:
    # Preferred: version stamped into the package at build time by build_app.sh.
    try:
        from voitta_compute import __version__ as _v
        if isinstance(_v, str) and _v and _v != "unknown":
            return _v
    except Exception:
        pass
    try:
        from importlib.metadata import version, PackageNotFoundError
        try:
            return version("voitta-compute")
        except PackageNotFoundError:
            pass
    except Exception:
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
            v = data.get("project", {}).get("version") or data.get("tool", {}).get("briefcase", {}).get("version")
            if isinstance(v, str) and v:
                return v
        except Exception:
            pass
        break
    return "unknown"


def ensure_fresh_deploy(log) -> None:
    """On a version bump, wipe userbase/ + rag/ so install + RAG rerun cleanly."""
    user_root = _user_data_root()
    stamp = _deploy_stamp_path()
    current = current_app_version()
    stored = stamp.read_text(encoding="utf-8").strip() if stamp.is_file() else None
    if stored == current:
        return
    log.info("deploy: stamp=%r current=%r — wiping userbase/", stored, current)
    p = user_root / "userbase"
    if p.is_dir():
        shutil.rmtree(p, ignore_errors=True)
    certs_dir = user_root / "backend" / "certs"
    if certs_dir.is_dir():
        shutil.rmtree(certs_dir, ignore_errors=True)
    _state_path().unlink(missing_ok=True)
    _user_site().mkdir(parents=True, exist_ok=True)


def mark_deploy_complete() -> None:
    stamp = _deploy_stamp_path()
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text(current_app_version(), encoding="utf-8")


def is_complete() -> bool:
    """All heavy packages importable in the current sys.path?"""
    for import_name, _ in HEAVY_PACKAGES:
        try:
            importlib.import_module(import_name)
        except ImportError:
            return False
    return True


def _lib_sources_dest() -> Path:
    return _user_data_root() / "lib-sources"


def _lib_sources_stamp_path() -> Path:
    return _user_data_root() / ".lib_sources_sha"


def lib_sources_need_update() -> bool:
    """True if lib-sources are absent or at a different SHA than the bundle stamp."""
    dest = _lib_sources_dest()
    stamp = _lib_sources_stamp_path()
    if not dest.is_dir() or not stamp.is_file():
        return True
    try:
        import voitta_compute
        bundled_stamp = (
            Path(voitta_compute.__file__).resolve().parent
            / "resources" / "code_sources_version.txt"
        )
        if not bundled_stamp.is_file():
            return False  # no stamp bundled — skip
        return stamp.read_text().strip() != bundled_stamp.read_text().strip()
    except Exception:
        return False


def clone_lib_sources(progress_cb: "Callable[[str], None]") -> bool:
    """Clone/update source submodules into the user data dir.

    Reads repo URLs from the bundled .gitmodules and SHAs from
    code_sources_version.txt, then does a shallow clone (or fetch+checkout)
    of each submodule at the pinned SHA.
    Returns True on success, False on any failure.
    """
    global last_failure_detail
    import configparser
    import re
    import subprocess as _sp

    try:
        import voitta_compute
        res = Path(voitta_compute.__file__).resolve().parent / "resources"
    except Exception as exc:
        last_failure_detail = f"Cannot locate bundle resources: {exc}"
        return False

    gitmodules = res / "gitmodules"
    version_txt = res / "code_sources_version.txt"

    if not gitmodules.is_file():
        progress_cb("lib-sources: no .gitmodules in bundle — skipping")
        return True

    # Parse submodule URLs from bundled gitmodules.
    cfg = configparser.RawConfigParser()
    cfg.read_string(gitmodules.read_text())
    submodules: list[tuple[str, str]] = []  # (name, url)
    for section in cfg.sections():
        m = re.match(r'submodule "(.+)"', section)
        if m:
            url = cfg.get(section, "url", fallback=None)
            if url:
                submodules.append((m.group(1), url))

    if not submodules:
        progress_cb("lib-sources: no submodules found — skipping")
        return True

    # Parse pinned SHAs from bundled code_sources_version.txt.
    # Format: " SHA path (describe)" — same as git submodule status output.
    pinned: dict[str, str] = {}
    if version_txt.is_file():
        for line in version_txt.read_text().splitlines():
            line = line.strip().lstrip("+-U")
            parts = line.split()
            if len(parts) >= 2:
                pinned[parts[1]] = parts[0]  # path → sha

    dest_root = _lib_sources_dest()
    dest_root.mkdir(parents=True, exist_ok=True)

    if not _can_reach_pypi():  # reuse network check (hits github.com too)
        last_failure_detail = "No network — cannot clone lib-sources."
        progress_cb("lib-sources: offline — skipping (RAG code corpus unavailable)")
        return True  # non-fatal: RAG will skip code corpus

    for name, url in submodules:
        dest = dest_root / name.split("/")[-1]  # e.g. lib-sources/three.js → three.js
        sha = pinned.get(name, "")
        if dest.is_dir():
            progress_cb(f"lib-sources: updating {name}…")
            try:
                _sp.run(["git", "fetch", "--depth=1", "origin", sha or "HEAD"],
                        cwd=dest, check=True, capture_output=True)
                _sp.run(["git", "checkout", sha or "FETCH_HEAD"],
                        cwd=dest, check=True, capture_output=True)
            except _sp.CalledProcessError as exc:
                progress_cb(f"lib-sources: {name} fetch failed: {exc.stderr.decode()[:200]}")
        else:
            progress_cb(f"lib-sources: cloning {name}…")
            cmd = ["git", "clone", "--depth=1", "--no-tags"]
            if sha:
                cmd += ["--no-checkout"]
            cmd += [url, str(dest)]
            try:
                _sp.run(cmd, check=True, capture_output=True)
                if sha:
                    _sp.run(["git", "fetch", "--depth=1", "origin", sha],
                            cwd=dest, check=True, capture_output=True)
                    _sp.run(["git", "checkout", sha],
                            cwd=dest, check=True, capture_output=True)
            except _sp.CalledProcessError as exc:
                err = exc.stderr.decode()[:300] if exc.stderr else str(exc)
                progress_cb(f"lib-sources: {name} clone failed: {err}")
                last_failure_detail = err
                return False

    # Write stamp so we don't re-clone on next launch.
    if version_txt.is_file():
        _lib_sources_stamp_path().write_text(version_txt.read_text())

    progress_cb("lib-sources: done")
    return True


def force_rebuild_stamps() -> None:
    """Wipe install + RAG stamps so the next setup run does everything fresh.

    Does NOT delete userbase/ or rag/ — the installer and RAG builder will
    overwrite them in-place. This means pip re-runs and RAG re-indexes without
    throwing away the existing wheels cache.
    """
    import shutil as _shutil
    from app.config import RAG_DIR

    from app.config import USER_CONFIG_DIR
    _state_path().unlink(missing_ok=True)
    _deploy_stamp_path().unlink(missing_ok=True)
    _lib_sources_stamp_path().unlink(missing_ok=True)
    (USER_CONFIG_DIR / ".code_source_hash").unlink(missing_ok=True)
    (USER_CONFIG_DIR / ".docs_content_hash").unlink(missing_ok=True)
    # Remove chroma stores so RAG builder recreates them cleanly.
    for sub in ("chroma_docs", "chroma_code", ".chroma", ".bm25", ".chroma_code"):
        p = RAG_DIR / sub
        if p.is_dir():
            _shutil.rmtree(p, ignore_errors=True)


def installed_set() -> set[str]:
    p = _state_path()
    if not p.exists():
        return set()
    try:
        data = json.loads(p.read_text())
        inst = data.get("installed") if isinstance(data, dict) else None
        return set(inst) if isinstance(inst, list) else set()
    except Exception:
        return set()


def _save_state(installed: set[str]) -> None:
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"installed": sorted(installed), "ts": time.time()}, indent=2))


def _can_reach_pypi(timeout_s: float = 3.0) -> bool:
    import socket
    try:
        with socket.create_connection(("pypi.org", 443), timeout=timeout_s):
            return True
    except (OSError, socket.error):
        return False


def _tail_lines(text: str, n: int) -> str:
    if not text:
        return "(no output)"
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    return "\n".join(lines[-n:])


def install_all(progress_cb: ProgressCb) -> bool:
    """Install every package whose import probe fails and isn't in the state file.

    ``progress_cb(current, total, label, log_line)`` — called on the worker thread.
    Returns True on full success, False on first pip failure.
    """
    global last_failure_detail
    last_failure_detail = ""

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

    if not _can_reach_pypi():
        last_failure_detail = (
            "Could not reach pypi.org.\n\n"
            "Voitta needs internet access to download required packages on "
            "first launch. Connect and relaunch — the installer resumes from "
            "where it stopped."
        )
        progress_cb(0, total, "Offline — cannot reach pypi.org", "!!! offline")
        return False

    from pip._internal.cli.main import main as pip_main

    # Redirect pip's tempdir (and that of PEP-517 build subprocesses) to a
    # path under USER_DATA_DIR. macOS App Translocation sandbox-namespaces
    # /var/folders/ so parent and child processes see different real paths for
    # the same /var/folders/... string — output.json written by the subprocess
    # is invisible to the parent. ~/Library/Application Support/ is not
    # namespaced, so both sides always resolve it to the same real path.
    import tempfile
    from app.config import USER_DATA_ROOT
    build_tmp = USER_DATA_ROOT / "build-tmp"
    build_tmp.mkdir(parents=True, exist_ok=True)
    os.environ["TMPDIR"] = str(build_tmp)
    tempfile.tempdir = str(build_tmp)

    for i, (import_name, spec) in enumerate(todo):
        blurb = PACKAGE_BLURBS.get(import_name, f"Installing {import_name}…")
        progress_cb(i, total, blurb, f">>> pip install {spec}")

        args = ["install", "--no-warn-script-location"]
        try:
            import voitta_compute
            _whl = Path(voitta_compute.__file__).resolve().parent / "resources" / "wheels"
        except Exception:
            _whl = Path(__file__).resolve().parent.parent.parent / "wheels"
        if _whl.is_dir():
            args += ["--find-links", str(_whl)]
        args.append(spec)
        out_buf = io.StringIO()
        err_buf = io.StringIO()
        try:
            with (
                contextlib.redirect_stdout(out_buf),
                contextlib.redirect_stderr(err_buf),
            ):
                rc = pip_main(args)
        except SystemExit as exc:
            rc = exc.code if isinstance(exc.code, int) else 1
        except Exception as exc:  # noqa: BLE001
            print(f"=== pip install {spec} (exception) ===", file=sys.stderr)
            print(out_buf.getvalue(), file=sys.stderr)
            print(err_buf.getvalue(), file=sys.stderr)
            tail = _tail_lines(err_buf.getvalue() or out_buf.getvalue() or str(exc), 12)
            last_failure_detail = (
                f"Failed at: {import_name} ({spec})\n"
                f"Reason: {type(exc).__name__}: {exc}\n\nLast pip output:\n{tail}"
            )
            progress_cb(i, total, f"pip crashed on {import_name}", f"!!! {type(exc).__name__}: {exc}")
            return False

        if rc != 0:
            print(f"=== pip install {spec} (rc={rc}) ===", file=sys.stderr)
            print(out_buf.getvalue(), file=sys.stderr)
            print(err_buf.getvalue(), file=sys.stderr)
            tail = _tail_lines(err_buf.getvalue() or out_buf.getvalue(), 12)
            last_failure_detail = (
                f"Failed at: {import_name} ({spec})\npip exit code: {rc}\n\nLast pip output:\n{tail}"
            )
            progress_cb(i, total, f"Failed: {import_name} (pip exit {rc})", f"!!! rc={rc}\n{tail}")
            return False

        print(f"=== pip install {spec} OK ===", file=sys.stderr)
        state.add(import_name)
        _save_state(state)
        importlib.invalidate_caches()
        progress_cb(i + 1, total, f"Installed {import_name}", None)

    return True
