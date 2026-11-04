"""Forward-only SQLite schema migrations.

The database stores the versions it has applied in ``schema_migrations``.
There are no down migrations: a newer database is rejected rather than
silently downgraded, and an upgrade should be preceded by a file copy.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib import resources


class MigrationError(RuntimeError):
    """Raised when the database schema cannot be migrated safely."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str


def utcnow() -> str:
    """Return an ISO-8601 UTC timestamp with millisecond precision."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _load_schema(filename: str) -> str:
    return (
        resources.files("stratum")
        .joinpath(filename)
        .read_text(encoding="utf-8")
    )


def _load_schema_v1() -> str:
    return _load_schema("schema.sql")


def _load_schema_v2() -> str:
    return _load_schema("schema_v2.sql")


def _load_schema_v3() -> str:
    return _load_schema("schema_v3.sql")


MIGRATIONS: tuple[Migration, ...] = (
    Migration(version=1, name="create_schema_v1", sql=_load_schema_v1()),
    Migration(version=2, name="add_claims", sql=_load_schema_v2()),
    Migration(version=3, name="add_review_events", sql=_load_schema_v3()),
)

SUPPORTED_SCHEMA_VERSION = MIGRATIONS[-1].version


def _validate_migrations() -> None:
    versions = [migration.version for migration in MIGRATIONS]
    if versions != list(range(1, len(MIGRATIONS) + 1)):
        raise MigrationError(
            "migration versions must be contiguous and start at 1, "
            f"got {versions}"
        )
    if len({migration.name for migration in MIGRATIONS}) != len(MIGRATIONS):
        raise MigrationError("migration names must be unique")


_validate_migrations()


def ensure_migrations_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version    INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
        """
    )


def applied_versions(conn: sqlite3.Connection) -> set[int]:
    ensure_migrations_table(conn)
    rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
    return {int(row[0]) for row in rows}


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _apply_one(conn: sqlite3.Connection, migration: Migration) -> None:
    script = (
        "BEGIN IMMEDIATE;\n"
        + migration.sql
        + "\nINSERT INTO schema_migrations (version, applied_at) VALUES ("
        + str(migration.version)
        + ", "
        + _sql_literal(utcnow())
        + ");\nCOMMIT;\n"
    )
    try:
        conn.executescript(script)
    except sqlite3.Error as exc:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise MigrationError(
            f"migration {migration.version} ({migration.name}) failed: {exc}"
        ) from exc


def apply_pending(conn: sqlite3.Connection) -> list[int]:
    """Apply every migration not yet recorded, in version order.

    Returns the versions that were applied by this call.
    """
    applied = applied_versions(conn)
    known = {migration.version for migration in MIGRATIONS}
    unknown = applied - known
    if unknown:
        newest = max(unknown)
        raise MigrationError(
            f"database schema version {newest} is newer than this build "
            f"(supported {SUPPORTED_SCHEMA_VERSION}); upgrade Stratum instead "
            "of running an older binary"
        )

    newly_applied: list[int] = []
    for migration in MIGRATIONS:
        if migration.version in applied:
            continue
        _apply_one(conn, migration)
        newly_applied.append(migration.version)

    current = max(applied | set(newly_applied) | {0})
    conn.execute(f"PRAGMA user_version = {current}")
    return newly_applied
