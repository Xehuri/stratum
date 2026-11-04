"""Schema v1 contract tests.

These tests inspect the real SQLite database rather than trusting any
migration bookkeeping, so a migration that silently skips DDL is caught.
"""

from __future__ import annotations

import sqlite3

import pytest

from stratum.db import open_db

META_TABLES = {
    "schema_migrations",
    "workspace_meta",
    "settings",
    "profiles",
    "segmentation_runs",
}
CONTENT_TABLES = {
    "blobs",
    "documents",
    "document_versions",
    "blocks",
    "block_lineage",
}
INDEX_TABLES = {
    "concepts",
    "cards",
    "card_concepts",
    "card_blocks",
    "card_relations",
    "reports",
    "report_blocks",
    "report_cards",
    "report_relations",
}
CLAIM_TABLES = {"claims", "claim_relations"}
OBSERVATION_TABLES = {
    "series",
    "metric_points",
    "observations",
    "states",
    "state_transitions",
    "change_points",
    "patterns",
    "pattern_instances",
    "evidence_links",
}
HISTORY_TABLES = {"commits", "changes", "entity_versions", "workspace_state"}
OPS_TABLES = {"import_runs", "legacy_id_map", "fsck_runs", "fsck_findings"}
SEARCH_TABLES = {"blocks_fts", "cards_fts", "observations_fts"}

ALL_TABLES = (
    META_TABLES
    | CONTENT_TABLES
    | INDEX_TABLES
    | CLAIM_TABLES
    | OBSERVATION_TABLES
    | HISTORY_TABLES
    | OPS_TABLES
    | SEARCH_TABLES
)

# schema_migrations has applied_at by contract; FTS5 virtual tables only hold
# indexed / unindexed search columns.
NO_CREATED_AT = {"schema_migrations", "blocks_fts", "cards_fts", "observations_fts"}


def _table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    return {row[0] for row in rows}


def test_schema_v1_contains_all_table_groups(tmp_path):
    conn = open_db(tmp_path / "schema.db")
    try:
        names = _table_names(conn)
        assert ALL_TABLES <= names, f"missing tables: {sorted(ALL_TABLES - names)}"
        assert all(not name.startswith("sqlite_") for name in ALL_TABLES)
    finally:
        conn.close()


def test_schema_migrations_records_all_versions(tmp_path):
    conn = open_db(tmp_path / "schema.db")
    try:
        rows = conn.execute(
            "SELECT version, applied_at FROM schema_migrations ORDER BY version"
        ).fetchall()
        from stratum.migrations import SUPPORTED_SCHEMA_VERSION

        assert [row[0] for row in rows] == list(
            range(1, SUPPORTED_SCHEMA_VERSION + 1)
        )
        assert all(row[1] for row in rows)
    finally:
        conn.close()


def test_connection_pragmas(tmp_path):
    conn = open_db(tmp_path / "schema.db")
    try:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1
    finally:
        conn.close()


def test_journal_mode_can_be_delete_via_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("STRATUM_JOURNAL_MODE", "delete")
    conn = open_db(tmp_path / "delete.db")
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "delete"
    finally:
        conn.close()


def test_foreign_keys_are_enforced(tmp_path):
    conn = open_db(tmp_path / "schema.db")
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO blocks (
                    id, document_id, retrieval_type, selector_type, anchor,
                    line_start, line_end, snapshot_text, snapshot_sha256
                ) VALUES (
                    'BLK-orphan', 'DOC-missing', 'structured', 'heading', 'x',
                    1, 2, 'x', 'sha'
                )
                """
            )
    finally:
        conn.close()


def test_every_data_table_has_created_at(tmp_path):
    conn = open_db(tmp_path / "schema.db")
    try:
        for table in sorted(ALL_TABLES - NO_CREATED_AT):
            columns = {
                row[1] for row in conn.execute(f"PRAGMA table_info({table})")
            }
            assert "created_at" in columns, f"{table} has no created_at column"
    finally:
        conn.close()


def test_json_columns_are_text(tmp_path):
    conn = open_db(tmp_path / "schema.db")
    try:
        for table, column in (
            ("cards", "metadata_json"),
            ("settings", "value"),
            ("observations", "evidence_links_json"),
            ("changes", "after_json"),
        ):
            info = {
                row[1]: row[2]
                for row in conn.execute(f"PRAGMA table_info({table})")
            }
            assert info[column].upper() == "TEXT"
    finally:
        conn.close()


def test_fts5_tables_are_virtual_and_searchable(tmp_path):
    conn = open_db(tmp_path / "schema.db")
    try:
        for table in SEARCH_TABLES:
            sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name = ?", (table,)
            ).fetchone()[0]
            assert "VIRTUAL TABLE" in sql.upper()
            assert "fts5" in sql.lower()

        conn.execute(
            """
            INSERT INTO blocks_fts (block_id, document_id, snapshot_text)
            VALUES ('BLK-1', 'DOC-1', 'the tidepool at dawn')
            """
        )
        hits = conn.execute(
            "SELECT block_id FROM blocks_fts WHERE blocks_fts MATCH ?", ("tidepool",)
        ).fetchall()
        assert [row[0] for row in hits] == ["BLK-1"]
    finally:
        conn.close()
