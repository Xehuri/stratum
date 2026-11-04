"""Forward-only migration runner tests."""

from __future__ import annotations

import pytest

from stratum import migrations
from stratum.db import open_db


def test_open_db_applies_v1_exactly_once(tmp_path):
    path = tmp_path / "migrate.db"

    conn = open_db(path)
    conn.close()
    conn = open_db(path)
    try:
        versions = [
            row[0]
            for row in conn.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        ]
        assert versions == [1]
        assert conn.execute(
            "SELECT applied_at FROM schema_migrations WHERE version = 1"
        ).fetchone()[0]
    finally:
        conn.close()


def test_database_newer_than_code_is_rejected(tmp_path):
    path = tmp_path / "future.db"
    conn = open_db(path)
    conn.execute(
        "INSERT INTO schema_migrations (version, applied_at) "
        "VALUES (999, '2026-01-01T00:00:00Z')"
    )
    conn.close()

    with pytest.raises(migrations.MigrationError, match="newer"):
        open_db(path)


def test_pending_migration_is_applied_and_idempotent(tmp_path, monkeypatch):
    path = tmp_path / "forward.db"
    conn = open_db(path)

    extra = migrations.Migration(
        version=migrations.SUPPORTED_SCHEMA_VERSION + 1,
        name="add_forward_probe",
        sql=(
            "CREATE TABLE forward_probe ("
            "id TEXT PRIMARY KEY, "
            "created_at TEXT NOT NULL"
            ");"
        ),
    )
    monkeypatch.setattr(migrations, "MIGRATIONS", migrations.MIGRATIONS + (extra,))
    monkeypatch.setattr(migrations, "SUPPORTED_SCHEMA_VERSION", extra.version)

    try:
        migrations.apply_pending(conn)
        names = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "forward_probe" in names
        versions = [
            row[0]
            for row in conn.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        ]
        assert versions == [1, extra.version]

        migrations.apply_pending(conn)
        count = conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
        assert count == 2
    finally:
        conn.close()


def test_failed_migration_rolls_back(tmp_path, monkeypatch):
    path = tmp_path / "broken.db"
    conn = open_db(path)

    broken = migrations.Migration(
        version=migrations.SUPPORTED_SCHEMA_VERSION + 1,
        name="broken",
        sql=(
            "CREATE TABLE half_applied (id TEXT PRIMARY KEY);"
            "INSERT INTO table_that_does_not_exist (id) VALUES ('x');"
        ),
    )
    monkeypatch.setattr(migrations, "MIGRATIONS", migrations.MIGRATIONS + (broken,))
    monkeypatch.setattr(migrations, "SUPPORTED_SCHEMA_VERSION", broken.version)

    try:
        with pytest.raises(migrations.MigrationError):
            migrations.apply_pending(conn)

        names = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "half_applied" not in names
        versions = [
            row[0]
            for row in conn.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        ]
        assert versions == [1]
    finally:
        conn.close()


def test_migration_versions_are_contiguous():
    versions = [migration.version for migration in migrations.MIGRATIONS]
    assert versions == list(range(1, migrations.SUPPORTED_SCHEMA_VERSION + 1))
