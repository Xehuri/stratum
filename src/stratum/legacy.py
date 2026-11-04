"""Import the legacy JSONL index into a Stratum workspace.

The legacy index is read-only: the importer never rewrites its source files.
Every row is copied into SQLite inside one transaction, so a failure rolls
all data rows back. A failed import still records an ``import_runs`` row in a
separate transaction; the completed run and its stats are committed together
with the data.

Blocks receive deterministic ``BLK-`` ids derived from the old id and file;
the old id lives on as ``legacy_block_id`` and in ``legacy_id_map``. Cards,
concepts and reports keep their old ids unchanged, as required by the
migration contract. A completed import is a no-op when the index fingerprint
is unchanged.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Iterator, Mapping

from .db import open_db
from .errors import ValidationError
from .ingest_source import transaction
from .markdown import normalize_relative_path
from .migrations import utcnow
from .segmentation.identity import compute_block_id
from .workspace import WorkspaceError, resolve_workspace

__all__ = [
    "COUNT_ORDER",
    "LegacyImportResult",
    "card_relation_id",
    "import_legacy_index",
    "legacy_block_identity",
    "legacy_card_period",
    "legacy_map_id",
    "map_report_type",
    "report_relation_id",
]

META_FILENAME = "index.meta.json"
LEGACY_JSONL_FILES = (
    "blocks.jsonl",
    "cards.jsonl",
    "card_blocks.jsonl",
    "card_concepts.jsonl",
    "card_relations.jsonl",
    "concept_types.jsonl",
    "reports.jsonl",
    "report_relations.jsonl",
)

LEGACY_SCHEMA_VERSION = 1
LEGACY_PROFILE_ID = "legacy"
LEGACY_SELECTOR = "legacy"

COUNT_ORDER = (
    "documents",
    "blocks",
    "cards",
    "concepts",
    "card_blocks",
    "card_concepts",
    "card_relations",
    "reports",
    "report_blocks",
    "report_cards",
    "report_relations",
    "legacy_id_map",
)

CARD_TYPES = frozenset(
    {"event", "observation", "finding", "state", "decision"}
)
CARD_STATUSES = frozenset(
    {"draft", "active", "superseded", "rejected", "stale"}
)
CARD_CONFIDENCES = frozenset({"high", "medium", "low"})
BLOCK_STATUSES = frozenset({"active", "stale", "missing"})
REPORT_TYPES = frozenset({"daily", "weekly", "biweekly", "summary"})
REPORT_STATUSES = frozenset({"draft", "active", "stale", "archived"})
REPORT_TYPE_ALIASES = {"program": "summary"}


@dataclass(frozen=True)
class LegacyImportResult:
    status: str
    index: str
    counts: Mapping[str, int]
    run_id: str | None = None


@dataclass
class _ImportState:
    block_new: dict[str, str] = field(default_factory=dict)
    block_text: dict[str, str] = field(default_factory=dict)
    block_quote_hashes: dict[str, frozenset[str]] = field(default_factory=dict)
    cards: set[str] = field(default_factory=set)
    concepts: set[str] = field(default_factory=set)
    reports: set[str] = field(default_factory=set)
    documents: set[str] = field(default_factory=set)


# ---------------------------------------------------------------------------
# Pure mapping helpers
# ---------------------------------------------------------------------------


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _is_sha256(value: str) -> bool:
    if len(value) != 64:
        return False
    return all(character in "0123456789abcdef" for character in value)


def legacy_block_identity(file_path: str, legacy_block_id: str) -> str:
    """Return the deterministic new block id for one legacy block."""

    doc_id = normalize_relative_path(file_path)
    if not doc_id:
        raise ValidationError(
            "LEGACY_FIELD_INVALID",
            "block file must be a non-empty relative path",
        )
    return compute_block_id(
        doc_id, LEGACY_PROFILE_ID, LEGACY_SELECTOR, legacy_block_id, 0
    )


def legacy_card_period(
    row: Mapping[str, object],
) -> tuple[str | None, str | None]:
    """Return the half-open card period from legacy period or date fields."""

    period_start = _optional_text(row.get("period_start"))
    period_end = _optional_text(row.get("period_end"))
    day = _optional_text(row.get("date"))

    if period_start is not None:
        start = _parse_date(period_start, "period_start", "card")
        if period_end is not None:
            end = _parse_date(period_end, "period_end", "card")
        else:
            end = start + timedelta(days=1)
    elif day is not None:
        start = _parse_date(day, "date", "card")
        end = start + timedelta(days=1)
    else:
        return None, None

    if end < start:
        raise ValidationError(
            "LEGACY_FIELD_INVALID",
            "card period_end must not be earlier than period_start",
        )
    return start.isoformat(), end.isoformat()


def map_report_type(value: str) -> str:
    """Map a legacy report type onto the schema's report type set."""

    text = str(value).strip()
    mapped = REPORT_TYPE_ALIASES.get(text, text)
    if mapped not in REPORT_TYPES:
        raise ValidationError(
            "LEGACY_FIELD_INVALID",
            f"unsupported report_type {value!r}",
        )
    return mapped


def legacy_map_id(entity_type: str, legacy_id: str) -> str:
    payload = f"{entity_type}\x00{legacy_id}"
    return "LM-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def card_relation_id(from_card: str, to_card: str, relation: str) -> str:
    payload = f"{from_card}\x00{to_card}\x00{relation}"
    return "CREL-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def report_relation_id(from_report: str, relation: str, to_report: str) -> str:
    payload = f"{from_report}\x00{relation}\x00{to_report}"
    return "RREL-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _parse_date(value: str, field_name: str, where: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError(
            "LEGACY_FIELD_INVALID",
            f"{where}: {field_name} must be YYYY-MM-DD, got {value!r}",
        ) from exc


def _required_text(
    row: Mapping[str, object], key: str, where: str
) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(
            "LEGACY_FIELD_INVALID",
            f"{where}: {key} must be a non-empty string",
        )
    return value


def _required_int(
    row: Mapping[str, object], key: str, where: str, minimum: int
) -> int:
    value = row.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(
            "LEGACY_FIELD_INVALID",
            f"{where}: {key} must be an integer",
        )
    if value < minimum:
        raise ValidationError(
            "LEGACY_FIELD_INVALID",
            f"{where}: {key} must be at least {minimum}",
        )
    return value


def _enum(
    value: object, allowed: frozenset[str], field_name: str, where: str
) -> str:
    text = str(value).strip() if value is not None else ""
    if text not in allowed:
        values = ", ".join(sorted(allowed))
        raise ValidationError(
            "LEGACY_FIELD_INVALID",
            f"{where}: unsupported {field_name} {value!r}; expected one of: {values}",
        )
    return text


def _quote_hashes(text: str) -> frozenset[str]:
    """Hash forms the legacy writer may use for a card/block quote.

    The live v1 index hashes the whole block text, but line and paragraph
    hashes are accepted so a future writer quoting a fragment still imports
    while a corrupted digest remains detectable.
    """

    hashes = {_sha256_text(text)}
    for line in text.split("\n"):
        if line:
            hashes.add(_sha256_text(line))
    for paragraph in text.split("\n\n"):
        if paragraph:
            hashes.add(_sha256_text(paragraph))
    return frozenset(hashes)


def _fail(code: str, message: str) -> None:
    raise ValidationError(code, message)


# ---------------------------------------------------------------------------
# Index files
# ---------------------------------------------------------------------------


def _iter_jsonl(path: Path) -> Iterator[tuple[int, dict]]:
    try:
        handle = path.open("r", encoding="utf-8-sig", newline="")
    except OSError as exc:
        raise ValidationError(
            "LEGACY_FILE_UNREADABLE",
            f"cannot read {path.name}: {exc}",
        ) from exc
    try:
        with handle:
            for lineno, raw in enumerate(handle, 1):
                if not raw.strip():
                    continue
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise ValidationError(
                        "LEGACY_JSON_INVALID",
                        f"{path.name}:{lineno}: {exc.msg}",
                    ) from exc
                if not isinstance(payload, dict):
                    raise ValidationError(
                        "LEGACY_JSON_INVALID",
                        f"{path.name}:{lineno}: expected a JSON object",
                    )
                yield lineno, payload
    except OSError as exc:
        raise ValidationError(
            "LEGACY_FILE_UNREADABLE",
            f"cannot read {path.name}: {exc}",
        ) from exc


def _validate_layout(index: Path) -> None:
    if not index.exists():
        _fail(
            "LEGACY_INDEX_MISSING",
            f"legacy index directory not found: {index}",
        )
    if not index.is_dir():
        _fail(
            "LEGACY_INDEX_MISSING",
            f"legacy index path is not a directory: {index}",
        )
    if not (index / META_FILENAME).is_file():
        _fail(
            "LEGACY_FILE_MISSING",
            f"legacy index is missing {META_FILENAME}",
        )
    for name in LEGACY_JSONL_FILES:
        if not (index / name).is_file():
            _fail(
                "LEGACY_FILE_MISSING",
                f"legacy index is missing {name}",
            )


def _load_meta(index: Path) -> dict:
    meta_path = index / META_FILENAME
    try:
        text = meta_path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise ValidationError(
            "LEGACY_FILE_UNREADABLE",
            f"cannot read {META_FILENAME}: {exc}",
        ) from exc
    try:
        mapping = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValidationError(
            "LEGACY_META_INVALID",
            f"{META_FILENAME}: {exc.msg}",
        ) from exc
    if not isinstance(mapping, dict):
        _fail("LEGACY_META_INVALID", f"{META_FILENAME}: expected a JSON object")
    version = mapping.get("schema_version")
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version != LEGACY_SCHEMA_VERSION
    ):
        _fail(
            "LEGACY_META_INVALID",
            f"{META_FILENAME}: unsupported schema_version {version!r}; "
            f"expected {LEGACY_SCHEMA_VERSION}",
        )
    return mapping


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
    except OSError as exc:
        raise ValidationError(
            "LEGACY_FILE_UNREADABLE",
            f"cannot read {path.name}: {exc}",
        ) from exc
    return digest.hexdigest()


def _index_fingerprint(index: Path) -> str:
    digest = hashlib.sha256()
    digest.update(b"stratum-legacy-v1\x00")
    for name in (META_FILENAME, *LEGACY_JSONL_FILES):
        digest.update(name.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(_file_sha256(index / name).encode("ascii"))
        digest.update(b"\x00")
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


def import_legacy_index(
    index_path: str | Path,
    *,
    workspace: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> LegacyImportResult:
    """Import one legacy JSONL index into the selected workspace.

    The operation is idempotent: a completed run with the same source path
    and content fingerprint is a no-op. Any validation, JSON, reference or
    SQLite failure rolls every data row back and leaves one failed
    ``import_runs`` row behind.
    """

    index = Path(index_path).expanduser()
    source = str(index.resolve()) if index.exists() else str(index.absolute())

    owns_connection = conn is None
    if conn is None:
        selected = resolve_workspace(explicit=workspace)
        database = Path(selected.db_path)
        if str(database) != ":memory:" and not database.is_file():
            raise WorkspaceError(
                f"workspace database not found: {selected.db_path}"
            )
        conn = open_db(selected.db_path)

    try:
        try:
            _validate_layout(index)
            _load_meta(index)
            fingerprint = _index_fingerprint(index)
        except BaseException as exc:
            _record_failed_run(conn, source, exc)
            raise

        previous = conn.execute(
            """
            SELECT id, stats_json FROM import_runs
            WHERE source = ? AND status = 'completed'
            ORDER BY created_at DESC, rowid DESC
            """,
            (source,),
        ).fetchone()
        if previous is not None:
            stats = _decode_stats(previous["stats_json"])
            if stats.get("index_sha256") == fingerprint:
                counts = {
                    name: int(stats.get("counts", {}).get(name, 0))
                    for name in COUNT_ORDER
                }
                return LegacyImportResult(
                    status="unchanged",
                    index=source,
                    counts=counts,
                    run_id=str(previous["id"]),
                )
            _fail(
                "LEGACY_INDEX_CHANGED",
                f"legacy index {source} changed since the last completed "
                "import; import it into a fresh workspace",
            )

        existing = conn.execute(
            "SELECT COUNT(*) FROM legacy_id_map"
        ).fetchone()[0]
        if existing:
            _fail(
                "LEGACY_WORKSPACE_NOT_EMPTY",
                "workspace already contains legacy id mappings; "
                "import into a fresh workspace",
            )

        return _run_import(conn, index, source, fingerprint)
    finally:
        if owns_connection:
            conn.close()


def _decode_stats(value: object) -> dict:
    if not isinstance(value, str) or not value:
        return {}
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _error_text(exc: BaseException) -> str:
    if isinstance(exc, ValidationError):
        return f"{exc.code}: {exc.message}"
    return f"{type(exc).__name__}: {exc}"


def _record_failed_run(
    conn: sqlite3.Connection, source: str, exc: BaseException
) -> None:
    """Record a preflight failure without touching any data rows."""

    try:
        now = utcnow()
        conn.execute(
            "INSERT INTO import_runs (id, source, status, started_at, "
            "finished_at, error) VALUES (?, ?, 'failed', ?, ?, ?)",
            (
                "IMP-" + uuid.uuid4().hex[:16],
                source,
                now,
                now,
                _error_text(exc),
            ),
        )
    except sqlite3.Error:
        pass


def _run_import(
    conn: sqlite3.Connection,
    index: Path,
    source: str,
    fingerprint: str,
) -> LegacyImportResult:
    run_id = "IMP-" + uuid.uuid4().hex[:16]
    conn.execute(
        "INSERT INTO import_runs (id, source, status, started_at) "
        "VALUES (?, ?, 'running', ?)",
        (run_id, source, utcnow()),
    )
    try:
        with transaction(conn):
            counts = _import_all(conn, index)
            stats = {
                "index_sha256": fingerprint,
                "counts": counts,
            }
            conn.execute(
                "UPDATE import_runs SET status = 'completed', "
                "finished_at = ?, stats_json = ? WHERE id = ?",
                (
                    utcnow(),
                    json.dumps(stats, ensure_ascii=False, sort_keys=True),
                    run_id,
                ),
            )
    except BaseException as exc:
        if conn.in_transaction:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
        try:
            conn.execute(
                "UPDATE import_runs SET status = 'failed', finished_at = ?, "
                "error = ? WHERE id = ?",
                (utcnow(), _error_text(exc), run_id),
            )
        except sqlite3.Error:
            pass
        raise
    return LegacyImportResult(
        status="imported",
        index=source,
        counts=counts,
        run_id=run_id,
    )


def _import_all(conn: sqlite3.Connection, index: Path) -> dict[str, int]:
    counts = {name: 0 for name in COUNT_ORDER}
    state = _ImportState()
    profile_id = _ensure_legacy_profile(conn)

    _import_concepts(conn, index / "concept_types.jsonl", counts, state)
    _import_blocks(conn, index / "blocks.jsonl", profile_id, counts, state)
    _import_cards(conn, index / "cards.jsonl", counts, state)
    _import_card_blocks(conn, index / "card_blocks.jsonl", counts, state)
    _import_card_concepts(conn, index / "card_concepts.jsonl", counts, state)
    _import_card_relations(conn, index / "card_relations.jsonl", counts, state)
    _import_reports(conn, index / "reports.jsonl", counts, state)
    _import_report_relations(
        conn, index / "report_relations.jsonl", counts, state
    )
    _apply_document_basenames(conn, state.documents)
    return counts


def _ensure_legacy_profile(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT id FROM profiles WHERE id = ?", (LEGACY_PROFILE_ID,)
    ).fetchone()
    if row is not None:
        return str(row["id"])
    row = conn.execute(
        "SELECT id FROM profiles WHERE name = ? AND version = 1",
        (LEGACY_PROFILE_ID,),
    ).fetchone()
    if row is not None:
        return str(row["id"])

    config = {
        "name": "legacy",
        "version": 1,
        "retrieval_type": "legacy",
        "selector": LEGACY_SELECTOR,
    }
    conn.execute(
        """
        INSERT INTO profiles (
            id, name, version, description, config_json
        ) VALUES (?, ?, 1, ?, ?)
        """,
        (
            LEGACY_PROFILE_ID,
            "legacy",
            "Legacy JSONL import profile",
            json.dumps(config, ensure_ascii=False, sort_keys=True),
        ),
    )
    return LEGACY_PROFILE_ID


def _ensure_document(
    conn: sqlite3.Connection,
    file_path: str,
    counts: dict[str, int],
    *,
    title: str | None = None,
    source_sha256: str | None = None,
) -> str:
    doc_id = normalize_relative_path(file_path)
    if not doc_id:
        _fail(
            "LEGACY_FIELD_INVALID",
            f"document path must be relative and non-empty, got {file_path!r}",
        )
    row = conn.execute(
        "SELECT id, title, source_sha256 FROM documents WHERE id = ?",
        (doc_id,),
    ).fetchone()
    if row is None:
        conn.execute(
            """
            INSERT INTO documents (
                id, path, title, retrieval_type, contract_version,
                source_sha256
            ) VALUES (?, ?, ?, 'legacy', 0, ?)
            """,
            (doc_id, doc_id, title, source_sha256),
        )
        counts["documents"] += 1
        return doc_id

    updates: list[str] = []
    params: list[object] = []
    if title and not row["title"]:
        updates.append("title = ?")
        params.append(title)
    if source_sha256 and not row["source_sha256"]:
        updates.append("source_sha256 = ?")
        params.append(source_sha256)
    if updates:
        updates.append("updated_at = ?")
        params.append(utcnow())
        params.append(doc_id)
        conn.execute(
            f"UPDATE documents SET {', '.join(updates)} WHERE id = ?",
            params,
        )
    return doc_id


def _apply_document_basenames(
    conn: sqlite3.Connection, document_ids: set[str]
) -> None:
    now = utcnow()
    for doc_id in document_ids:
        title = posixpath.basename(doc_id.rstrip("/")) or doc_id
        conn.execute(
            "UPDATE documents SET title = ?, updated_at = ? "
            "WHERE id = ? AND (title IS NULL OR title = '')",
            (title, now, doc_id),
        )


def _concept_sort_key(item: tuple[int, dict]) -> tuple[int, int]:
    level = item[1].get("level") or 1
    try:
        rank = int(level)
    except (TypeError, ValueError):
        rank = 1
    return rank, item[0]


def _insert_legacy_map(
    conn: sqlite3.Connection,
    entity_type: str,
    legacy_id: str,
    new_id: str,
) -> None:
    conn.execute(
        """
        INSERT INTO legacy_id_map (
            id, entity_type, legacy_id, new_id, source
        ) VALUES (?, ?, ?, ?, 'legacy-jsonl')
        """,
        (legacy_map_id(entity_type, legacy_id), entity_type, legacy_id, new_id),
    )


def _import_concepts(
    conn: sqlite3.Connection,
    path: Path,
    counts: dict[str, int],
    state: _ImportState,
) -> None:
    rows: list[tuple[int, dict]] = []
    ids: set[str] = set()
    parent_ids: set[str] = set()
    for lineno, row in _iter_jsonl(path):
        where = f"{path.name}:{lineno}"
        concept_id = _required_text(row, "concept_id", where)
        if concept_id in ids:
            _fail(
                "LEGACY_DUPLICATE_ID",
                f"{where}: duplicate concept_id {concept_id!r}",
            )
        ids.add(concept_id)
        parent_id = _optional_text(row.get("parent_id"))
        if parent_id is not None:
            parent_ids.add(parent_id)
        rows.append((lineno, row))

    for lineno, row in rows:
        where = f"{path.name}:{lineno}"
        parent_id = _optional_text(row.get("parent_id"))
        if parent_id is not None and parent_id not in ids:
            _fail(
                "LEGACY_REFERENCE_MISSING",
                f"{where}: parent concept {parent_id!r} does not exist",
            )

    rows.sort(key=_concept_sort_key)
    for lineno, row in rows:
        where = f"{path.name}:{lineno}"
        concept_id = str(row["concept_id"])
        name = _required_text(row, "name", where)
        description = row.get("definition")
        if description is not None and not isinstance(description, str):
            _fail(
                "LEGACY_FIELD_INVALID",
                f"{where}: definition must be a string",
            )
        level = row.get("level") or 1
        if isinstance(level, bool) or not isinstance(level, int):
            _fail(
                "LEGACY_FIELD_INVALID",
                f"{where}: level must be an integer",
            )
        is_leaf = 1 if level >= 3 or concept_id not in parent_ids else 0
        parent_id = _optional_text(row.get("parent_id"))
        conn.execute(
            """
            INSERT INTO concepts (
                id, name, parent_id, path, is_leaf, description
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                concept_id,
                name,
                parent_id,
                concept_id,
                is_leaf,
                description,
            ),
        )
        state.concepts.add(concept_id)
        _insert_legacy_map(conn, "concept", concept_id, concept_id)
        counts["concepts"] += 1
        counts["legacy_id_map"] += 1


def _import_blocks(
    conn: sqlite3.Connection,
    path: Path,
    profile_id: str,
    counts: dict[str, int],
    state: _ImportState,
) -> None:
    for lineno, row in _iter_jsonl(path):
        where = f"{path.name}:{lineno}"
        legacy_id = _required_text(row, "block_id", where)
        if legacy_id in state.block_new:
            _fail(
                "LEGACY_DUPLICATE_ID",
                f"{where}: duplicate block_id {legacy_id!r}",
            )
        file_path = _required_text(row, "file", where)
        text = row.get("text")
        if not isinstance(text, str):
            _fail(
                "LEGACY_FIELD_INVALID",
                f"{where}: text must be a string",
            )
        line_start = _required_int(row, "line_start", where, 1)
        line_end = _required_int(row, "line_end", where, 1)
        if line_end < line_start:
            _fail(
                "LEGACY_FIELD_INVALID",
                f"{where}: line_end must not be earlier than line_start",
            )
        status = _enum(
            row.get("status", "active"), BLOCK_STATUSES, "status", where
        )

        actual_sha = _sha256_text(text)
        provided_sha = row.get("text_sha256")
        if provided_sha is None or provided_sha == "":
            snapshot_sha = actual_sha
        else:
            if (
                not isinstance(provided_sha, str)
                or provided_sha.lower() != actual_sha
            ):
                _fail(
                    "LEGACY_HASH_MISMATCH",
                    f"{where}: text_sha256 does not match block "
                    f"{legacy_id!r}",
                )
            snapshot_sha = actual_sha

        doc_id = _ensure_document(conn, file_path, counts)
        state.documents.add(doc_id)
        new_id = legacy_block_identity(file_path, legacy_id)
        metadata = {
            "source_kind": row.get("source_kind"),
            "date": row.get("date"),
            "heading": row.get("heading"),
            "source_type": row.get("source_type"),
        }
        conn.execute(
            """
            INSERT INTO blocks (
                id, document_id, profile_id, retrieval_type, selector_type,
                anchor, occurrence, line_start, line_end, snapshot_text,
                snapshot_sha256, status, legacy_block_id, metadata_json
            ) VALUES (?, ?, ?, 'legacy', 'legacy', ?, 0, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id,
                doc_id,
                profile_id,
                legacy_id,
                line_start,
                line_end,
                text,
                snapshot_sha,
                status,
                legacy_id,
                json.dumps(metadata, ensure_ascii=False, sort_keys=True),
            ),
        )
        state.block_new[legacy_id] = new_id
        state.block_text[legacy_id] = text
        _insert_legacy_map(conn, "block", legacy_id, new_id)
        counts["blocks"] += 1
        counts["legacy_id_map"] += 1


def _import_cards(
    conn: sqlite3.Connection,
    path: Path,
    counts: dict[str, int],
    state: _ImportState,
) -> None:
    for lineno, row in _iter_jsonl(path):
        where = f"{path.name}:{lineno}"
        card_id = _required_text(row, "card_id", where)
        if card_id in state.cards:
            _fail(
                "LEGACY_DUPLICATE_ID",
                f"{where}: duplicate card_id {card_id!r}",
            )
        kind = _required_text(row, "kind", where)
        if kind not in CARD_TYPES:
            values = ", ".join(sorted(CARD_TYPES))
            _fail(
                "LEGACY_FIELD_INVALID",
                f"{where}: unsupported card kind {kind!r}; "
                f"expected one of: {values}",
            )
        title = _required_text(row, "title", where)
        statement = _required_text(row, "statement", where)
        status = _enum(
            row.get("status", "active"), CARD_STATUSES, "status", where
        )
        confidence = row.get("confidence")
        if confidence is not None:
            if (
                not isinstance(confidence, str)
                or confidence not in CARD_CONFIDENCES
            ):
                _fail(
                    "LEGACY_FIELD_INVALID",
                    f"{where}: unsupported confidence {confidence!r}",
                )
        period_start, period_end = legacy_card_period(row)
        metadata = {
            "slots": row.get("slots"),
            "time": row.get("time"),
            "end_time": row.get("end_time"),
            "version": row.get("version"),
        }
        conn.execute(
            """
            INSERT INTO cards (
                id, card_type, title, body, status, confidence,
                period_start, period_end, legacy_card_id, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                card_id,
                kind,
                title,
                statement,
                status,
                confidence,
                period_start,
                period_end,
                card_id,
                json.dumps(metadata, ensure_ascii=False, sort_keys=True),
            ),
        )
        state.cards.add(card_id)
        _insert_legacy_map(conn, "card", card_id, card_id)
        counts["cards"] += 1
        counts["legacy_id_map"] += 1


def _import_card_blocks(
    conn: sqlite3.Connection,
    path: Path,
    counts: dict[str, int],
    state: _ImportState,
) -> None:
    for lineno, row in _iter_jsonl(path):
        where = f"{path.name}:{lineno}"
        card_id = _required_text(row, "card_id", where)
        legacy_block_id = _required_text(row, "block_id", where)
        if card_id not in state.cards:
            _fail(
                "LEGACY_REFERENCE_MISSING",
                f"{where}: card {card_id!r} does not exist",
            )
        if legacy_block_id not in state.block_new:
            _fail(
                "LEGACY_REFERENCE_MISSING",
                f"{where}: block {legacy_block_id!r} does not exist",
            )

        quote_sha = row.get("quote_sha")
        if quote_sha is not None:
            if not isinstance(quote_sha, str):
                _fail(
                    "LEGACY_FIELD_INVALID",
                    f"{where}: quote_sha must be a string",
                )
            quote_sha = quote_sha.lower()
            if not _is_sha256(quote_sha):
                _fail(
                    "LEGACY_FIELD_INVALID",
                    f"{where}: quote_sha must be a SHA-256 hex digest",
                )
            allowed = state.block_quote_hashes.get(legacy_block_id)
            if allowed is None:
                allowed = _quote_hashes(state.block_text[legacy_block_id])
                state.block_quote_hashes[legacy_block_id] = allowed
            if quote_sha not in allowed:
                _fail(
                    "LEGACY_HASH_MISMATCH",
                    f"{where}: quote_sha does not match block "
                    f"{legacy_block_id!r}",
                )

        role = row.get("role", "evidence")
        if not isinstance(role, str) or not role.strip():
            _fail(
                "LEGACY_FIELD_INVALID",
                f"{where}: role must be a non-empty string",
            )
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO card_blocks (
                card_id, block_id, quote_sha256, role
            ) VALUES (?, ?, ?, ?)
            """,
            (
                card_id,
                state.block_new[legacy_block_id],
                quote_sha,
                role,
            ),
        )
        if cursor.rowcount:
            counts["card_blocks"] += 1


def _import_card_concepts(
    conn: sqlite3.Connection,
    path: Path,
    counts: dict[str, int],
    state: _ImportState,
) -> None:
    for lineno, row in _iter_jsonl(path):
        where = f"{path.name}:{lineno}"
        card_id = _required_text(row, "card_id", where)
        concept_id = _required_text(row, "concept_id", where)
        if card_id not in state.cards:
            _fail(
                "LEGACY_REFERENCE_MISSING",
                f"{where}: card {card_id!r} does not exist",
            )
        if concept_id not in state.concepts:
            _fail(
                "LEGACY_REFERENCE_MISSING",
                f"{where}: concept {concept_id!r} does not exist",
            )
        cursor = conn.execute(
            "INSERT OR IGNORE INTO card_concepts (card_id, concept_id) "
            "VALUES (?, ?)",
            (card_id, concept_id),
        )
        if cursor.rowcount:
            counts["card_concepts"] += 1


def _import_card_relations(
    conn: sqlite3.Connection,
    path: Path,
    counts: dict[str, int],
    state: _ImportState,
) -> None:
    for lineno, row in _iter_jsonl(path):
        where = f"{path.name}:{lineno}"
        from_card = _required_text(row, "from_card", where)
        to_card = _required_text(row, "to_card", where)
        relation = _required_text(row, "relation", where)
        if from_card not in state.cards:
            _fail(
                "LEGACY_REFERENCE_MISSING",
                f"{where}: card {from_card!r} does not exist",
            )
        if to_card not in state.cards:
            _fail(
                "LEGACY_REFERENCE_MISSING",
                f"{where}: card {to_card!r} does not exist",
            )
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO card_relations (
                id, from_card_id, to_card_id, relation_type, source
            ) VALUES (?, ?, ?, ?, 'manual')
            """,
            (
                card_relation_id(from_card, to_card, relation),
                from_card,
                to_card,
                relation,
            ),
        )
        if cursor.rowcount:
            counts["card_relations"] += 1


def _import_reports(
    conn: sqlite3.Connection,
    path: Path,
    counts: dict[str, int],
    state: _ImportState,
) -> None:
    for lineno, row in _iter_jsonl(path):
        where = f"{path.name}:{lineno}"
        report_id = _required_text(row, "report_id", where)
        if report_id in state.reports:
            _fail(
                "LEGACY_DUPLICATE_ID",
                f"{where}: duplicate report_id {report_id!r}",
            )
        report_type = map_report_type(_required_text(row, "report_type", where))
        title = _required_text(row, "title", where)
        period_start = _parse_date(
            _required_text(row, "period_start", where),
            "period_start",
            where,
        )
        period_end = _parse_date(
            _required_text(row, "period_end", where),
            "period_end",
            where,
        )
        if period_end < period_start:
            _fail(
                "LEGACY_FIELD_INVALID",
                f"{where}: period_end must not be earlier than period_start",
            )
        status = _enum(
            row.get("status", "active"), REPORT_STATUSES, "status", where
        )
        file_path = _required_text(row, "file", where)
        text_sha = row.get("text_sha256")
        if text_sha is not None:
            if not isinstance(text_sha, str) or not _is_sha256(text_sha.lower()):
                _fail(
                    "LEGACY_FIELD_INVALID",
                    f"{where}: text_sha256 must be a SHA-256 hex digest",
                )
            text_sha = text_sha.lower()
        document_id = _ensure_document(
            conn,
            file_path,
            counts,
            title=title,
            source_sha256=text_sha,
        )
        state.documents.add(document_id)
        conn.execute(
            """
            INSERT INTO reports (
                id, report_type, title, period_start, period_end,
                author, contract_version, base_commit, document_id, status
            ) VALUES (?, ?, ?, ?, ?, NULL, 0, NULL, ?, ?)
            """,
            (
                report_id,
                report_type,
                title,
                period_start.isoformat(),
                period_end.isoformat(),
                document_id,
                status,
            ),
        )

        block_ids = row.get("block_ids") or []
        if not isinstance(block_ids, list):
            _fail(
                "LEGACY_FIELD_INVALID",
                f"{where}: block_ids must be a list",
            )
        for legacy_block_id in block_ids:
            if not isinstance(legacy_block_id, str):
                _fail(
                    "LEGACY_FIELD_INVALID",
                    f"{where}: block_ids entries must be strings",
                )
            if legacy_block_id not in state.block_new:
                _fail(
                    "LEGACY_REFERENCE_MISSING",
                    f"{where}: block {legacy_block_id!r} does not exist",
                )
            cursor = conn.execute(
                "INSERT OR IGNORE INTO report_blocks "
                "(report_id, block_id, role) VALUES (?, ?, 'evidence')",
                (report_id, state.block_new[legacy_block_id]),
            )
            if cursor.rowcount:
                counts["report_blocks"] += 1

        card_ids = row.get("card_ids") or []
        if not isinstance(card_ids, list):
            _fail(
                "LEGACY_FIELD_INVALID",
                f"{where}: card_ids must be a list",
            )
        for card_id in card_ids:
            if not isinstance(card_id, str):
                _fail(
                    "LEGACY_FIELD_INVALID",
                    f"{where}: card_ids entries must be strings",
                )
            if card_id not in state.cards:
                _fail(
                    "LEGACY_REFERENCE_MISSING",
                    f"{where}: card {card_id!r} does not exist",
                )
            cursor = conn.execute(
                "INSERT OR IGNORE INTO report_cards "
                "(report_id, card_id, role) VALUES (?, ?, 'reference')",
                (report_id, card_id),
            )
            if cursor.rowcount:
                counts["report_cards"] += 1

        state.reports.add(report_id)
        _insert_legacy_map(conn, "report", report_id, report_id)
        counts["reports"] += 1
        counts["legacy_id_map"] += 1


def _import_report_relations(
    conn: sqlite3.Connection,
    path: Path,
    counts: dict[str, int],
    state: _ImportState,
) -> None:
    for lineno, row in _iter_jsonl(path):
        where = f"{path.name}:{lineno}"
        from_report = _required_text(row, "from_report", where)
        relation = _required_text(row, "relation", where)
        to_report = _optional_text(row.get("to_report"))
        to_card = _optional_text(row.get("to_card"))
        if from_report not in state.reports:
            _fail(
                "LEGACY_REFERENCE_MISSING",
                f"{where}: report {from_report!r} does not exist",
            )
        if (to_report is None) == (to_card is None):
            _fail(
                "LEGACY_FIELD_INVALID",
                f"{where}: exactly one of to_report or to_card is required",
            )

        if to_report is not None:
            if to_report not in state.reports:
                _fail(
                    "LEGACY_REFERENCE_MISSING",
                    f"{where}: report {to_report!r} does not exist",
                )
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO report_relations (
                    id, from_report_id, to_report_id, relation_type
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    report_relation_id(from_report, relation, to_report),
                    from_report,
                    to_report,
                    relation,
                ),
            )
            if cursor.rowcount:
                counts["report_relations"] += 1
            continue

        assert to_card is not None
        if to_card not in state.cards:
            _fail(
                "LEGACY_REFERENCE_MISSING",
                f"{where}: card {to_card!r} does not exist",
            )
        role = "reference" if relation == "observes" else relation
        cursor = conn.execute(
            "INSERT OR IGNORE INTO report_cards (report_id, card_id, role) "
            "VALUES (?, ?, ?)",
            (from_report, to_card, role),
        )
        if cursor.rowcount:
            counts["report_cards"] += 1
