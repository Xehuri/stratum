"""SQLite connection management and PRAGMAs."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from .migrations import SUPPORTED_SCHEMA_VERSION, apply_pending

SCHEMA_VERSION = SUPPORTED_SCHEMA_VERSION

ALLOWED_JOURNAL_MODES = frozenset(
    {"WAL", "DELETE", "TRUNCATE", "MEMORY", "PERSIST"}
)


def _journal_mode(explicit: str | None) -> str:
    mode = explicit or os.environ.get("STRATUM_JOURNAL_MODE") or "WAL"
    mode = mode.strip().upper()
    if mode not in ALLOWED_JOURNAL_MODES:
        allowed = ", ".join(sorted(ALLOWED_JOURNAL_MODES))
        raise ValueError(
            f"unsupported journal mode {mode!r}; expected one of: {allowed}"
        )
    return mode


def open_db(path: str | os.PathLike[str], *, journal_mode: str | None = None) -> sqlite3.Connection:
    """Open (and migrate) a Stratum database.

    WAL is the default because local databases benefit from concurrent
    readers. Synchronised or network folders should set
    ``STRATUM_JOURNAL_MODE=DELETE``.
    """
    path_text = os.fspath(path)
    if path_text != ":memory:":
        db_path = Path(path_text).expanduser()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        path_text = os.fspath(db_path)

    conn = sqlite3.connect(path_text, isolation_level=None, timeout=5.0)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute(f"PRAGMA journal_mode = {_journal_mode(journal_mode)}")
        conn.execute("PRAGMA synchronous = NORMAL")
        apply_pending(conn)
    except BaseException:
        conn.close()
        raise
    return conn
