"""CLI shell behaviour: text output and exit codes."""

from __future__ import annotations

from pathlib import Path

from stratum.cli import main
from stratum.db import open_db
from stratum.migrations import SUPPORTED_SCHEMA_VERSION


def test_help_lists_commands(capsys):
    assert main(["--help"]) == 0
    out = capsys.readouterr().out
    assert "usage:" in out.lower()
    for command in (
        "init",
        "workspace",
        "status",
        "lint",
        "ingest",
        "review",
        "query",
        "commit",
        "log",
        "diff",
        "restore",
        "fsck",
        "demo",
    ):
        assert command in out


def test_version(capsys):
    assert main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == "stratum 0.1.1"


def test_unknown_command_exits_two(capsys):
    assert main(["bogus"]) == 2
    err = capsys.readouterr().err
    assert "bogus" in err
    assert "error" in err.lower()


def test_init_requires_root(capsys):
    assert main(["init"]) == 2
    err = capsys.readouterr().err
    assert "--root" in err


def test_status_without_workspace_exits_two(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("STRATUM_HOME", str(tmp_path / "empty-home"))
    monkeypatch.chdir(tmp_path)

    assert main(["status"]) == 2
    assert "workspace" in capsys.readouterr().err.lower()


def test_init_list_and_status_roundtrip(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    root = tmp_path / "notes"
    monkeypatch.setenv("STRATUM_HOME", str(home))
    monkeypatch.chdir(tmp_path)

    assert main(["init", "--root", str(root), "--name", "demo"]) == 0
    init_out = capsys.readouterr().out
    assert "demo" in init_out
    assert "Initialized" in init_out

    assert main(["workspace", "list"]) == 0
    list_out = capsys.readouterr().out
    assert "demo" in list_out
    assert "Workspaces" in list_out

    assert main(["status"]) == 0
    status_out = capsys.readouterr().out
    assert "Workspace: demo" in status_out
    assert "HEAD: none" in status_out
    assert "blocks 0" in status_out
    assert "cards 0" in status_out
    assert "reports 0" in status_out
    assert not status_out.lstrip().startswith("{")

    conn = open_db(root / ".stratum" / "stratum.db")
    try:
        assert (
            conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
            == SUPPORTED_SCHEMA_VERSION
        )
        assert conn.execute("SELECT COUNT(*) FROM blocks").fetchone()[0] == 0
    finally:
        conn.close()


def test_duplicate_init_via_cli_is_idempotent(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    root = tmp_path / "notes"
    monkeypatch.setenv("STRATUM_HOME", str(home))

    assert main(["init", "--root", str(root), "--name", "demo"]) == 0
    capsys.readouterr()
    assert main(["init", "--root", str(root), "--name", "other"]) == 0
    out = capsys.readouterr().out
    assert "already initialized" in out.lower()


def test_workspace_remove_keeps_user_files(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    root = tmp_path / "notes"
    monkeypatch.setenv("STRATUM_HOME", str(home))

    assert main(["init", "--root", str(root), "--name", "demo"]) == 0
    capsys.readouterr()

    assert main(["workspace", "remove", "demo"]) == 0
    capsys.readouterr()
    assert (root / ".stratum" / "workspace.toml").is_file()
    assert (root / ".stratum" / "stratum.db").is_file()

    assert main(["workspace", "list"]) == 0
    assert "demo" not in capsys.readouterr().out


def test_workspace_relocate_via_cli(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    root = tmp_path / "notes"
    new_root = tmp_path / "moved"
    monkeypatch.setenv("STRATUM_HOME", str(home))

    assert main(["init", "--root", str(root), "--name", "demo"]) == 0
    capsys.readouterr()

    assert (
        main(["workspace", "relocate", "demo", "--root", str(new_root)]) == 0
    )
    relocate_out = capsys.readouterr().out
    assert "demo" in relocate_out
    assert (new_root / ".stratum" / "workspace.toml").is_file()

    assert main(["status"]) == 0
    assert "Workspace: demo" in capsys.readouterr().out


def test_missing_workspace_reference_exits_two(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("STRATUM_HOME", str(tmp_path / "home"))

    assert main(["workspace", "remove", "nope"]) == 2
    assert "nope" in capsys.readouterr().err


def test_workspace_without_action_exits_two(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("STRATUM_HOME", str(tmp_path / "home"))

    assert main(["workspace"]) == 2
    assert "workspace" in capsys.readouterr().err.lower()


def test_workspace_flag_selects_non_default(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    first = tmp_path / "one"
    second = tmp_path / "two"
    monkeypatch.setenv("STRATUM_HOME", str(home))

    assert main(["init", "--root", str(first), "--name", "one"]) == 0
    assert main(["init", "--root", str(second), "--name", "two"]) == 0
    capsys.readouterr()

    assert main(["--workspace", "two", "status"]) == 0
    assert "Workspace: two" in capsys.readouterr().out


def _init_date_workspace(tmp_path, monkeypatch):
    home = tmp_path / "home"
    root = tmp_path / "notes"
    monkeypatch.setenv("STRATUM_HOME", str(home))
    monkeypatch.chdir(tmp_path)
    assert main(["init", "--root", str(root), "--name", "demo"]) == 0
    return root


def _write_source(root: Path, relative: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# {relative}\n\nSynthetic source for status coverage.\n", encoding="utf-8")
    return path


def test_status_date_reports_complete_coverage(tmp_path, monkeypatch, capsys):
    root = _init_date_workspace(tmp_path, monkeypatch)
    first = _write_source(root, "data/daily/2026-01-05/a.md")
    second = _write_source(root, "data/daily/2026-01-05/b.md")
    assert main(["ingest", "source", str(first)]) == 0
    assert main(["ingest", "source", str(second)]) == 0
    capsys.readouterr()

    assert main(["status", "--date", "2026-01-05"]) == 0
    out = capsys.readouterr().out
    assert "Date: 2026-01-05" in out
    assert "Documents: 2" in out
    assert "Coverage: complete" in out
    assert "a.md  blocks 1" in out
    assert "b.md  blocks 1" in out
    assert "{" not in out


def test_status_date_reports_missing_documents(tmp_path, monkeypatch, capsys):
    _init_date_workspace(tmp_path, monkeypatch)
    capsys.readouterr()

    assert main(["status", "--date", "2026-01-06"]) == 0
    out = capsys.readouterr().out
    assert "Documents: 0" in out
    assert "Coverage: incomplete" in out
    assert "Reason: no documents contain 2026-01-06" in out
    assert "Documents on 2026-01-06:\n  none" in out


def test_status_date_reports_stale_and_missing_blocks(tmp_path, monkeypatch, capsys):
    root = _init_date_workspace(tmp_path, monkeypatch)
    source = _write_source(root, "data/daily/2026-01-05/a.md")
    assert main(["ingest", "source", str(source)]) == 0
    capsys.readouterr()

    conn = open_db(root / ".stratum" / "stratum.db")
    try:
        conn.execute("UPDATE blocks SET status = 'stale'")
        conn.commit()
    finally:
        conn.close()

    assert main(["status", "--date", "2026-01-05"]) == 0
    out = capsys.readouterr().out
    assert "Coverage: incomplete" in out
    assert "1 stale block(s)" in out


def test_status_date_rejects_invalid_date(tmp_path, monkeypatch, capsys):
    _init_date_workspace(tmp_path, monkeypatch)
    capsys.readouterr()

    assert main(["status", "--date", "2026-02-30"]) == 2
    assert "--date" in capsys.readouterr().err


def test_status_prints_full_cmt_head(tmp_path, monkeypatch, capsys):
    root = _init_date_workspace(tmp_path, monkeypatch)
    capsys.readouterr()
    conn = open_db(root / ".stratum" / "stratum.db")
    try:
        conn.execute(
            "INSERT INTO commits "
            "(id, seq, parent_id, author, message, change_count, schema_version) "
            "VALUES ('CMT-0123456789abcdef', 1, NULL, 'tester', 'test', 0, 3)"
        )
        conn.commit()
    finally:
        conn.close()

    assert main(["status"]) == 0
    out = capsys.readouterr().out
    assert "HEAD: CMT-0123456789abcdef" in out


def test_text_output_only(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    root = tmp_path / "notes"
    monkeypatch.setenv("STRATUM_HOME", str(home))

    assert main(["init", "--root", str(root), "--name", "demo"]) == 0
    capsys.readouterr()

    for argv in (["workspace", "list"], ["status"]):
        assert main(argv) == 0
        out = capsys.readouterr().out
        assert "{" not in out
        assert "}" not in out
