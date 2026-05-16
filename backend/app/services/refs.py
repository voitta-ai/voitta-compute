"""Canonical upstream-artefact references.

A *ref* is a URI-shaped string that names a durable upstream artefact:

    vre://file_id=42&asset=cad_mesh&slug=base-frame
    drive://file_id=1AbC...XYZ
    drive://file_id=1AbC...XYZ&export=text/csv

The grammar is intentionally simple:

    <scheme>://<key>=<value>(&<key>=<value>)*

(No host, no path, no fragment — the keys are the entire payload. The
``//`` is kept so it parses with stdlib URL tools when needed.)

Why refs:

  • Reports persist source code that runs months later. Local
    ``py_<handle>`` strings are ephemeral; the upstream id is stable.
  • Two reports referencing the same upstream artefact share its
    cache entry, deduplicating disk and download cost.
  • Signed-URL TTLs (VRE's 1 h, Drive's OAuth refresh) live inside
    the resolver — never in the report.

Canonicalisation: keys are sorted alphabetically when forming the
canonical string. ``vre://asset=original&file_id=42`` and
``vre://file_id=42&asset=original`` produce the same canonical, so
match-by-canonical is order-insensitive. Values are URL-decoded for
the parsed form and URL-encoded for the canonical string — using the
same encoding as :mod:`urllib.parse.quote`/``unquote``. ``&`` and
``=`` inside values must be percent-encoded (``%26`` / ``%3D``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping
from urllib.parse import quote, unquote


@dataclass(frozen=True)
class Ref:
    """A parsed upstream-artefact reference.

    ``canonical`` is the form we store in ``meta.json::origin.ref`` and
    match against on cache lookup — it's the same Ref always, regardless
    of which order the original string had its keys in.
    """

    scheme: str
    params: dict[str, str]
    canonical: str

    def get(self, key: str, default: str | None = None) -> str | None:
        return self.params.get(key, default)


class RefError(ValueError):
    """Raised when a ref string is malformed."""


def parse(ref: str) -> Ref:
    """Parse a ref string into a :class:`Ref`.

    Raises :class:`RefError` on any structural problem (missing
    ``://``, empty scheme, malformed key=value pair, duplicate key).
    Unknown schemes are accepted — the caller's resolver registry
    decides what's supported.
    """
    if not isinstance(ref, str) or "://" not in ref:
        raise RefError(f"ref must look like 'scheme://k=v&...': {ref!r}")
    scheme, _, rest = ref.partition("://")
    scheme = scheme.strip().lower()
    if not scheme:
        raise RefError("ref scheme is empty")
    if not scheme.isidentifier() and not all(c.isalnum() or c in "+-." for c in scheme):
        raise RefError(f"ref scheme not a valid scheme: {scheme!r}")
    params: dict[str, str] = {}
    if rest:
        for pair in rest.split("&"):
            if not pair:
                continue
            if "=" not in pair:
                raise RefError(f"ref param missing '=': {pair!r}")
            k, _, v = pair.partition("=")
            k = k.strip()
            if not k:
                raise RefError(f"ref param has empty key: {pair!r}")
            if k in params:
                raise RefError(f"ref param {k!r} appears more than once")
            params[k] = unquote(v)
    return Ref(scheme=scheme, params=params, canonical=_canonical(scheme, params))


def _canonical(scheme: str, params: Mapping[str, str]) -> str:
    """Build the deterministic canonical form. Keys sorted; values
    percent-encoded for ``&``/``=``/``%``/space."""
    if not params:
        return f"{scheme}://"
    parts = []
    for k in sorted(params):
        # quote with no safe chars beyond the alnum + a tiny set we want
        # readable in logs (``/`` is common in VRE slugs and survives
        # transport without conflict, so we keep it).
        parts.append(f"{k}={quote(params[k], safe='/:.-_')}")
    return f"{scheme}://" + "&".join(parts)


def canonicalise(ref: str) -> str:
    """Convenience: parse ``ref`` and return its canonical form."""
    return parse(ref).canonical
