import json
import os
import threading

import pytest

from app.reports import store
from app.reports.slug import InvalidSlug


def test_write_then_read(scripts_root):
    meta = store.write_script("hello", "def build(ctx): pass\n")
    assert meta.name == "hello"
    assert meta.created_at and meta.updated_at
    assert store.exists("hello")
    assert store.read_code("hello") == "def build(ctx): pass\n"


def test_overwrite_preserves_created_at(scripts_root):
    m1 = store.write_script("hello", "def build(ctx): return 1\n")
    m2 = store.write_script("hello", "def build(ctx): return 2\n")
    assert m1.created_at == m2.created_at
    assert m2.updated_at >= m1.updated_at


def test_meta_schema_version(scripts_root):
    store.write_script("x", "def build(ctx): pass\n")
    raw = json.loads((scripts_root / "x" / "meta.json").read_text())
    assert raw["schema_version"] == 1
    # Required keys present.
    for k in ("name", "created_at", "updated_at"):
        assert k in raw


def test_list_excludes_dotfiles_and_partials(scripts_root):
    store.write_script("a", "def build(ctx): pass\n")
    (scripts_root / ".hidden").mkdir()
    (scripts_root / "no_code").mkdir()  # missing code.py
    metas = store.list_scripts()
    assert [m.name for m in metas] == ["a"]


def test_delete_is_idempotent(scripts_root):
    assert store.delete_script("ghost") is False
    store.write_script("real", "def build(ctx): pass\n")
    assert store.delete_script("real") is True
    assert not store.exists("real")
    # Calling again is fine.
    assert store.delete_script("real") is False


def test_atomic_write_no_partial_file(scripts_root, monkeypatch):
    """If the rename fails, the destination must be either old or
    missing — never a half-written copy."""
    # Force os.replace to fail after the temp file is written.
    real_replace = os.replace

    def boom(src, dst):
        # Simulate the rename failing — the temp file should exist
        # under the parent dir, and the destination should still match
        # whatever was there before.
        raise OSError("simulated disk error")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        store.write_script("crash", "def build(ctx): pass\n")
    # code.py must NOT exist as a partial.
    assert not (scripts_root / "crash" / "code.py").is_file()
    # Restore for the rest of the test.
    monkeypatch.setattr(os, "replace", real_replace)


def test_concurrent_writes_dont_corrupt(scripts_root):
    """Five threads racing on the same slug — final state must be
    a valid snapshot from one of them, never a torn file."""
    expected = {f"def build(ctx): return {i}\n" for i in range(5)}

    def writer(i: int) -> None:
        store.write_script("race", f"def build(ctx): return {i}\n")

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert store.read_code("race") in expected


def test_invalid_slug_rejected(scripts_root):
    with pytest.raises(InvalidSlug):
        store.write_script("BAD", "def build(ctx): pass\n")
