"""Source document ingest: documents, versions, blobs, blocks and lineage.

The public entry point is :func:`ingest_source_file`. It opens one SQLite
transaction and rolls the whole source ingest back on any failure. The
internal :func:`ingest_document` is reused by report ingest so a report and
its document land in the same transaction.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .db import open_db
from .errors import BusinessError, UsageProblem
from .markdown import (
    ScannedDocument,
    normalize_text,
    resolve_default_profile,
    scan_document,
)
from .migrations import utcnow
from .segmentation.identity import BlockDraft, document_id
from .segmentation.profiles import Profile, ProfileSet, builtin_profile_set, load_profiles, segment_document
from .segmentation.resegment import BlockSnapshot, resegment
from .workspace import Workspace, resolve_workspace

__all__ = [
    "SourceIngestResult",
    "ingest_document",
    "ingest_source_file",
    "load_workspace_profiles",
    "resolve_workspace_file",
    "transaction",
]


@dataclass(frozen=True)
class SourceIngestResult:
    status: str
    path: str
    document_id: str
    version: int
    profile_name: str
    created: int = 0
    kept: int = 0
    updated: int = 0
    missing: int = 0
    lineage: int = 0
    blob_id: str | None = None
    document_version_id: str | None = None
    segmentation_run_id: str | None = None


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _short_hash(*parts: object) -> str:
    joined = "\x00".join(str(part) for part in parts)
    return _sha256_bytes(joined.encode("utf-8"))[:16]


def _iso_mtime(path: Path) -> str:
    stamp = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return stamp.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


@contextlib.contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """Run a block in one immediate transaction, rolling back on failure."""

    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def load_workspace_profiles(root: str | Path) -> ProfileSet:
    """Load ``.stratum/profiles`` TOML files, or fall back to builtins."""

    profiles_dir = Path(root) / ".stratum" / "profiles"
    if profiles_dir.is_dir() and any(profiles_dir.glob("*.toml")):
        return load_profiles(profiles_dir)
    return builtin_profile_set()


def _document_title(scanned: ScannedDocument) -> str | None:
    title = scanned.frontmatter_value("title")
    if title:
        return str(title)
    for heading in scanned.headings:
        if heading.level == 1 and heading.heading_text:
            return heading.heading_text
    if scanned.headings and scanned.headings[0].heading_text:
        return scanned.headings[0].heading_text
    return None


def _frontmatter_json(scanned: ScannedDocument) -> str | None:
    if scanned.frontmatter is None:
        return None
    return json.dumps(
        dict(scanned.frontmatter.values),
        ensure_ascii=False,
        sort_keys=True,
    )


def _resolve_profile(
    scanned: ScannedDocument,
    relative_path: str,
    override: str | None,
    profile_set: ProfileSet,
) -> Profile:
    if override is not None:
        return profile_set.get(override)
    name = resolve_default_profile(
        scanned,
        relative_path,
        dict(profile_set.path_profiles),
    )
    return profile_set.get(name)


def _ensure_profile(conn: sqlite3.Connection, profile: Profile) -> str:
    row = conn.execute(
        "SELECT id FROM profiles WHERE name = ? AND version = ?",
        (profile.name, profile.version),
    ).fetchone()
    if row is not None:
        return str(row["id"])

    profile_id = "PRF-" + _short_hash(profile.name, profile.version)
    config = {
        "name": profile.name,
        "version": profile.version,
        "selector": profile.to_selector_spec(),
        "retrieval_type": profile.retrieval_type,
        "fallback": profile.fallback,
    }
    now = utcnow()
    conn.execute(
        """
        INSERT INTO profiles (
            id, name, version, description, config_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            profile_id,
            profile.name,
            profile.version,
            None,
            json.dumps(config, ensure_ascii=False, sort_keys=True),
            now,
            now,
        ),
    )
    return profile_id


def _ensure_blob(conn: sqlite3.Connection, text: str) -> str:
    encoded = text.encode("utf-8")
    sha = _sha256_bytes(encoded)
    row = conn.execute("SELECT id FROM blobs WHERE sha256 = ?", (sha,)).fetchone()
    if row is not None:
        return str(row["id"])

    blob_id = "BLOB-" + sha[:16]
    conn.execute(
        """
        INSERT INTO blobs (id, sha256, media_type, size_bytes, content)
        VALUES (?, ?, 'text/markdown', ?, ?)
        """,
        (blob_id, sha, len(encoded), encoded),
    )
    return blob_id


def _source_state(
    conn: sqlite3.Connection, relative_path: str
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM documents WHERE path = ?", (relative_path,)
    ).fetchone()


def _current_blocks(conn: sqlite3.Connection, identifier: str) -> list[BlockSnapshot]:
    rows = conn.execute(
        """
        SELECT id, anchor, snapshot_text, snapshot_sha256, selector_type,
               line_start, line_end, status
        FROM blocks
        WHERE document_id = ?
        ORDER BY line_start, line_end, id
        """,
        (identifier,),
    ).fetchall()
    return [
        BlockSnapshot(
            block_id=str(row["id"]),
            anchor=str(row["anchor"]),
            snapshot_text=str(row["snapshot_text"]),
            snapshot_sha256=str(row["snapshot_sha256"]),
            selector_type=str(row["selector_type"]),
            line_start=int(row["line_start"]),
            line_end=int(row["line_end"]),
            status=str(row["status"]),
        )
        for row in rows
    ]


def _block_map(drafts: list[BlockDraft]) -> dict[str, BlockDraft]:
    return {draft.block_id: draft for draft in drafts}


def _metadata_json(draft: BlockDraft) -> str | None:
    if not draft.metadata:
        return None
    return json.dumps(dict(draft.metadata), ensure_ascii=False, sort_keys=True)


def _insert_block(
    conn: sqlite3.Connection,
    draft: BlockDraft,
    document_version_id: str,
    *,
    status: str,
) -> bool:
    """Insert a new block row, or revive one that already exists."""

    existing = conn.execute(
        "SELECT 1 FROM blocks WHERE id = ?", (draft.block_id,)
    ).fetchone()
    now = utcnow()
    if existing is None:
        conn.execute(
            """
            INSERT INTO blocks (
                id, document_id, document_version_id, profile_id,
                parent_block_id, retrieval_type, selector_type, anchor,
                occurrence, line_start, line_end, snapshot_text,
                snapshot_sha256, status, legacy_block_id, metadata_json,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                draft.block_id,
                draft.doc_id,
                document_version_id,
                draft.profile_id,
                draft.parent_block_id,
                draft.retrieval_type,
                draft.selector_type,
                draft.anchor,
                draft.occurrence,
                draft.line_start,
                draft.line_end,
                draft.snapshot_text,
                draft.snapshot_sha256,
                status,
                draft.legacy_block_id,
                _metadata_json(draft),
                now,
                now,
            ),
        )
        return True

    _update_block_from_draft(
        conn, str(draft.block_id), draft, document_version_id, status=status
    )
    return False


def _update_block_from_draft(
    conn: sqlite3.Connection,
    block_id: str,
    draft: BlockDraft,
    document_version_id: str,
    *,
    status: str,
) -> None:
    """Refresh an existing block row while preserving its id."""

    now = utcnow()
    conn.execute(
        """
        UPDATE blocks SET
            document_version_id = ?,
            profile_id = ?,
            parent_block_id = ?,
            retrieval_type = ?,
            selector_type = ?,
            anchor = ?,
            occurrence = ?,
            line_start = ?,
            line_end = ?,
            snapshot_text = ?,
            snapshot_sha256 = ?,
            status = ?,
            legacy_block_id = COALESCE(?, legacy_block_id),
            metadata_json = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (
            document_version_id,
            draft.profile_id,
            draft.parent_block_id,
            draft.retrieval_type,
            draft.selector_type,
            draft.anchor,
            draft.occurrence,
            draft.line_start,
            draft.line_end,
            draft.snapshot_text,
            draft.snapshot_sha256,
            status,
            draft.legacy_block_id,
            _metadata_json(draft),
            now,
            block_id,
        ),
    )


def _apply_resegmentation(
    conn: sqlite3.Connection,
    document_version_id: str,
    old_blocks: list[BlockSnapshot],
    drafts: list[BlockDraft],
) -> tuple[int, int, int, int, int]:
    """Apply a W2 resegmentation result. Returns ingest counts."""

    result = resegment(old_blocks, drafts)
    by_id = _block_map(drafts)
    consumed: dict[str, str] = {}
    inserted_ids: set[str] = set()

    for kept in result.kept:
        draft = by_id[kept.new_block_id]
        _update_block_from_draft(
            conn, kept.old_block_id, draft, document_version_id, status="active"
        )
        consumed[kept.old_block_id] = "kept"

    for updated in result.updated:
        draft = by_id[updated.new_block_id]
        _update_block_from_draft(
            conn, updated.old_block_id, draft, document_version_id, status="stale"
        )
        consumed[updated.old_block_id] = "stale"

    for edge in result.lineage:
        draft = by_id[edge.new_block_id]
        if edge.new_block_id not in inserted_ids:
            _insert_block(conn, draft, document_version_id, status="active")
            inserted_ids.add(edge.new_block_id)
        conn.execute(
            """
            INSERT OR IGNORE INTO block_lineage (
                id, old_block_id, new_block_id, reason, method, confidence
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "LIN-" + _short_hash(
                    edge.old_block_id, edge.new_block_id, edge.reason
                ),
                edge.old_block_id,
                edge.new_block_id,
                edge.reason,
                edge.method,
                edge.confidence,
            ),
        )
        consumed[edge.old_block_id] = (
            "stale" if edge.reason in {"split", "merge"} else "missing"
        )

    for created in result.created:
        if created.block_id in inserted_ids:
            continue
        _insert_block(conn, created, document_version_id, status="active")
        inserted_ids.add(created.block_id)

    now = utcnow()
    for snapshot in result.missing:
        consumed[snapshot.block_id] = "missing"
    for old_id, status in consumed.items():
        if status == "kept":
            continue
        conn.execute(
            "UPDATE blocks SET status = ?, updated_at = ? WHERE id = ?",
            (status, now, old_id),
        )

    created_count = len(inserted_ids)
    return (
        created_count,
        len(result.kept),
        len(result.updated),
        len(result.missing),
        len(result.lineage),
    )


def _changed_blocks(
    old_blocks: list[BlockSnapshot], drafts: list[BlockDraft]
) -> bool:
    old_signature = {
        (block.block_id, block.snapshot_sha256) for block in old_blocks
    }
    new_signature = {(draft.block_id, draft.snapshot_sha256) for draft in drafts}
    return old_signature != new_signature or len(old_blocks) != len(drafts)


def resolve_workspace_file(
    workspace: Workspace, path: str | Path
) -> tuple[Path, str]:
    """Resolve a file argument and require it to live inside the workspace.

    Relative paths first resolve against the current directory and then
    against the workspace root, so both shell conventions work.
    """

    root = Path(workspace.root).expanduser().resolve()
    raw = Path(path).expanduser()
    if raw.is_absolute():
        candidate = raw.resolve()
    else:
        working = (Path.cwd() / raw).resolve()
        candidate = working if working.is_file() else (root / raw).resolve()

    if not candidate.is_file():
        raise UsageProblem(f"source file not found: {candidate}")
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise UsageProblem(
            f"source file is outside workspace root {root}: {candidate}"
        ) from exc
    return candidate, relative.as_posix()


def ingest_document(
    conn: sqlite3.Connection,
    workspace: Workspace,
    path: str | Path,
    *,
    profile_name: str | None = None,
    profile: str | None = None,
) -> SourceIngestResult:
    """Ingest one Markdown file inside the caller's transaction."""

    if profile_name is None:
        profile_name = profile
    candidate, relative_path = resolve_workspace_file(workspace, path)
    text = normalize_text(candidate.read_bytes())
    scanned = scan_document(text)
    source_sha = _sha256_bytes(text.encode("utf-8"))
    doc_id = document_id(
        scanned.frontmatter.values if scanned.frontmatter is not None else {},
        relative_path,
    )

    profile_set = load_workspace_profiles(Path(workspace.root))
    profile = _resolve_profile(scanned, relative_path, profile_name, profile_set)
    drafts = segment_document(
        scanned,
        profile,
        relative_path=relative_path,
        doc_id=doc_id,
        profile_set=profile_set,
    )

    existing = _source_state(conn, relative_path)
    old_blocks = _current_blocks(conn, doc_id) if existing is not None else []
    same_source = existing is not None and existing["source_sha256"] == source_sha
    if existing is not None and existing["id"] != doc_id:
        raise BusinessError(
            f"document identity conflict at {relative_path}: "
            f"stored id {existing['id']!r}, derived id {doc_id!r}"
        )
    if existing is None:
        other = conn.execute(
            "SELECT path FROM documents WHERE id = ?", (doc_id,)
        ).fetchone()
        if other is not None:
            raise BusinessError(
                f"document id {doc_id!r} already exists at {other['path']!r}"
            )

    max_version_row = conn.execute(
        "SELECT MAX(version) FROM document_versions WHERE document_id = ?",
        (doc_id,),
    ).fetchone()
    current_version = int(max_version_row[0] or 0)

    if same_source and not _changed_blocks(old_blocks, drafts):
        return SourceIngestResult(
            status="unchanged",
            path=relative_path,
            document_id=doc_id,
            version=current_version,
            profile_name=profile.name,
        )

    now = utcnow()
    if existing is None:
        conn.execute(
            """
            INSERT INTO documents (
                id, path, title, retrieval_type, contract_version,
                frontmatter_json, source_sha256, size_bytes, mtime,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?)
            """,
            (
                doc_id,
                relative_path,
                _document_title(scanned),
                profile.retrieval_type,
                _frontmatter_json(scanned),
                source_sha,
                len(text.encode("utf-8")),
                _iso_mtime(candidate),
                now,
                now,
            ),
        )
    else:
        conn.execute(
            """
            UPDATE documents SET
                title = ?,
                retrieval_type = ?,
                frontmatter_json = ?,
                source_sha256 = ?,
                size_bytes = ?,
                mtime = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                _document_title(scanned),
                profile.retrieval_type,
                _frontmatter_json(scanned),
                source_sha,
                len(text.encode("utf-8")),
                _iso_mtime(candidate),
                now,
                doc_id,
            ),
        )

    profile_id = _ensure_profile(conn, profile)
    drafts = [replace(draft, profile_id=profile_id) for draft in drafts]
    blob_id = _ensure_blob(conn, text)
    version = current_version + 1
    version_id = "DVER-" + _short_hash(doc_id, version, source_sha)
    conn.execute(
        """
        INSERT INTO document_versions (
            id, document_id, version, blob_id, source_sha256, line_count,
            created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            version_id,
            doc_id,
            version,
            blob_id,
            source_sha,
            len(scanned.lines),
            now,
        ),
    )

    run_id = "SEG-" + _short_hash(doc_id, version, profile_id, source_sha)
    conn.execute(
        """
        INSERT INTO segmentation_runs (
            id, document_id, document_version_id, profile_id,
            selector_count, block_count, status, options_json,
            started_at, finished_at, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, 'completed', ?, ?, ?, ?)
        """,
        (
            run_id,
            doc_id,
            version_id,
            profile_id,
            len(drafts),
            len(drafts),
            json.dumps({"profile": profile.name}, ensure_ascii=False, sort_keys=True),
            now,
            now,
            now,
        ),
    )

    created, kept, updated, missing, lineage = _apply_resegmentation(
        conn, version_id, old_blocks, drafts
    )
    return SourceIngestResult(
        status="created" if existing is None else "updated",
        path=relative_path,
        document_id=doc_id,
        version=version,
        profile_name=profile.name,
        created=created,
        kept=kept,
        updated=updated,
        missing=missing,
        lineage=lineage,
        blob_id=blob_id,
        document_version_id=version_id,
        segmentation_run_id=run_id,
    )


def ingest_source_file(
    path: str | Path,
    *,
    profile_name: str | None = None,
    profile: str | None = None,
    workspace: str | None = None,
) -> SourceIngestResult:
    """Ingest one source file in its own transaction."""

    if profile_name is None:
        profile_name = profile
    selected = resolve_workspace(explicit=workspace)
    conn = open_db(selected.db_path)
    try:
        with transaction(conn):
            return ingest_document(
                conn, selected, path, profile_name=profile_name
            )
    finally:
        conn.close()


# Convenience alias for callers that prefer a shorter verb.
ingest_source = ingest_source_file
