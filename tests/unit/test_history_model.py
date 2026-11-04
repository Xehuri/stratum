"""Unit-level checks for the linear history model."""

from __future__ import annotations

import json
import re
import sqlite3

import pytest

from stratum.db import open_db
from stratum.errors import UsageProblem
from stratum.history import (
    commit,
    diff_changes,
    list_commits,
    restore,
)

CLAIM_ID = "OBS-D-2026-01-05-01"


def _open(tmp_path):
    return open_db(tmp_path / "history.db")


def _seed_claim(
    conn: sqlite3.Connection,
    claim_id: str = CLAIM_ID,
    *,
    body: str = "claim body",
    status: str = "draft",
) -> None:
    conn.execute(
        """
        INSERT INTO claims (
            id, claim_type, period_start, period_end, granularity,
            entities_json, body, confidence, status, content_sha256
        ) VALUES (?, 'observation', '2026-01-05', '2026-01-06', 'day',
                  '["self"]', ?, 'medium', ?, 'seed-sha')
        """,
        (claim_id, body, status),
    )


def _seed_block(
    conn: sqlite3.Connection,
    *,
    block_id: str = "BLK-history-01",
    status: str = "active",
    snapshot_text: str = "block text",
) -> None:
    conn.execute(
        """
        INSERT INTO documents (id, path, retrieval_type)
        VALUES ('DOC-history', 'source.md', 'structured')
        """
    )
    conn.execute(
        """
        INSERT INTO blocks (
            id, document_id, retrieval_type, selector_type, anchor,
            occurrence, line_start, line_end, snapshot_text,
            snapshot_sha256, status
        ) VALUES (?, 'DOC-history', 'structured', 'heading', 'anchor',
                  0, 1, 2, ?, ?, ?)
        """,
        (block_id, snapshot_text, "a" * 64, status),
    )


def _counts(conn: sqlite3.Connection) -> tuple[int, int, int]:
    return tuple(
        conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("commits", "entity_versions", "changes")
    )


def test_commit_creates_versions_changes_and_head(tmp_path):
    conn = _open(tmp_path)
    try:
        _seed_claim(conn)
        result = commit(conn, author="Axeuh", message="first")

        assert result.status == "committed"
        assert result.seq == 1
        assert result.parent_id is None
        assert result.change_count == 1
        assert re.fullmatch(r"CMT-[0-9a-f]{16}", result.commit_id)

        commit_row = conn.execute("SELECT * FROM commits").fetchone()
        assert commit_row["change_count"] == 1
        assert commit_row["schema_version"] == 3

        version = conn.execute("SELECT * FROM entity_versions").fetchone()
        assert version["entity_type"] == "claim"
        assert version["entity_id"] == CLAIM_ID
        assert version["version"] == 1
        snapshot = json.loads(version["data_json"])
        assert snapshot["body"] == "claim body"
        assert snapshot["status"] == "draft"
        assert "updated_at" not in snapshot
        assert list(snapshot) == sorted(snapshot)

        change = conn.execute("SELECT * FROM changes").fetchone()
        assert change["op"] == "create"
        assert change["before_version_id"] is None
        assert change["after_version_id"] == version["id"]

        state = {
            row["key"]: row["value"]
            for row in conn.execute("SELECT key, value FROM workspace_state")
        }
        assert state == {
            "head_commit": result.commit_id,
            "head_seq": "1",
        }
    finally:
        conn.close()


def test_commit_ignores_timestamp_only_churn(tmp_path):
    conn = _open(tmp_path)
    try:
        _seed_claim(conn)
        assert commit(conn, author="Axeuh", message="first").status == "committed"

        conn.execute(
            "UPDATE claims SET updated_at = '2099-01-01T00:00:00.000Z' "
            "WHERE id = ?",
            (CLAIM_ID,),
        )
        result = commit(conn, author="Axeuh", message="second")
        assert result.status == "empty"
        assert _counts(conn) == (1, 1, 1)
    finally:
        conn.close()


def test_commit_detects_update_and_delete(tmp_path):
    conn = _open(tmp_path)
    try:
        _seed_claim(conn)
        first = commit(conn, author="Axeuh", message="first")

        conn.execute(
            "UPDATE claims SET body = 'changed body' WHERE id = ?", (CLAIM_ID,)
        )
        second = commit(conn, author="Axeuh", message="second")
        assert second.status == "committed"
        assert second.change_count == 1
        assert second.parent_id == first.commit_id

        update = conn.execute(
            "SELECT * FROM changes WHERE commit_id = ?", (second.commit_id,)
        ).fetchone()
        assert update["op"] == "update"
        assert json.loads(update["before_json"])["body"] == "claim body"
        assert json.loads(update["after_json"])["body"] == "changed body"

        conn.execute("DELETE FROM claims WHERE id = ?", (CLAIM_ID,))
        third = commit(conn, author="Axeuh", message="third")
        assert third.status == "committed"
        assert third.change_count == 1

        delete_change = conn.execute(
            "SELECT * FROM changes WHERE commit_id = ?", (third.commit_id,)
        ).fetchone()
        assert delete_change["op"] == "delete"
        assert delete_change["after_json"] is None
        deleted_version = conn.execute(
            "SELECT data_json FROM entity_versions WHERE id = ?",
            (delete_change["after_version_id"],),
        ).fetchone()
        assert deleted_version["data_json"] == "null"

        assert commit(conn, author="Axeuh", message="nothing").status == "empty"
        assert _counts(conn) == (3, 3, 3)
    finally:
        conn.close()


def test_commit_identifier_is_deterministic(tmp_path):
    ids = []
    for name in ("a", "b"):
        conn = open_db(tmp_path / f"{name}.db")
        try:
            _seed_claim(conn)
            result = commit(conn, author="Axeuh", message="same")
            ids.append(result.commit_id)
        finally:
            conn.close()
    assert ids[0] == ids[1]


def test_commit_rolls_back_when_a_change_insert_fails(tmp_path):
    conn = _open(tmp_path)
    try:
        _seed_claim(conn)
        conn.execute(
            """
            CREATE TRIGGER fail_changes BEFORE INSERT ON changes
            BEGIN
                SELECT RAISE(ABORT, 'injected failure');
            END
            """
        )
        with pytest.raises(sqlite3.IntegrityError):
            commit(conn, author="Axeuh", message="first")
        assert _counts(conn) == (0, 0, 0)
        assert conn.execute("SELECT COUNT(*) FROM workspace_state").fetchone()[0] == 0
    finally:
        conn.close()


def test_diff_working_state_and_between_commits(tmp_path):
    conn = _open(tmp_path)
    try:
        _seed_claim(conn)
        first = commit(conn, author="Axeuh", message="first")

        conn.execute(
            "UPDATE claims SET body = 'changed', confidence = 'high' "
            "WHERE id = ?",
            (CLAIM_ID,),
        )
        working = diff_changes(conn)
        assert working.base_label == first.commit_id
        assert working.target_label == "working"
        assert len(working.entries) == 1
        entry = working.entries[0]
        assert (entry.entity_type, entry.entity_id, entry.op) == (
            "claim",
            CLAIM_ID,
            "update",
        )
        assert entry.changed_fields == ("body", "confidence")

        second = commit(conn, author="Axeuh", message="second")
        between = diff_changes(
            conn, from_id=first.commit_id, to_id=second.commit_id
        )
        assert [item.op for item in between.entries] == ["update"]
        assert between.entries[0].changed_fields == ("body", "confidence")

        empty = diff_changes(conn, from_id=second.commit_id, to_id=second.commit_id)
        assert empty.entries == ()

        with pytest.raises(UsageProblem):
            diff_changes(conn, from_id=first.commit_id)
    finally:
        conn.close()


def test_log_is_newest_first_and_honours_limit(tmp_path):
    conn = _open(tmp_path)
    try:
        _seed_claim(conn)
        first = commit(conn, author="Axeuh", message="first")
        conn.execute("UPDATE claims SET body = 'two' WHERE id = ?", (CLAIM_ID,))
        second = commit(conn, author="Axeuh", message="second")

        logs = list_commits(conn)
        assert [item.seq for item in logs] == [2, 1]
        assert logs[0].id == second.commit_id
        assert logs[1].id == first.commit_id

        assert len(list_commits(conn, limit=1)) == 1
        with pytest.raises(UsageProblem):
            list_commits(conn, limit=0)
    finally:
        conn.close()


def test_restore_updates_block_status_only(tmp_path):
    conn = _open(tmp_path)
    try:
        _seed_block(conn)
        first = commit(conn, author="Axeuh", message="first")

        conn.execute(
            "UPDATE blocks SET status = 'missing', snapshot_text = 'new text', "
            "snapshot_sha256 = ? WHERE id = ?",
            ("b" * 64, "BLK-history-01"),
        )
        second = commit(conn, author="Axeuh", message="second")
        assert second.change_count == 1

        result = restore(
            conn, first.commit_id, author="Axeuh", reason="undo block status"
        )
        assert result.status == "restored"
        assert result.seq == 3
        assert result.change_count == 1

        block = conn.execute(
            "SELECT status, snapshot_text FROM blocks WHERE id = 'BLK-history-01'"
        ).fetchone()
        assert block["status"] == "active"
        assert block["snapshot_text"] == "new text"

        restore_change = conn.execute(
            "SELECT op FROM changes WHERE commit_id = ?", (result.commit_id,)
        ).fetchone()
        assert restore_change["op"] == "restore"

        again = restore(
            conn, first.commit_id, author="Axeuh", reason="undo block status"
        )
        assert again.status == "empty"
    finally:
        conn.close()


def test_restore_keeps_entities_created_after_the_target(tmp_path):
    conn = _open(tmp_path)
    try:
        _seed_claim(conn, claim_id="OBS-D-2026-01-05-01", body="first claim")
        first = commit(conn, author="Axeuh", message="first")

        _seed_claim(conn, claim_id="OBS-D-2026-01-06-01", body="second claim")
        commit(conn, author="Axeuh", message="second")

        result = restore(conn, first.commit_id, author="Axeuh", reason="roll back")
        assert result.status == "empty"
        remaining = {
            row[0]
            for row in conn.execute("SELECT id FROM claims ORDER BY id")
        }
        assert remaining == {
            "OBS-D-2026-01-05-01",
            "OBS-D-2026-01-06-01",
        }
    finally:
        conn.close()
