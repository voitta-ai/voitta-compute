"""TLS cert provisioning via mkcert.

Called from the first-launch installer (phase 0) and from the
"(Re)create TLS certificates…" tray menu item.

mkcert resolution order:
  1. Binary bundled in the .app (voitta_compute/resources/bin/mkcert)
  2. System PATH (``brew install mkcert`` in dev)
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Callable

from app.config import PROJECT_ROOT, TLS_CERT_PATH, TLS_KEY_PATH

CERTS_DIR = PROJECT_ROOT / "certs"

ProgressCb = Callable[[str], None]


class CertError(RuntimeError):
    """Provisioning failed in a way the caller should surface."""


def is_present() -> bool:
    return TLS_CERT_PATH.exists() and TLS_KEY_PATH.exists()


def _mkcert_path() -> str | None:
    """Locate mkcert. Prefer the binary bundled inside the .app."""
    try:
        import voitta_compute
        bundled = Path(voitta_compute.__file__).resolve().parent / "resources" / "bin" / "mkcert"
        if bundled.is_file():
            import os, stat
            mode = bundled.stat().st_mode
            if not (mode & stat.S_IXUSR):
                bundled.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            return str(bundled)
    except Exception:  # noqa: BLE001
        pass
    return shutil.which("mkcert")


def provision(force: bool = False) -> Path:
    """Generate a 127.0.0.1+localhost cert pair via mkcert.

    Returns the cert path on success. Raises ``CertError`` on failure.
    """
    mkcert = _mkcert_path()
    if mkcert is None:
        raise CertError(
            "mkcert not found. Install with `brew install mkcert` or ensure "
            "the .app bundle includes resources/bin/mkcert."
        )

    CERTS_DIR.mkdir(parents=True, exist_ok=True)

    if force:
        for pem in CERTS_DIR.glob("*.pem"):
            pem.unlink(missing_ok=True)

    install_proc = subprocess.run(
        [mkcert, "-install"],
        capture_output=True, text=True,
    )
    if install_proc.returncode != 0:
        raise CertError(
            f"mkcert -install failed.\nstderr: {install_proc.stderr.strip()}"
        )

    gen_proc = subprocess.run(
        [mkcert, "127.0.0.1", "localhost"],
        cwd=CERTS_DIR,
        capture_output=True, text=True,
    )
    if gen_proc.returncode != 0:
        raise CertError(
            f"mkcert generation failed.\nstderr: {gen_proc.stderr.strip()}"
        )

    cert = CERTS_DIR / "127.0.0.1+1.pem"
    if not cert.exists():
        raise CertError(
            f"mkcert reported success but {cert.name} is missing in {CERTS_DIR}."
        )
    return cert


def provision_with_progress(progress_cb: ProgressCb) -> bool:
    """Run mkcert -install + cert generation, reporting to ``progress_cb``.

    Returns True on success (or if certs already exist), False on failure.
    Called from the phase-0 installer worker thread.
    """
    if is_present():
        progress_cb("TLS certificates already present — skipping.")
        return True

    mkcert = _mkcert_path()
    if mkcert is None:
        progress_cb("mkcert not found — app will run over HTTP only.")
        return True  # Soft failure: HTTP fallback is acceptable

    progress_cb("Running mkcert -install (adds local CA to trust store)…")
    try:
        proc = subprocess.run(
            [mkcert, "-install"],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            progress_cb(f"mkcert -install failed: {proc.stderr.strip()[:200]}")
            return False
    except OSError as exc:
        progress_cb(f"mkcert -install error: {exc}")
        return False

    progress_cb("Generating TLS certificate pair for 127.0.0.1 + localhost…")
    CERTS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run(
            [mkcert, "127.0.0.1", "localhost"],
            cwd=CERTS_DIR,
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            progress_cb(f"mkcert generation failed: {proc.stderr.strip()[:200]}")
            return False
    except OSError as exc:
        progress_cb(f"mkcert error: {exc}")
        return False

    if not (CERTS_DIR / "127.0.0.1+1.pem").exists():
        progress_cb("Cert file missing after mkcert run — HTTP fallback.")
        return True  # Soft failure

    progress_cb("TLS certificates ready.")
    return True
