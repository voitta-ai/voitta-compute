"""TLS cert provisioning via mkcert.

Used in three places:
  • Source dev (`backend/run.sh`): the shell ensure_cert() function
    handles this — Python isn't loaded yet.
  • Packaged .app first-run installer: ``provision_if_missing`` is
    called from ``_kick_install_then_serve`` so the user gets HTTPS
    out of the box.
  • Menu bar "(Re)create TLS certificates" item: ``provision``
    regenerates unconditionally, replacing any existing pair.

mkcert is treated as an external dep — we shell out to it and fall back
gracefully if it's missing. On the .app side, the heavy-packages
installer doesn't ship mkcert, so a missing tool means the user runs in
HTTP mode until they `brew install mkcert` and click the menu item.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from app.config import PROJECT_ROOT, TLS_CERT_PATH, TLS_KEY_PATH

CERTS_DIR = PROJECT_ROOT / "backend" / "certs"


class CertError(RuntimeError):
    """Provisioning failed in a way the caller should surface."""


def is_present() -> bool:
    return TLS_CERT_PATH.exists() and TLS_KEY_PATH.exists()


def _mkcert_path() -> str | None:
    return shutil.which("mkcert")


def provision(force: bool = False) -> Path:
    """Generate a 127.0.0.1+localhost cert pair via mkcert.

    Returns the cert path on success. Raises ``CertError`` if mkcert
    is unavailable or any step fails. When ``force`` is True, any
    existing pair is removed first so the new cert always wins.
    """
    mkcert = _mkcert_path()
    if mkcert is None:
        raise CertError(
            "mkcert not found on PATH. Install with `brew install mkcert` "
            "(macOS) or see https://github.com/FiloSottile/mkcert."
        )

    CERTS_DIR.mkdir(parents=True, exist_ok=True)

    if force:
        # Nuke any existing pair so the new one is picked up by
        # _detect_cert_pair on next launch.
        for pem in CERTS_DIR.glob("*.pem"):
            pem.unlink(missing_ok=True)

    # Idempotent: mkcert -install writes the local CA to the system
    # trust store. If already trusted, it's a near-instant no-op.
    install_proc = subprocess.run(
        [mkcert, "-install"],
        capture_output=True, text=True,
    )
    if install_proc.returncode != 0:
        raise CertError(
            "mkcert -install failed (system trust store update).\n"
            f"stderr: {install_proc.stderr.strip()}"
        )

    # mkcert writes <name>.pem and <name>-key.pem in cwd. The expected
    # filenames are 127.0.0.1+1.pem / 127.0.0.1+1-key.pem.
    gen_proc = subprocess.run(
        [mkcert, "127.0.0.1", "localhost"],
        cwd=CERTS_DIR,
        capture_output=True, text=True,
    )
    if gen_proc.returncode != 0:
        raise CertError(
            "mkcert generation failed.\n"
            f"stderr: {gen_proc.stderr.strip()}"
        )

    cert = CERTS_DIR / "127.0.0.1+1.pem"
    if not cert.exists():
        raise CertError(
            f"mkcert reported success but {cert.name} is missing. "
            "Inspect backend/certs/ for the actual filenames."
        )
    return cert


def provision_if_missing() -> Path | None:
    """First-run helper: only provision when no pair exists.

    Returns the cert path if anything was generated, ``None`` if a pair
    was already present or mkcert is unavailable. Failures are swallowed
    (HTTP-only fallback is acceptable).
    """
    if is_present():
        return None
    if _mkcert_path() is None:
        return None
    try:
        return provision(force=False)
    except CertError:
        return None
