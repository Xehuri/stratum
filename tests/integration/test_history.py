"""History CLI: commit, log, diff, restore and linearity."""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

from stratum.cli import main
from stratum.db import open_db
from stratum.workspace import init_workspace

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
SOURCE = FIXTURES / "history" / "source.md"

CLAIM_ID = "OBS-D-2026-01-05-01"
CLAIM_BODY = "Claim body for history."


def _init(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "home"
    root = tmp_path / "notes"
    root.mkdir(parents=True)
    monkeypatch.setenv("STRATUM_HOME", str(home))
    monkeypatch.chdir(tmp_path)
    init_workspace(root, name="demo")
    return root


def _db(root: Path) -> sqlite3.Connection:
    conn = open_db(root / ".stratum" / "stratum.db")
    conn.row_factory = sqlite3.Row
    return conn


def _setup(root: Path, capsys) -> tuple[str, str, str]:
    source = root / "source.md"
    shutil.copyfile(SOURCE, source)
    assert main(["ingest", "source", str(source)]) == 0
    capsys.readouterr()

    conn = _db(root)
    try:
        block_id = conn.execute(
            "SELECT id FROM blocks WHERE retrieval_type = 'structured' "
            "ORDER BY line_start LIMIT 1"
        ).fetchone()[0]
    finally:
        conn.close()

    assert main(
        [
            "add", "card", "--block", block_id, "--type", "event",
            "--body", "History card body.", "--title", "History card",
            "--author", "02-daily-review", "--date", "2026-01-05",
        ]
    ) == 0
    capsys.readouterr()

    conn = _db(root)
    try:
        card_id = conn.execute("SELECT id FROM cards").fetchone()[0]
    finally:
        conn.close()

    assert main(
        [
            "add", "claim", "--claim-id", CLAIM_ID,
            "--claim-type", "observation",
            "--period-start", "2026-01-05", "--period-end", "2026-01-06",
            "--granularity", "day", "--entity", "self",
            "--evidence", card_id, "--confidence", "medium",
            "--body", CLAIM_BODY, "--author", "02-daily-review",
        ]
    ) == 0
    capsys.readouterr()

    assert main(
        ["review", "promote", card_id, "--author", "human", "--reason", "qa"]
    ) == 0
    assert main(
        ["review", "promote", CLAIM_ID, "--author", "human", "--reason", "qa"]
    ) == 0
    capsys.readouterr()
    return claim_of(root), card_id, block_id


def claim_of(root: Path) -> str:
    return CLAIM_ID


def _commit(root: Path, capsys, message: str) -> str:
    assert main(["commit", "--author", "Axeuh", "--message", message]) == 0
    return capsys.readouterr().out


def _first_commit_id(root: Path) -> str:
    conn = _db(root)
    try:
        return str(
            conn.execute(
                "SELECT id FROM commits ORDER BY seq LIMIT 1"
            ).fetchone()[0]
        )
    finally:
        conn.close()


def _counts(root: Path) -> dict[str, int]:
    conn = _db(root)
    try:
        return {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("commits", "changes", "entity_versions", "fsck_runs")
        }
    finally:
        conn.close()


def test_commit_noop_then_change_and_log_order(tmp_path, monkeypatch, capsys):
    root = _init(tmp_path, monkeypatch)

    assert main(["commit", "--author", "Axeuh", "--message", "empty"]) == 0
    assert "nothing to commit" in capsys.readouterr().out
    assert _counts(root)["commits"] == 0

    _setup(root, capsys)
    first_out = _commit(root, capsys, "first")
    assert "Committed" in first_out
    assert "Seq: 1" in first_out
    assert "Changes: 4" in first_out

    assert main(["commit", "--author", "Axeuh", "--message", "again"]) == 0
    assert "nothing to commit" in capsys.readouterr().out

    conn = _db(root)
    try:
        first_id = str(
            conn.execute("SELECT id FROM commits ORDER BY seq").fetchone()[0]
        )
        conn.execute(
            "UPDATE claims SET body = 'changed body' WHERE id = ?",
            (CLAIM_ID,),
        )
    finally:
        conn.close()

    second_out = _commit(root, capsys, "second")
    assert "Seq: 2" in second_out
    assert "Changes: 1" in second_out

    conn = _db(root)
    try:
        rows = conn.execute(
            "SELECT id, seq, parent_id, change_count FROM commits ORDER BY seq"
        ).fetchall()
        second_id = str(rows[1]["id"])
        assert rows[1]["parent_id"] == first_id
        assert rows[0]["change_count"] == 4
        change_types = {
            row[0]
            for row in conn.execute(
                "SELECT DISTINCT entity_type FROM changes WHERE commit_id = ?",
                (first_id,),
            )
        }
        assert change_types == {"block", "card", "claim"}
    finally:
        conn.close()

    assert main(["log"]) == 0
    log_out = capsys.readouterr().out
    assert log_out.index(second_id) < log_out.index(first_id)
    assert "2 commits" in log_out
    assert "{" not in log_out

    assert main(["log", "--limit", "1"]) == 0
    limited = capsys.readouterr().out
    assert second_id in limited
    assert first_id not in limited
    assert "1 commits" in limited


def test_diff_working_state_and_between_commits(tmp_path, monkeypatch, capsys):
    root = _init(tmp_path, monkeypatch)
    _setup(root, capsys)
    _commit(root, capsys, "first")
    first_id = _first_commit_id(root)

    conn = _db(root)
    try:
        conn.execute(
            "UPDATE claims SET body = 'changed body' WHERE id = ?",
            (CLAIM_ID,),
        )
    finally:
        conn.close()

    assert main(["diff"]) == 0
    working = capsys.readouterr().out
    assert working.startswith(f"Diff {first_id}..working")
    assert f"update claim {CLAIM_ID}" in working
    assert "body" in working
    assert "{" not in working

    _commit(root, capsys, "second")
    conn = _db(root)
    try:
        second_id = str(
            conn.execute(
                "SELECT id FROM commits ORDER BY seq DESC LIMIT 1"
            ).fetchone()[0]
        )
    finally:
        conn.close()

    assert main(["diff", "--from", first_id, "--to", second_id]) == 0
    between = capsys.readouterr().out
    assert f"Diff {first_id}..{second_id}" in between
    assert "update claim" in between
    assert "1 changes" in between

    assert main(["diff", "--from", second_id, "--to", first_id]) == 0
    inverse = capsys.readouterr().out
    assert "update claim" in inverse

    assert main(["diff", "--from", first_id]) == 2
    assert "together" in capsys.readouterr().err

    assert main(["diff", "--from", "CMT-ffffffffffffffff", "--to", second_id]) == 1
    assert "not found" in capsys.readouterr().err


def test_restore_creates_compensating_commit_and_keeps_markdown(
    tmp_path, monkeypatch, capsys
):
    root = _init(tmp_path, monkeypatch)
    _setup(root, capsys)
    source = root / "source.md"
    markdown_before = source.read_bytes()
    _commit(root, capsys, "first")
    first_id = _first_commit_id(root)

    conn = _db(root)
    try:
        conn.execute(
            "UPDATE claims SET body = 'corrupt body', status = 'stale' "
            "WHERE id = ?",
            (CLAIM_ID,),
        )
    finally:
        conn.close()
    _commit(root, capsys, "second")

    assert main(
        [
            "restore", first_id, "--author", "Axeuh",
            "--reason", "back to first",
        ]
    ) == 0
    out = capsys.readouterr().out
    assert "Restored" in out
    assert "seq 3" in out

    conn = _db(root)
    try:
        claim = conn.execute(
            "SELECT body, status FROM claims WHERE id = ?", (CLAIM_ID,)
        ).fetchone()
        assert claim["body"] == CLAIM_BODY
        assert claim["status"] == "active"

        commits = conn.execute(
            "SELECT id, seq, parent_id, message FROM commits ORDER BY seq"
        ).fetchall()
        assert [row["seq"] for row in commits] == [1, 2, 3]
        assert commits[2]["parent_id"] == commits[1]["id"]
        assert first_id in commits[2]["message"]
        assert "back to first" in commits[2]["message"]

        restore_ops = [
            row[0]
            for row in conn.execute(
                "SELECT op FROM changes WHERE commit_id = ?",
                (commits[2]["id"],),
            )
        ]
        assert restore_ops == ["restore"]
    finally:
        conn.close()

    assert source.read_bytes() == markdown_before

    assert main(
        [
            "restore", first_id, "--author", "Axeuh",
            "--reason", "back to first",
        ]
    ) == 0
    assert "nothing to restore" in capsys.readouterr().out
    assert _counts(root)["commits"] == 3


def test_restore_block_status_after_source_change(tmp_path, monkeypatch, capsys):
    root = _init(tmp_path, monkeypatch)
    _setup(root, capsys)
    source = root / "source.md"
    _commit(root, capsys, "first")
    first_id = _first_commit_id(root)

    changed_source = source.read_bytes() + b"\nAppended after the commit.\n"
    source.write_bytes(changed_source)

    conn = _db(root)
    try:
        block_id = str(
            conn.execute(
                "SELECT id FROM blocks ORDER BY line_start LIMIT 1"
            ).fetchone()[0]
        )
        conn.execute(
            "UPDATE blocks SET status = 'missing', snapshot_text = 'changed text' "
            "WHERE id = ?",
            (block_id,),
        )
    finally:
        conn.close()
    _commit(root, capsys, "second")

    assert main(
        [
            "restore", first_id, "--author", "Axeuh",
            "--reason", "restore block status",
        ]
    ) == 0
    capsys.readouterr()

    conn = _db(root)
    try:
        block = conn.execute(
            "SELECT status, snapshot_text FROM blocks WHERE id = ?", (block_id,)
        ).fetchone()
        assert block["status"] == "active"
        assert block["snapshot_text"] == "changed text"
    finally:
        conn.close()
    assert source.read_bytes() == changed_source


def test_history_is_linear_and_state_tracks_head(tmp_path, monkeypatch, capsys):
    root = _init(tmp_path, monkeypatch)
    _setup(root, capsys)
    _commit(root, capsys, "first")

    conn = _db(root)
    try:
        for index in (2, 3):
            conn.execute(
                "UPDATE claims SET body = ? WHERE id = ?",
                (f"body {index}", CLAIM_ID),
            )
            conn.commit()
    finally:
        conn.close()
    _commit(root, capsys, "second")

    conn = _db(root)
    try:
        conn.execute(
            "UPDATE claims SET confidence = 'high' WHERE id = ?", (CLAIM_ID,)
        )
    finally:
        conn.close()
    _commit(root, capsys, "third")

    conn = _db(root)
    try:
        commits = conn.execute(
            "SELECT id, seq, parent_id FROM commits ORDER BY seq"
        ).fetchall()
        assert [row["seq"] for row in commits] == [1, 2, 3]
        assert commits[0]["parent_id"] is None
        assert commits[1]["parent_id"] == commits[0]["id"]
        assert commits[2]["parent_id"] == commits[1]["id"]
        assert len({row["seq"] for row in commits}) == 3

        state = {
            row["key"]: row["value"]
            for row in conn.execute("SELECT key, value FROM workspace_state")
        }
        assert state["head_commit"] == commits[2]["id"]
        assert state["head_seq"] == "3"

        assert conn.execute("SELECT COUNT(*) FROM fsck_runs").fetchone()[0] == 0
    finally:
        conn.close()


def test_history_argument_errors_are_usage_errors(tmp_path, monkeypatch, capsys):
    root = _init(tmp_path, monkeypatch)
    _setup(root, capsys)
    _commit(root, capsys, "first")

    assert main(["commit"]) == 2
    assert "required" in capsys.readouterr().err

    assert main(["commit", "--author", "  ", "--message", "m"]) == 2
    assert "author" in capsys.readouterr().err

    assert main(["commit", "--author", "a", "--message", "  "]) == 2
    assert "message" in capsys.readouterr().err

    assert main(["log", "--limit", "0"]) == 2
    assert "limit" in capsys.readouterr().err

    assert main(["restore", "--author", "a", "--reason", "r"]) == 2
    assert "required" in capsys.readouterr().err

    assert main(["restore", "CMT-ffffffffffffffff", "--author", "a", "--reason", "r"]) == 1
    assert "not found" in capsys.readouterr().err
