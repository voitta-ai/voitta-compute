"""Canonical upstream-artefact references.

A *ref* is a URI that names a durable upstream artefact:

    vre://stella/magazine/test.pdf
    vre://stella/parts/base-frame.glb?asset=cad_mesh
    vre://stella/parts/rail-l?asset=cad_projection&export=iso
    drive://1AbC...XYZ
    drive://1AbC...XYZ?export=text%2Fcsv

Grammar::

    <scheme>://<authority>/<path>[?<key>=<value>(&<key>=<value>)*]

* ``authority`` — top-level namespace (VRE: indexed folder name; Drive:
  root is implicit so authority is the file ID directly)
* ``path`` — slash-separated relative path within the authority (empty
  string for single-object schemes like Drive)
* ``params`` — optional query params. Values are URL-decoded for the
  parsed form and URL-encoded for the canonical string.

Canonicalisation: authority and path are stored decoded; query param
keys are sorted alphabetically. Two refs with the same components in
different param order produce the same canonical.

Why refs:

  • Reports persist source code that runs months later. Folder/file paths
    in the upstream system are stable across re-indexing; integer IDs are
    not.
  • Two reports referencing the same upstream artefact share its
    cache entry, deduplicating disk and download cost.
  • Signed-URL TTLs (VRE's 1 h, Drive's OAuth refresh) live inside
    the resolver — never in the report.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping
from urllib.parse import quote, unquote


@dataclass(frozen=True)
class Ref:
    """A parsed upstream-artefact reference.

    ``canonical`` is the form stored in ``meta.json::origin.ref`` and
    matched on cache lookup — identical regardless of original param order.
    """

    scheme: str
    authority: str          # folder name (VRE) or top-level id (Drive)
    path: str               # relative path within authority; "" for root refs
    params: dict[str, str]  # query params, decoded
    canonical: str          # deterministic reconstructed form

    def get(self, key: str, default: str | None = None) -> str | None:
        return self.params.get(key, default)


class RefError(ValueError):
    """Raised when a ref string is malformed."""


def parse(ref: str) -> Ref:
    """Parse a ref string into a :class:`Ref`.

    Raises :class:`RefError` on any structural problem (missing ``://``,
    empty scheme, empty authority, duplicate param key).
    Unknown schemes are accepted — the resolver registry decides support.
    """
    if not isinstance(ref, str) or "://" not in ref:
        raise RefError(f"ref must start with 'scheme://': {ref!r}")
    scheme, _, rest = ref.partition("://")
    scheme = scheme.strip().lower()
    if not scheme:
        raise RefError("ref scheme is empty")
    if not all(c.isalnum() or c in "+-._ " for c in scheme):
        raise RefError(f"ref scheme not valid: {scheme!r}")

    # Split path from query params
    if "?" in rest:
        path_part, _, query = rest.partition("?")
    else:
        path_part, query = rest, ""

    # authority is the first path segment; path is the rest.
    # "vre:///file.pdf" → rest starts with "/" meaning empty authority.
    if path_part.startswith("/"):
        raise RefError(f"ref authority (folder/id) is empty: {ref!r}")
    if "/" in path_part:
        authority, _, rel_path = path_part.partition("/")
    else:
        authority, rel_path = path_part, ""

    authority = unquote(authority).strip()
    rel_path = unquote(rel_path).strip("/")

    if not authority:
        raise RefError(f"ref authority (folder/id) is empty: {ref!r}")

    # Parse query params
    params: dict[str, str] = {}
    if query:
        for pair in query.split("&"):
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

    canonical = _canonical(scheme, authority, rel_path, params)
    return Ref(scheme=scheme, authority=authority, path=rel_path,
               params=params, canonical=canonical)


def _canonical(
    scheme: str,
    authority: str,
    path: str,
    params: Mapping[str, str],
) -> str:
    """Build the deterministic canonical form.

    ``scheme://authority/path`` with sorted, percent-encoded query params.
    ``/`` is kept readable in slugs; space and other specials are encoded.
    """
    _safe = "/:.-_"
    base = f"{scheme}://{quote(authority, safe=_safe)}"
    if path:
        base += f"/{quote(path, safe=_safe)}"
    if not params:
        return base
    parts = [f"{k}={quote(v, safe=_safe)}" for k in sorted(params) for v in [params[k]]]
    return base + "?" + "&".join(parts)


def canonicalise(ref: str) -> str:
    """Convenience: parse ``ref`` and return its canonical form."""
    return parse(ref).canonical
