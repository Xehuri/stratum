"""Workspace registry, lifecycle and discovery tests."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from stratum import workspace as wsmod
from stratum.db import open_db


@pytest.fixture
def home(tmp_path: Path) -> Path:
    return tmp_path / "home"


def test_init_creates_workspace_files_and_registry(tmp_path, home):
    root = tmp_path / "notes"
    result = wsmod.init_workspace(root, name="demo", home=home)

    assert result.created is True
    ws = result.workspace
    assert ws.name == "demo"
    assert ws.workspace_id
    assert Path(ws.root) == root.resolve()
    assert (root / ".stratum" / "workspace.toml").is_file()
    assert (root / ".stratum" / "profiles").is_dir()
    assert (root / ".stratum" / "cache").is_dir()
    assert Path(ws.db_path).is_file()
    assert (home / "registry.toml").is_file()

    conn = open_db(ws.db_path)
    try:
        assert conn.execute("SELECT version FROM schema_migrations").fetchone()[0] == 1
    finally:
        conn.close()

    listed = wsmod.list_workspaces(home=home)
    assert [item.workspace_id for item in listed] == [ws.workspace_id]


def test_init_can_place_database_outside_root(tmp_path, home):
    root = tmp_path / "notes"
    db_path = tmp_path / "state" / "notes.db"
    result = wsmod.init_workspace(root, name="demo", db_path=db_path, home=home)

    assert Path(result.workspace.db_path) == db_path.resolve()
    assert db_path.is_file()
    assert not (root / ".stratum" / "stratum.db").exists()


def test_duplicate_init_is_idempotent_and_does_not_overwrite(tmp_path, home):
    root = tmp_path / "notes"
    first = wsmod.init_workspace(root, name="demo", home=home)
    first_id = first.workspace.workspace_id

    second = wsmod.init_workspace(root, name="different", home=home)

    assert second.created is False
    assert second.workspace.workspace_id == first_id
    assert second.workspace.name == "demo"
    assert len(wsmod.list_workspaces(home=home)) == 1

    meta = wsmod.load_workspace(root)
    assert meta.workspace_id == first_id
    assert meta.name == "demo"


def test_add_registers_an_existing_workspace(tmp_path, home):
    root = tmp_path / "notes"
    first = wsmod.init_workspace(root, name="demo", home=tmp_path / "other-home")

    added = wsmod.add_workspace(root, home=home)

    assert added.workspace_id == first.workspace.workspace_id
    assert [item.workspace_id for item in wsmod.list_workspaces(home=home)] == [
        added.workspace_id
    ]


def test_add_missing_metadata_is_an_error(tmp_path, home):
    empty = tmp_path / "empty"
    empty.mkdir()

    with pytest.raises(wsmod.WorkspaceError, match="workspace"):
        wsmod.add_workspace(empty, home=home)


def test_remove_only_removes_the_registry_entry(tmp_path, home):
    root = tmp_path / "notes"
    result = wsmod.init_workspace(root, name="demo", home=home)
    ws = result.workspace

    removed = wsmod.remove_workspace(ws.workspace_id, home=home)

    assert removed.workspace_id == ws.workspace_id
    assert wsmod.list_workspaces(home=home) == []
    assert (root / ".stratum" / "workspace.toml").is_file()
    assert Path(ws.db_path).is_file()
    assert root.is_dir()


def test_remove_clears_default_workspace(tmp_path, home):
    root = tmp_path / "notes"
    result = wsmod.init_workspace(root, name="demo", home=home)

    wsmod.remove_workspace(result.workspace.workspace_id, home=home)

    registry_text = (home / "registry.toml").read_text(encoding="utf-8")
    assert result.workspace.workspace_id not in registry_text


def test_relocate_updates_registry_and_metadata(tmp_path, home):
    root = tmp_path / "notes"
    result = wsmod.init_workspace(root, name="demo", home=home)
    ws = result.workspace

    new_root = tmp_path / "moved-notes"
    shutil.move(str(root), str(new_root))

    relocated = wsmod.relocate_workspace(ws.workspace_id, root=new_root, home=home)

    assert Path(relocated.root) == new_root.resolve()
    assert Path(relocated.db_path) == (new_root / ".stratum" / "stratum.db").resolve()
    assert Path(relocated.db_path).is_file()

    meta = wsmod.load_workspace(new_root)
    assert meta.workspace_id == ws.workspace_id
    assert meta.root == relocated.root
    assert meta.db_path == relocated.db_path

    listed = wsmod.list_workspaces(home=home)
    assert len(listed) == 1
    assert Path(listed[0].root) == new_root.resolve()


def test_relocate_database_path_only(tmp_path, home):
    root = tmp_path / "notes"
    result = wsmod.init_workspace(root, name="demo", home=home)
    ws = result.workspace

    new_db = tmp_path / "state" / "moved.db"
    relocated = wsmod.relocate_workspace(ws.workspace_id, db_path=new_db, home=home)

    assert Path(relocated.root) == Path(ws.root)
    assert Path(relocated.db_path) == new_db.resolve()
    assert new_db.is_file()


def test_relocate_rejects_foreign_workspace_metadata(tmp_path, home):
    first = wsmod.init_workspace(tmp_path / "one", name="one", home=home)
    second = wsmod.init_workspace(tmp_path / "two", name="two", home=home)

    with pytest.raises(wsmod.WorkspaceError, match="different workspace"):
        wsmod.relocate_workspace(
            first.workspace.workspace_id, root=tmp_path / "two", home=home
        )

    assert second.workspace.workspace_id != first.workspace.workspace_id


def test_resolve_explicit_path(tmp_path, home):
    root = tmp_path / "notes"
    result = wsmod.init_workspace(root, name="demo", home=home)

    resolved = wsmod.resolve_workspace(explicit=str(root), home=home)

    assert resolved.workspace_id == result.workspace.workspace_id


def test_resolve_explicit_name(tmp_path, home):
    result = wsmod.init_workspace(tmp_path / "notes", name="demo", home=home)

    resolved = wsmod.resolve_workspace(explicit="demo", home=home)

    assert resolved.workspace_id == result.workspace.workspace_id


def test_resolve_explicit_beats_environment(tmp_path, home, monkeypatch):
    first = wsmod.init_workspace(tmp_path / "one", name="one", home=home)
    second = wsmod.init_workspace(tmp_path / "two", name="two", home=home)
    monkeypatch.setenv("STRATUM_WORKSPACE", str(Path(second.workspace.root)))

    resolved = wsmod.resolve_workspace(explicit=str(Path(first.workspace.root)), home=home)

    assert resolved.workspace_id == first.workspace.workspace_id


def test_resolve_environment_variable(tmp_path, home, monkeypatch):
    first = wsmod.init_workspace(tmp_path / "one", name="one", home=home)
    second = wsmod.init_workspace(tmp_path / "two", name="two", home=home)
    monkeypatch.setenv("STRATUM_WORKSPACE", str(Path(second.workspace.root)))

    resolved = wsmod.resolve_workspace(home=home)

    assert resolved.workspace_id == second.workspace.workspace_id
    assert resolved.workspace_id != first.workspace.workspace_id


def test_resolve_upward_from_nested_directory(tmp_path, home, monkeypatch):
    root = tmp_path / "notes"
    result = wsmod.init_workspace(root, name="demo", home=home)
    wsmod.remove_workspace(result.workspace.workspace_id, home=home)

    nested = root / "deep" / "deeper"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)

    resolved = wsmod.resolve_workspace(home=home)

    assert resolved.workspace_id == result.workspace.workspace_id


def test_resolve_registry_default(tmp_path, home, monkeypatch):
    result = wsmod.init_workspace(tmp_path / "notes", name="demo", home=home)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    resolved = wsmod.resolve_workspace(home=home)

    assert resolved.workspace_id == result.workspace.workspace_id


def test_resolve_missing_workspace_is_an_error(tmp_path, home, monkeypatch):
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.chdir(empty)

    with pytest.raises(wsmod.WorkspaceError, match="[Nn]o workspace"):
        wsmod.resolve_workspace(home=home)
