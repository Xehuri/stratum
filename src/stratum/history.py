"""Linear history for Stratum-owned index state.

History is deliberately simple: one linear chain of commits, no staging
area, no branches, no merge or rebase. ``commit`` compares the current
``claims``, ``cards``, ``reports`` and ``blocks`` rows against the latest
``entity_versions`` snapshot and records create/update/delete changes in one
transaction. Markdown files are never read or written here: block content is
tracked as derived state, and ``restore`` only puts index rows back.

Timestamps (``created_at`` / ``updated_at``) are excluded from snapshots so
that a replay of an otherwise identical ingest is not a change. Row identity
and every other column are part of the canonical JSON payload; canonical
JSON uses sorted keys and compact separators, which keeps commit identifiers
deterministic for the same sequence, parent, author, message and content.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Mapping

from .errors import BusinessError, UsageProblem
from .ingest_source import transaction
from .migrations import SUPPORTED_SCHEMA_VERSION, utcnow

__all__ = [
    "CommitInfo",
    "CommitResult",
    "DiffEntry",
    "DiffResult",
    "RestoreResult",
    "changed_fields",
    "commit",
    "diff_changes",
    "format_diff",
    "latest_commit",
    "list_commits",
    "restore",
    "snapshot_entities",
]

ENTITY_TYPES = ("claim", "card", "report", "block")
ENTITY_TABLES = {
    "claim": "claims",
    "card": "cards",
    "report": "reports",
    "block": "blocks",
}

# Explicit column lists keep history independent from unrelated schema
# additions and from volatile bookkeeping timestamps. ``id`` is always first.
ENTITY_COLUMNS = {
    "claim": (
        "id",
        "report_id",
        "claim_type",
        "period_start",
        "period_end",
        "granularity",
        "entities_json",
        "body",
        "confidence",
        "status",
        "author",
        "contract_version",
        "content_sha256",
        "data_gap",
        "metric",
        "baseline",
        "delta",
        "sample_size",
        "conditions",
        "outcome",
        "support_count",
        "opportunity_count",
        "counterexample_count",
        "counterexample_note",
        "lag",
    ),
    "card": (
        "id",
        "card_type",
        "title",
        "body",
        "status",
        "claim_type",
        "confidence",
        "period_start",
        "period_end",
        "granularity",
        "valid_from",
        "valid_to",
        "legacy_card_id",
        "superseded_by",
        "author",
        "metadata_json",
    ),
    "report": (
        "id",
        "report_type",
        "title",
        "period_start",
        "period_end",
        "author",
        "contract_version",
        "base_commit",
        "document_id",
        "status",
    ),
    "block": (
        "id",
        "document_id",
        "document_version_id",
        "profile_id",
        "parent_block_id",
        "retrieval_type",
        "selector_type",
        "anchor",
        "occurrence",
        "line_start",
        "line_end",
        "snapshot_text",
        "snapshot_sha256",
        "status",
        "legacy_block_id",
        "metadata_json",
    ),
}

DEFAULT_LOG_LIMIT = 20


@dataclass(frozen=True)
class CommitInfo:
    id: str
    seq: int
    parent_id: str | None
    author: str
    message: str
    change_count: int
    created_at: str


@dataclass(frozen=True)
class CommitResult:
    status: str
    author: str = ""
    message: str = ""
    commit_id: str | None = None
    seq: int | None = None
    parent_id: str | None = None
    change_count: int = 0
    created_at: str | None = None


@dataclass(frozen=True)
class DiffEntry:
    entity_type: str
    entity_id: str
    op: str
    before: Mapping[str, object] | None
    after: Mapping[str, object] | None

    @property
    def changed_fields(self) -> tuple[str, ...]:
        return changed_fields(self.before, self.after)


@dataclass(frozen=True)
class DiffResult:
    base_label: str
    target_label: str
    entries: tuple[DiffEntry, ...]


@dataclass(frozen=True)
class RestoreResult:
    status: str
    target_id: str
    commit_id: str | None = None
    seq: int | None = None
    change_count: int = 0


@dataclass(frozen=True)
class _PendingChange:
    entity_type: str
    entity_id: str
    op: str
    before: Mapping[str, object] | None
    after: Mapping[str, object] | None
    latest_version_id: str | None
    latest_version: int


# ---------------------------------------------------------------------------
# Canonical payloads and snapshots
# ---------------------------------------------------------------------------


def _canonical_json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def changed_fields(
    before: Mapping[str, object] | None,
    after: Mapping[str, object] | None,
) -> tuple[str, ...]:
    if before is None or after is None:
        return ()
    keys = set(before) | set(after)
    return tuple(
        sorted(key for key in keys if before.get(key) != after.get(key))
    )


def snapshot_entities(conn: sqlite3.Connection) -> dict[tuple[str, str], dict]:
    """Return the current canonical snapshot of every tracked entity."""

    snapshot: dict[tuple[str, str], dict] = {}
    for entity_type in ENTITY_TYPES:
        columns = ENTITY_COLUMNS[entity_type]
        table = ENTITY_TABLES[entity_type]
        rows = conn.execute(
            f"SELECT {', '.join(columns)} FROM {table} ORDER BY id"
        ).fetchall()
        for row in rows:
            data = {column: row[index] for index, column in enumerate(columns)}
            snapshot[(entity_type, str(data["id"]))] = data
    return snapshot


def _latest_versions(
    conn: sqlite3.Connection,
) -> dict[tuple[str, str], tuple[str, int, object]]:
    placeholders = ", ".join("?" for _ in ENTITY_TYPES)
    rows = conn.execute(
        f"""
        SELECT entity_type, entity_id, id, version, data_json
        FROM entity_versions
        WHERE entity_type IN ({placeholders})
        ORDER BY entity_type, entity_id, version
        """,
        ENTITY_TYPES,
    ).fetchall()
    latest: dict[tuple[str, str], tuple[str, int, object]] = {}
    for row in rows:
        entity_type, entity_id, version_id, version, data_json = row
        latest[(str(entity_type), str(entity_id))] = (
            str(version_id),
            int(version),
            json.loads(str(data_json)),
        )
    return latest


def _pending_changes(
    current: Mapping[tuple[str, str], Mapping[str, object]],
    latest: Mapping[tuple[str, str], tuple[str, int, object]],
) -> list[_PendingChange]:
    pending: list[_PendingChange] = []
    for entity_type, entity_id in sorted(set(current) | set(latest)):
        key = (entity_type, entity_id)
        after = current.get(key)
        previous = latest.get(key)
        before = previous[2] if previous is not None else None

        if after is None:
            if before is not None:
                pending.append(
                    _PendingChange(
                        entity_type=entity_type,
                        entity_id=entity_id,
                        op="delete",
                        before=before,
                        after=None,
                        latest_version_id=previous[0] if previous else None,
                        latest_version=previous[1] if previous else 0,
                    )
                )
            continue

        if before is None:
            op = "create"
        elif _canonical_json(before) == _canonical_json(after):
            continue
        else:
            op = "update"

        pending.append(
            _PendingChange(
                entity_type=entity_type,
                entity_id=entity_id,
                op=op,
                before=before,
                after=after,
                latest_version_id=previous[0] if previous else None,
                latest_version=previous[1] if previous else 0,
            )
        )
    return pending


# ---------------------------------------------------------------------------
# HEAD and commit lookup
# ---------------------------------------------------------------------------


def latest_commit(conn: sqlite3.Connection) -> CommitInfo | None:
    row = conn.execute(
        """
        SELECT id, seq, parent_id, author, message, change_count, created_at
        FROM commits ORDER BY seq DESC LIMIT 1
        """
    ).fetchone()
    if row is None:
        return None
    return CommitInfo(
        id=str(row[0]),
        seq=int(row[1]),
        parent_id=str(row[2]) if row[2] is not None else None,
        author=str(row[3]),
        message=str(row[4]),
        change_count=int(row[5]),
        created_at=str(row[6]),
    )


def _resolve_commit(conn: sqlite3.Connection, reference: str) -> CommitInfo:
    text = (reference or "").strip()
    if not text:
        raise UsageProblem("a commit id is required")
    rows = conn.execute(
        """
        SELECT id, seq, parent_id, author, message, change_count, created_at
        FROM commits WHERE substr(id, 1, ?) = ?
        ORDER BY seq
        """,
        (len(text), text),
    ).fetchall()
    if not rows:
        raise BusinessError(f"commit not found: {text}")
    if len(rows) > 1:
        raise BusinessError(
            f"commit reference {text!r} is ambiguous; use a longer id"
        )
    row = rows[0]
    return CommitInfo(
        id=str(row[0]),
        seq=int(row[1]),
        parent_id=str(row[2]) if row[2] is not None else None,
        author=str(row[3]),
        message=str(row[4]),
        change_count=int(row[5]),
        created_at=str(row[6]),
    )


def _state_at_commit(conn: sqlite3.Connection, commit: CommitInfo) -> dict:
    placeholders = ", ".join("?" for _ in ENTITY_TYPES)
    rows = conn.execute(
        f"""
        SELECT ev.entity_type, ev.entity_id, ev.version, ev.data_json
        FROM entity_versions ev
        JOIN commits c ON c.id = ev.commit_id
        WHERE c.seq <= ? AND ev.entity_type IN ({placeholders})
        ORDER BY ev.entity_type, ev.entity_id, ev.version
        """,
        (commit.seq, *ENTITY_TYPES),
    ).fetchall()
    state: dict[tuple[str, str], dict] = {}
    for row in rows:
        key = (str(row[0]), str(row[1]))
        data = json.loads(str(row[3]))
        if data is None:
            state.pop(key, None)
        else:
            state[key] = data
    return state


# ---------------------------------------------------------------------------
# Commit
# ---------------------------------------------------------------------------


def _commit_id(
    seq: int,
    parent_id: str | None,
    author: str,
    message: str,
    pending: list[_PendingChange],
) -> str:
    content = [
        [
            change.entity_type,
            change.entity_id,
            change.op,
            _canonical_json(change.after) if change.after is not None else None,
        ]
        for change in pending
    ]
    seed = _canonical_json(
        {
            "seq": seq,
            "parent_id": parent_id,
            "author": author,
            "message": message,
            "changes": content,
        }
    )
    return "CMT-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def _commit_locked(
    conn: sqlite3.Connection,
    *,
    author: str,
    message: str,
    restore_op: bool = False,
) -> CommitResult:
    head = latest_commit(conn)
    seq = (head.seq if head is not None else 0) + 1
    parent_id = head.id if head is not None else None

    current = snapshot_entities(conn)
    latest = _latest_versions(conn)
    pending = _pending_changes(current, latest)
    if not pending:
        return CommitResult(status="empty", author=author, message=message)

    commit_id = _commit_id(seq, parent_id, author, message, pending)
    now = utcnow()
    conn.execute(
        """
        INSERT INTO commits (
            id, seq, parent_id, author, message, change_count,
            schema_version, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            commit_id,
            seq,
            parent_id,
            author,
            message,
            len(pending),
            SUPPORTED_SCHEMA_VERSION,
            now,
        ),
    )

    for change in pending:
        version = change.latest_version + 1
        data_json = (
            "null"
            if change.after is None
            else _canonical_json(change.after)
        )
        version_id = "EVR-" + hashlib.sha256(
            "\x00".join(
                (change.entity_type, change.entity_id, str(version), data_json)
            ).encode("utf-8")
        ).hexdigest()[:16]
        conn.execute(
            """
            INSERT INTO entity_versions (
                id, entity_type, entity_id, version, commit_id,
                blob_id, data_json, created_at
            ) VALUES (?, ?, ?, ?, ?, NULL, ?, ?)
            """,
            (
                version_id,
                change.entity_type,
                change.entity_id,
                version,
                commit_id,
                data_json,
                now,
            ),
        )

        op = "restore" if restore_op and change.op == "update" else change.op
        before_json = (
            _canonical_json(change.before)
            if change.before is not None
            else None
        )
        after_json = (
            _canonical_json(change.after)
            if change.after is not None
            else None
        )
        change_id = "CHG-" + hashlib.sha256(
            "\x00".join(
                (
                    commit_id,
                    change.entity_type,
                    change.entity_id,
                    op,
                )
            ).encode("utf-8")
        ).hexdigest()[:16]
        conn.execute(
            """
            INSERT INTO changes (
                id, commit_id, entity_type, entity_id, op,
                before_version_id, after_version_id,
                before_json, after_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                change_id,
                commit_id,
                change.entity_type,
                change.entity_id,
                op,
                change.latest_version_id,
                version_id,
                before_json,
                after_json,
                now,
            ),
        )

    for key, value in (("head_commit", commit_id), ("head_seq", str(seq))):
        conn.execute(
            """
            INSERT INTO workspace_state (key, value, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (key, value, now, now),
        )

    return CommitResult(
        status="committed",
        author=author,
        message=message,
        commit_id=commit_id,
        seq=seq,
        parent_id=parent_id,
        change_count=len(pending),
        created_at=now,
    )


def _require_author_message(author: str, message: str) -> tuple[str, str]:
    author_text = (author or "").strip()
    message_text = (message or "").strip()
    if not author_text:
        raise UsageProblem("--author is required and must not be empty")
    if not message_text:
        raise UsageProblem("--message is required and must not be empty")
    return author_text, message_text


def commit(
    conn: sqlite3.Connection, *, author: str, message: str
) -> CommitResult:
    author_text, message_text = _require_author_message(author, message)
    with transaction(conn):
        return _commit_locked(
            conn, author=author_text, message=message_text
        )


# ---------------------------------------------------------------------------
# Log
# ---------------------------------------------------------------------------


def list_commits(
    conn: sqlite3.Connection, *, limit: int = DEFAULT_LOG_LIMIT
) -> list[CommitInfo]:
    if limit < 1:
        raise UsageProblem("--limit must be at least 1")
    rows = conn.execute(
        """
        SELECT id, seq, parent_id, author, message, change_count, created_at
        FROM commits ORDER BY seq DESC LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [
        CommitInfo(
            id=str(row[0]),
            seq=int(row[1]),
            parent_id=str(row[2]) if row[2] is not None else None,
            author=str(row[3]),
            message=str(row[4]),
            change_count=int(row[5]),
            created_at=str(row[6]),
        )
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------


def _diff_states(
    before: Mapping[tuple[str, str], Mapping[str, object]],
    after: Mapping[tuple[str, str], Mapping[str, object]],
) -> tuple[DiffEntry, ...]:
    entries: list[DiffEntry] = []
    for entity_type, entity_id in sorted(set(before) | set(after)):
        key = (entity_type, entity_id)
        old = before.get(key)
        new = after.get(key)
        if old is None and new is None:  # pragma: no cover - defensive
            continue
        if old is None:
            op = "create"
        elif new is None:
            op = "delete"
        elif _canonical_json(old) == _canonical_json(new):
            continue
        else:
            op = "update"
        entries.append(
            DiffEntry(
                entity_type=entity_type,
                entity_id=entity_id,
                op=op,
                before=old,
                after=new,
            )
        )
    return tuple(entries)


def diff_changes(
    conn: sqlite3.Connection,
    *,
    from_id: str | None = None,
    to_id: str | None = None,
) -> DiffResult:
    if (from_id is None) != (to_id is None):
        raise UsageProblem("--from and --to must be used together")

    if from_id is not None and to_id is not None:
        base_commit = _resolve_commit(conn, from_id)
        target_commit = _resolve_commit(conn, to_id)
        base = _state_at_commit(conn, base_commit)
        target = _state_at_commit(conn, target_commit)
        return DiffResult(
            base_label=base_commit.id,
            target_label=target_commit.id,
            entries=_diff_states(base, target),
        )

    head = latest_commit(conn)
    base = _state_at_commit(conn, head) if head is not None else {}
    target = snapshot_entities(conn)
    return DiffResult(
        base_label=head.id if head is not None else "empty",
        target_label="working",
        entries=_diff_states(base, target),
    )


def format_diff(result: DiffResult) -> str:
    lines = [f"Diff {result.base_label}..{result.target_label}"]
    if not result.entries:
        lines.append("no changes")
        return "\n".join(lines) + "\n"
    for entry in result.entries:
        if entry.op == "update":
            fields = ", ".join(entry.changed_fields) or "no visible fields"
            lines.append(
                f"update {entry.entity_type} {entry.entity_id}  {fields}"
            )
        else:
            lines.append(f"{entry.op} {entry.entity_type} {entry.entity_id}")
    lines.append(f"{len(result.entries)} changes")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------


def _restore_entity_row(
    conn: sqlite3.Connection,
    entity_type: str,
    data: Mapping[str, object],
    *,
    now: str,
) -> None:
    columns = ENTITY_COLUMNS[entity_type]
    assignments = ", ".join(
        f"{column} = ?" for column in columns if column != "id"
    )
    values = [
        data.get(column) for column in columns if column != "id"
    ]
    conn.execute(
        f"""
        UPDATE {ENTITY_TABLES[entity_type]}
        SET {assignments}, updated_at = ?
        WHERE id = ?
        """,
        (*values, now, data["id"]),
    )


def _require_author_reason(author: str, reason: str) -> tuple[str, str]:
    author_text = (author or "").strip()
    reason_text = (reason or "").strip()
    if not author_text:
        raise UsageProblem("--author is required and must not be empty")
    if not reason_text:
        raise UsageProblem("--reason is required and must not be empty")
    return author_text, reason_text


def restore(
    conn: sqlite3.Connection,
    reference: str,
    *,
    author: str,
    reason: str,
) -> RestoreResult:
    """Restore index rows to their state at ``reference``.

    Only rows that still exist are updated. Restore never deletes entities
    and never creates rows for entities that do not exist in the current
    working state; it also never reads or writes Markdown. Blocks only have
    their ``status`` restored because their content is derived from source
    files.
    """

    author_text, reason_text = _require_author_reason(author, reason)
    with transaction(conn):
        target = _resolve_commit(conn, reference)
        target_state = _state_at_commit(conn, target)
        current = snapshot_entities(conn)
        now = utcnow()
        for key in sorted(target_state):
            data = target_state[key]
            if data is None:
                continue
            if key not in current:
                continue
            entity_type, entity_id = key
            if entity_type == "block":
                if current[key].get("status") == data.get("status"):
                    continue
                conn.execute(
                    "UPDATE blocks SET status = ?, updated_at = ? WHERE id = ?",
                    (data.get("status"), now, entity_id),
                )
                continue
            if _canonical_json(current[key]) == _canonical_json(data):
                continue
            _restore_entity_row(conn, entity_type, data, now=now)

        message = f"restore {target.id}: {reason_text}"
        result = _commit_locked(
            conn, author=author_text, message=message, restore_op=True
        )
        if result.status == "empty":
            return RestoreResult(status="empty", target_id=target.id)
        return RestoreResult(
            status="restored",
            target_id=target.id,
            commit_id=result.commit_id,
            seq=result.seq,
            change_count=result.change_count,
        )
