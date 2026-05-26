"""Slug validation + safe path resolution.

Slugs MUST match ``^[a-z0-9_-]{1,64}$``. No uppercase, no slashes, no
dots — this rules out the path-traversal class of bugs without further
checks. Callers should still resolve and verify that the resolved path
lives under ``SCRIPTS_DIR`` as a belt-and-braces measure (see
:func:`resolve_under`).
"""

from __future__ import annotations

import re
from pathlib import Path

SLUG_RE = re.compile(r"^[a-z0-9_-]{1,64}$")


class InvalidSlug(ValueError):
    """Raised when a slug fails validation."""


def validate_slug(slug: str) -> str:
    if not isinstance(slug, str):
        raise InvalidSlug(f"slug must be str, got {type(slug).__name__}")
    if not SLUG_RE.fullmatch(slug):
        raise InvalidSlug(
            f"slug {slug!r} must match {SLUG_RE.pattern} "
            "(lowercase letters, digits, underscore, hyphen; 1..64 chars)"
        )
    return slug


def resolve_under(root: Path, slug: str) -> Path:
    """Return ``root/<slug>``, raising if the resolved path escapes the
    root (defence-in-depth — :func:`validate_slug` already prevents this
    syntactically, but the resolve check protects us if the regex ever
    loosens)."""
    validate_slug(slug)
    target = (root / slug).resolve()
    root_resolved = root.resolve()
    if root_resolved not in target.parents and target != root_resolved:
        raise InvalidSlug(f"slug {slug!r} resolves outside {root}")
    return target
