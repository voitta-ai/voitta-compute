"""Canonical refs parser + ensure_local cache lookup.

The resolver code paths (vre:// / drive://) need a running MCP server
or live OAuth tokens to actually fetch — those aren't tested here.
We test the bits that are unit-testable in isolation:

  • Ref parsing: scheme/key extraction, canonicalisation, error cases.
  • Cache lookup: building a fake ``python_storage/cache/`` tree and
    confirming ensure_local finds the matching snapshot.
  • Resolver registry: dispatch goes to the right scheme handler.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


def test_parse_minimal():
    from app.services.refs import parse

    r = parse("vre://file_id=42&asset=cad_mesh")
    assert r.scheme == "vre"
    assert r.params == {"file_id": "42", "asset": "cad_mesh"}
    assert r.canonical == "vre://asset=cad_mesh&file_id=42"


def test_parse_with_slug_and_export():
    from app.services.refs import parse

    r = parse("vre://file_id=42&asset=cad_projection&slug=base-frame/rail-l&export=iso")
    # Keys sorted alphabetically; slashes preserved (they're safe in
    # the canonical encoding).
    assert r.canonical == (
        "vre://asset=cad_projection&export=iso"
        "&file_id=42&slug=base-frame/rail-l"
    )


def test_parse_drive_with_mime_export():
    from app.services.refs import parse

    r = parse("drive://file_id=1ABC&export=text/csv")
    # text/csv contains '/' — safe; we keep it readable.
    assert r.params["export"] == "text/csv"
    assert "export=text/csv" in r.canonical


def test_parse_url_decodes_values():
    from app.services.refs import parse

    # Percent-encoded ampersand inside a value must round-trip.
    r = parse("vre://file_id=42&asset=cad_mesh&slug=a%26b")
    assert r.params["slug"] == "a&b"


def test_parse_canonical_is_order_insensitive():
    from app.services.refs import parse

    a = parse("vre://file_id=42&asset=cad_mesh")
    b = parse("vre://asset=cad_mesh&file_id=42")
    assert a.canonical == b.canonical


def test_parse_rejects_no_scheme():
    from app.services.refs import parse, RefError

    with pytest.raises(RefError):
        parse("file_id=42")


def test_parse_rejects_duplicate_key():
    from app.services.refs import parse, RefError

    with pytest.raises(RefError):
        parse("vre://file_id=1&file_id=2")


def test_parse_rejects_missing_equals():
    from app.services.refs import parse, RefError

    with pytest.raises(RefError):
        parse("vre://file_id")


# ─── ensure_local cache lookup ─────────────────────────────────────────────


@pytest.fixture
def cache_root(tmp_path, monkeypatch):
    """Point ``python_storage.STORAGE_ROOT`` at a fresh cache dir."""
    from app.services import python_storage

    root = tmp_path / "cache"
    root.mkdir()
    monkeypatch.setattr(python_storage, "STORAGE_ROOT", root)
    return root


def _make_snapshot(root: Path, handle: str, ref: str | None, body: bytes = b"") -> Path:
    """Build a single-file snapshot under ``root``. ``stored_name`` is
    always set ⇒ cache hit returns the file path (matches the
    contract single-file VRE resolvers + Drive downloader produce)."""
    snap = root / f"snapshot_{handle}"
    snap.mkdir()
    meta: dict = {
        "handle": handle,
        "kind": "test",
        "stored_name": "data.bin",
    }
    if ref is not None:
        meta["origin"] = {"ref": ref}
    (snap / "meta.json").write_text(json.dumps(meta))
    (snap / "data.bin").write_bytes(body)
    return snap


def test_cache_lookup_hit_single_file_returns_file(cache_root):
    """Single-file snapshots (``stored_name`` set + the file exists)
    return the file path so scripts can ``read_bytes()`` immediately
    after the first call (which returned the same file path)."""
    from app.services import ensure_local as el

    _make_snapshot(cache_root, "py_aaa", "vre://asset=cad_mesh&file_id=42")
    path = el._find_cached("vre://asset=cad_mesh&file_id=42")
    assert path is not None
    assert path.name == "data.bin"
    assert path.is_file()


def test_cache_lookup_hit_multi_file_returns_dir(cache_root):
    """Multi-file snapshots (``stored_name`` is None) return the dir
    so scripts can enumerate variants."""
    from app.services import ensure_local as el

    snap = cache_root / "snapshot_py_bbb"
    snap.mkdir()
    (snap / "meta.json").write_text(json.dumps({
        "handle": "py_bbb",
        # No stored_name → multi-file semantics.
        "origin": {"ref": "vre://asset=cad_projection&file_id=42"},
    }))
    (snap / "front.png").write_bytes(b"")
    (snap / "top.png").write_bytes(b"")
    path = el._find_cached("vre://asset=cad_projection&file_id=42")
    assert path is not None
    assert path.name == "snapshot_py_bbb"
    assert path.is_dir()


def test_cache_lookup_miss_returns_none(cache_root):
    from app.services import ensure_local as el

    _make_snapshot(cache_root, "py_aaa", "vre://asset=cad_mesh&file_id=42")
    assert el._find_cached("vre://asset=cad_mesh&file_id=99") is None


def test_cache_lookup_ignores_snapshots_without_origin_ref(cache_root):
    from app.services import ensure_local as el

    _make_snapshot(cache_root, "py_aaa", ref=None)
    assert el._find_cached("vre://asset=cad_mesh&file_id=42") is None


def test_resolver_registry_dispatches(cache_root):
    """When the cache misses, ensure_local routes to the scheme's
    resolver. The resolver runs on the private background loop
    ensure_local manages on its own — the caller (this test thread)
    blocks on the future and gets the result back synchronously.
    """
    from app.services import ensure_local as el
    from app.services import refs

    called: dict = {}

    async def fake_resolver(parsed: refs.Ref) -> Path:
        called["scheme"] = parsed.scheme
        called["canonical"] = parsed.canonical
        path = cache_root / "snapshot_py_FAKE"
        path.mkdir()
        (path / "meta.json").write_text(json.dumps({
            "handle": "py_FAKE",
            "stored_name": "out.txt",
            "origin": {"ref": parsed.canonical},
        }))
        (path / "out.txt").write_text("hello")
        return path / "out.txt"

    el.register("testscheme", fake_resolver)
    out = el.ensure_local("testscheme://key=val")
    assert called["scheme"] == "testscheme"
    assert "key=val" in called["canonical"]
    assert out.endswith("out.txt")


def test_resolver_missing_scheme_raises(cache_root):
    from app.services import ensure_local as el

    with pytest.raises(el.EnsureLocalError):
        el.ensure_local("nonexistent://k=v")


def test_resolver_invalid_ref_raises(cache_root):
    from app.services import ensure_local as el

    with pytest.raises(el.EnsureLocalError):
        el.ensure_local("not-a-ref")
