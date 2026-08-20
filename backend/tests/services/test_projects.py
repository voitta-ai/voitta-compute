"""Projects: legacy adoption, active-project latch, registry CRUD,
archive-on-delete, memory, and cross-project script listing."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services import projects, user_settings


@pytest.fixture
def proj_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated user root + settings + cold caches."""
    from app.services import current_user

    monkeypatch.setattr(current_user, "USER_DATA_ROOT", tmp_path, raising=True)
    monkeypatch.setattr(user_settings, "USER_CONFIG_DIR", tmp_path / "config")
    current_user.set_current_email(None)
    projects._reset_caches_for_tests()
    yield tmp_path
    projects._reset_caches_for_tests()


def test_adoption_moves_legacy_trees_once(proj_home: Path):
    # Pre-projects layout.
    (proj_home / "scripts" / "old-script").mkdir(parents=True)
    (proj_home / "scripts" / "old-script" / "code.py").write_text("def build(ctx): ...")
    (proj_home / "python_storage" / "cache").mkdir(parents=True)
    (proj_home / "uploads").mkdir()

    root = projects.project_data_root()
    assert root == proj_home / "projects" / "legacy"
    assert (root / "scripts" / "old-script" / "code.py").is_file()
    assert (root / "python_storage" / "cache").is_dir()
    assert (root / "uploads").is_dir()
    # Originals gone from the user root.
    assert not (proj_home / "scripts").exists()
    assert not (proj_home / "uploads").exists()
    # Idempotent: second call must not shuffle anything.
    assert projects.project_data_root() == root
    assert projects.get_project("legacy").name == "Legacy"


def test_fresh_root_adopts_to_empty_legacy(proj_home: Path):
    root = projects.project_data_root()
    assert root == proj_home / "projects" / "legacy"
    assert (root / "project.json").is_file()


def test_active_defaults_to_legacy_and_switches(proj_home: Path):
    assert projects.active_project() == "legacy"
    p = projects.create_project("Stella Park")
    assert p.slug == "stella-park"
    projects.set_active_project("stella-park")
    assert projects.active_project() == "stella-park"
    assert projects.project_data_root().name == "stella-park"
    # Persisted — survives a cache reset (i.e. process restart).
    projects._reset_caches_for_tests()
    assert projects.active_project() == "stella-park"


def test_switch_to_unknown_raises(proj_home: Path):
    with pytest.raises(projects.UnknownProject):
        projects.set_active_project("nope")


def test_dangling_active_falls_back_to_legacy(proj_home: Path):
    projects.project_data_root()  # adopt
    blob = user_settings.read()
    blob["activeProject"] = "deleted-long-ago"
    user_settings.write(blob)
    projects._reset_caches_for_tests()
    assert projects.active_project() == "legacy"


def test_create_duplicate_and_bad_names(proj_home: Path):
    projects.create_project("My Thing")
    with pytest.raises(ValueError, match="already exists"):
        projects.create_project("My Thing")
    with pytest.raises(ValueError):
        projects.create_project("   ")


def test_rename_is_display_only(proj_home: Path):
    p = projects.create_project("Alpha")
    projects.rename_project(p.slug, "Alpha Prime")
    assert projects.get_project(p.slug).name == "Alpha Prime"
    assert projects.get_project(p.slug).slug == "alpha"


def test_delete_archives_into_legacy(proj_home: Path):
    p = projects.create_project("Doomed")
    projects.set_active_project(p.slug)
    d = projects.project_dir(p.slug)
    (d / "scripts").mkdir(parents=True)
    (d / "scripts" / "keep-me.txt").write_text("data")
    projects.delete_project(p.slug)
    # Active fell back, data preserved under legacy/_archived.
    assert projects.active_project() == "legacy"
    archived = projects.project_dir("legacy") / "_archived" / "doomed"
    assert (archived / "scripts" / "keep-me.txt").is_file()
    with pytest.raises(projects.UnknownProject):
        projects.get_project("doomed")


def test_legacy_undeletable(proj_home: Path):
    projects.project_data_root()
    with pytest.raises(ValueError, match="cannot be deleted"):
        projects.delete_project("legacy")


def test_memory_append_and_prompt_block(proj_home: Path):
    projects.project_data_root()
    assert projects.project_memory() == ""
    block = projects.system_prompt_block()
    assert "Active project: Legacy" in block
    assert "project_remember" in block

    projects.append_memory("The budget sheet ID is 1AbC.")
    mem = projects.project_memory()
    assert "budget sheet ID" in mem
    assert "# Project notes — Legacy" in mem
    assert "budget sheet ID" in projects.system_prompt_block()
    with pytest.raises(ValueError):
        projects.append_memory("   ")


def test_memory_is_per_project(proj_home: Path):
    projects.append_memory("legacy fact")
    p = projects.create_project("Other")
    projects.set_active_project(p.slug)
    assert projects.project_memory() == ""
    projects.append_memory("other fact")
    projects.set_active_project("legacy")
    assert "legacy fact" in projects.project_memory()
    assert "other fact" not in projects.project_memory()


def test_cross_project_list_scripts_includes_folders(proj_home: Path):
    from app.reports import store

    projects.project_data_root()
    p = projects.create_project("Src")
    scripts_root = projects.project_dir(p.slug) / "scripts"
    (scripts_root / "flat-one").mkdir(parents=True)
    (scripts_root / "flat-one" / "code.py").write_text("def build(ctx): ...")
    (scripts_root / "folders" / "grp" / "deep-one").mkdir(parents=True)
    (scripts_root / "folders" / "grp" / "deep-one" / "code.py").write_text(
        "def build(ctx): ..."
    )
    metas = store.list_scripts(root=scripts_root)
    by_name = {m.name: m for m in metas}
    assert set(by_name) == {"flat-one", "deep-one"}
    assert by_name["deep-one"].folder_name == "grp"
    assert by_name["flat-one"].folder_name is None
