"""Artifacts mutation endpoints — DELETE / PATCH / POST run.

The in-pane Finder-style browser writes through these. Each one's
allow-list must reject everything except the four canonical "unit"
shapes: snapshot dirs, script slug dirs, and run dirs. Mutating a
derived file (a meta.json, a single run output image) must be
impossible.

These tests build a synthetic PROJECT_ROOT under tmp_path and use
FastAPI's TestClient to hit the routes directly.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


@pytest.fixture
def project_root(tmp_path, monkeypatch):
    """Point ``app.main.PROJECT_ROOT`` at a fresh tmp dir without
    reloading the module (a reload would re-execute the import of
    ``panel`` and other heavy deps not present in the test venv).
    The mutation routes resolve relative paths against this binding,
    so patching it is enough.
    """
    import app.main
    import app.config
    monkeypatch.setattr(app.main, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(app.config, "PROJECT_ROOT", tmp_path)

    # Seed: snapshot dir with meta.json.
    snap = tmp_path / "python_storage" / "cache" / "snapshot_pyAAA"
    snap.mkdir(parents=True)
    (snap / "meta.json").write_text(json.dumps({
        "handle": "pyAAA",
        "kind": "drive_file",
        "stored_name": "Original.csv",
    }))
    (snap / "Original.csv").write_text("a,b\n1,2\n")

    # Seed: compute slug with code.py + a run dir.
    comp = tmp_path / "python_storage" / "compute" / "mytool"
    comp.mkdir(parents=True)
    (comp / "code.py").write_text("def run(ctx, args=None): return None\n")
    (comp / "meta.json").write_text("{}")
    run = comp / "runs" / "abcdef123456"
    run.mkdir(parents=True)
    (run / "out.png").write_bytes(b"")

    # Seed: a reports slug.
    rep = tmp_path / "python_storage" / "reports" / "myreport"
    rep.mkdir(parents=True)
    (rep / "code.py").write_text("def build(ctx): return ctx.text('hi')\n")

    return tmp_path


@pytest.fixture
def client(project_root):
    from fastapi.testclient import TestClient
    import app.main

    return TestClient(app.main.app)


# ─── DELETE ────────────────────────────────────────────────────────────────

def test_delete_snapshot_removes_dir(project_root, client):
    r = client.delete("/api/artifacts/python_storage/cache/snapshot_pyAAA")
    assert r.status_code == 200, r.text
    assert not (project_root / "python_storage" / "cache" / "snapshot_pyAAA").exists()


def test_delete_compute_slug_removes_subtree(project_root, client):
    r = client.delete("/api/artifacts/python_storage/compute/mytool")
    assert r.status_code == 200
    assert not (project_root / "python_storage" / "compute" / "mytool").exists()


def test_delete_run_dir(project_root, client):
    r = client.delete("/api/artifacts/python_storage/compute/mytool/runs/abcdef123456")
    assert r.status_code == 200
    assert not (project_root / "python_storage" / "compute" / "mytool" / "runs" / "abcdef123456").exists()
    # Slug still there.
    assert (project_root / "python_storage" / "compute" / "mytool" / "code.py").exists()


def test_delete_rejects_root_namespace(client):
    r = client.delete("/api/artifacts/python_storage")
    assert r.status_code == 400
    r = client.delete("/api/artifacts/python_storage/cache")
    assert r.status_code == 400
    r = client.delete("/api/artifacts/python_storage/compute")
    assert r.status_code == 400


def test_delete_rejects_derived_file(client):
    # meta.json inside a snapshot is derived — not a unit.
    r = client.delete("/api/artifacts/python_storage/cache/snapshot_pyAAA/meta.json")
    assert r.status_code == 400
    # code.py inside a slug is derived.
    r = client.delete("/api/artifacts/python_storage/compute/mytool/code.py")
    assert r.status_code == 400


def test_delete_rejects_traversal(client):
    r = client.delete("/api/artifacts/python_storage/../etc/passwd")
    assert r.status_code == 400


def test_delete_404_for_missing(client):
    r = client.delete("/api/artifacts/python_storage/cache/snapshot_pyNOPE")
    assert r.status_code == 404


# ─── PATCH ─────────────────────────────────────────────────────────────────

def test_patch_snapshot_renames_display_name(project_root, client):
    r = client.patch(
        "/api/artifacts/python_storage/cache/snapshot_pyAAA",
        json={"display_name": "Renamed.csv"},
    )
    assert r.status_code == 200, r.text
    meta = json.loads((project_root / "python_storage" / "cache" / "snapshot_pyAAA" / "meta.json").read_text())
    assert meta["stored_name"] == "Renamed.csv"
    # Dir is untouched — handle is canonical.
    assert (project_root / "python_storage" / "cache" / "snapshot_pyAAA").exists()


def test_patch_snapshot_rejects_empty_name(client):
    r = client.patch(
        "/api/artifacts/python_storage/cache/snapshot_pyAAA",
        json={"display_name": "  "},
    )
    assert r.status_code == 400


def test_patch_compute_slug_renames_dir(project_root, client):
    r = client.patch(
        "/api/artifacts/python_storage/compute/mytool",
        json={"slug": "newname"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["path"] == "python_storage/compute/newname"
    assert (project_root / "python_storage" / "compute" / "newname" / "code.py").exists()
    assert not (project_root / "python_storage" / "compute" / "mytool").exists()


def test_patch_slug_rejects_bad_chars(client):
    r = client.patch(
        "/api/artifacts/python_storage/compute/mytool",
        json={"slug": "Bad Slug!"},
    )
    assert r.status_code == 400


def test_patch_slug_rejects_collision(project_root, client):
    # Pre-create the target.
    (project_root / "python_storage" / "compute" / "taken").mkdir()
    r = client.patch(
        "/api/artifacts/python_storage/compute/mytool",
        json={"slug": "taken"},
    )
    assert r.status_code == 409


def test_patch_run_not_renameable(client):
    r = client.patch(
        "/api/artifacts/python_storage/compute/mytool/runs/abcdef123456",
        json={"slug": "whatever"},
    )
    assert r.status_code == 400


def test_patch_rejects_wrong_body_for_unit(client):
    # Snapshot can't take `slug`.
    r = client.patch(
        "/api/artifacts/python_storage/cache/snapshot_pyAAA",
        json={"slug": "no"},
    )
    assert r.status_code == 400


# ─── RUN ───────────────────────────────────────────────────────────────────

def test_run_holoviz_returns_iframe_path(client):
    r = client.post("/api/artifacts/python_storage/reports/myreport/run")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "holoviz"
    assert body["report_id"] == "myreport"
    assert body["path"].startswith("/panel/reports?id=myreport")
    assert "render_id" in body


def test_run_rejects_compute_slug(client):
    r = client.post("/api/artifacts/python_storage/compute/mytool/run")
    assert r.status_code == 400


def test_run_404_when_code_missing(project_root, client):
    # Empty reports slug with no code.py.
    empty = project_root / "python_storage" / "reports" / "empty"
    empty.mkdir(parents=True)
    r = client.post("/api/artifacts/python_storage/reports/empty/run")
    assert r.status_code == 404


# ─── REFS ──────────────────────────────────────────────────────────────────

def test_refs_returns_recorded_list(project_root, client):
    """``GET .../refs`` reads the sidecar last_refs.json the script
    runner writes after every run."""
    sidecar = project_root / "python_storage" / "reports" / "myreport" / "last_refs.json"
    sidecar.write_text(json.dumps({
        "refs": [
            "vre://asset=cad_mesh&file_id=42",
            "drive://file_id=1ABCxyz",
        ],
        "at": "2026-05-16T22:00:00Z",
    }))
    r = client.get("/api/artifacts/python_storage/reports/myreport/refs")
    assert r.status_code == 200
    body = r.json()
    assert body["refs"] == [
        "vre://asset=cad_mesh&file_id=42",
        "drive://file_id=1ABCxyz",
    ]
    assert body["at"] == "2026-05-16T22:00:00Z"


def test_refs_empty_when_no_sidecar(project_root, client):
    """Slug exists but hasn't been run yet → empty list, no error."""
    r = client.get("/api/artifacts/python_storage/reports/myreport/refs")
    assert r.status_code == 200
    assert r.json()["refs"] == []


def test_refs_404_when_slug_missing(project_root, client):
    r = client.get("/api/artifacts/python_storage/reports/nopey/refs")
    assert r.status_code == 404


def test_refs_rejects_non_script_unit(project_root, client):
    # Snapshots don't have refs sidecars.
    r = client.get("/api/artifacts/python_storage/cache/snapshot_pyAAA/refs")
    assert r.status_code == 400
