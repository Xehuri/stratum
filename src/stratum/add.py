"""Manual draft creation: ``stratum add card``, ``add claim`` and ``add relation``.

All commands are idempotent for identical content. Cards link structured
blocks as evidence; claims reuse the report contract validators and evidence
resolution so manual drafts cannot bypass the claim rules. Card relations are
the explicit causal/temporal graph between cards.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import date as date_type
from datetime import timedelta

from .claims import (
    ClaimDraft,
    delete_evidence,
    draft_from_fields,
    insert_claim_row,
    insert_evidence_link,
    resolve_evidence,
    update_claim_row,
)
from .db import open_db
from .errors import BusinessError, UsageProblem, ValidationError
from .ingest_source import transaction
from .migrations import utcnow
from .workspace import resolve_workspace

__all__ = [
    "AddCardResult",
    "AddClaimResult",
    "AddRelationResult",
    "CARD_TYPES",
    "CARD_RELATION_TYPES",
    "add_card",
    "add_claim",
    "add_relation",
]

CARD_TYPES = ("event", "observation", "finding", "state", "decision")
CARD_RELATION_TYPES = (
    "derived_from",
    "supported_by",
    "precedes",
    "state_of",
    "triggered_by",
    "same_day",
    "supersedes",
    "contradicts",
)
RELATION_CLAIM_TYPES = ("temporal", "causal", "associative")
DEFAULT_RELATION_CLAIM_TYPE = {
    "precedes": "temporal",
    "same_day": "temporal",
    "triggered_by": "causal",
    "derived_from": "causal",
    "supported_by": "associative",
    "state_of": "associative",
    "supersedes": "associative",
    "contradicts": "associative",
}


@dataclass(frozen=True)
class AddCardResult:
    status: str
    card_id: str
    block_ids: tuple[str, ...]


@dataclass(frozen=True)
class AddClaimResult:
    status: str
    claim_id: str
    evidence_count: int


@dataclass(frozen=True)
class AddRelationResult:
    status: str
    relation_id: str
    from_card_id: str
    to_card_id: str
    relation_type: str
    claim_type: str


def _card_title(title: str | None, body: str) -> str:
    if title is not None:
        text = title.strip()
        if not text:
            raise ValidationError("CARD_INVALID", "card title must not be empty")
        return text
    for line in body.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:80]
    raise ValidationError("CARD_INVALID", "card body must not be empty")


def _card_period(
    date_value: str | None, period_end: str | None
) -> tuple[str | None, str | None]:
    if date_value is None:
        if period_end is not None:
            raise UsageProblem("--period-end requires --date")
        return None, None

    try:
        start = date_type.fromisoformat(date_value)
    except (TypeError, ValueError) as exc:
        raise UsageProblem(
            f"invalid --date {date_value!r}; expected YYYY-MM-DD"
        ) from exc

    if period_end is None:
        end = start + timedelta(days=1)
    else:
        try:
            end = date_type.fromisoformat(period_end)
        except (TypeError, ValueError) as exc:
            raise UsageProblem(
                f"invalid --period-end {period_end!r}; expected YYYY-MM-DD"
            ) from exc
        if end <= start:
            raise UsageProblem("--period-end must be later than --date")

    return start.isoformat(), end.isoformat()


def card_id_for(
    card_type: str,
    body: str,
    block_ids: list[str],
    *,
    period_start: str | None = None,
    period_end: str | None = None,
) -> str:
    payload: dict[str, object] = {
        "type": card_type,
        "body": body,
        "blocks": sorted(block_ids),
    }
    # Keep the legacy hash for undated cards so repeated adds stay idempotent
    # against cards created before periods were introduced.
    if period_start is not None:
        payload["period_start"] = period_start
        payload["period_end"] = period_end
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return "C-" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def _load_blocks(
    conn: sqlite3.Connection, block_ids: list[str]
) -> list[sqlite3.Row]:
    rows: list[sqlite3.Row] = []
    seen: set[str] = set()
    for block_id in block_ids:
        if block_id in seen:
            continue
        seen.add(block_id)
        row = conn.execute("SELECT * FROM blocks WHERE id = ?", (block_id,)).fetchone()
        if row is None:
            raise ValidationError(
                "EVIDENCE_UNRESOLVED", f"card block {block_id} does not exist"
            )
        if row["retrieval_type"] == "prose":
            raise ValidationError(
                "EVIDENCE_PROSE",
                f"card block {block_id} is prose and cannot be evidence",
            )
        rows.append(row)
    return rows


def _workspace_conn(workspace: str | None) -> sqlite3.Connection:
    selected = resolve_workspace(explicit=workspace)
    return open_db(selected.db_path)


def add_card(
    *,
    blocks: list[str],
    card_type: str,
    body: str,
    title: str | None = None,
    author: str | None = None,
    date: str | None = None,
    period_end: str | None = None,
    workspace: str | None = None,
) -> AddCardResult:
    if isinstance(blocks, str):
        blocks = [blocks]
    if card_type not in CARD_TYPES:
        raise ValidationError(
            "CARD_INVALID", f"card type must be one of: {', '.join(CARD_TYPES)}"
        )
    if not body.strip():
        raise ValidationError("CARD_INVALID", "card body must not be empty")
    if not blocks:
        raise UsageProblem("at least one --block is required")
    period_start, period_end = _card_period(date, period_end)

    conn = _workspace_conn(workspace)
    try:
        with transaction(conn):
            selected = _load_blocks(conn, blocks)
            block_ids = [str(row["id"]) for row in selected]
            card_title = _card_title(title, body)
            card_id = card_id_for(
                card_type,
                body,
                block_ids,
                period_start=period_start,
                period_end=period_end,
            )

            existing = conn.execute(
                "SELECT * FROM cards WHERE id = ?", (card_id,)
            ).fetchone()
            if existing is not None:
                for row in selected:
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO card_blocks (
                            card_id, block_id, quote_sha256, role, created_at
                        ) VALUES (?, ?, ?, 'evidence', ?)
                        """,
                        (
                            card_id,
                            str(row["id"]),
                            str(row["snapshot_sha256"]),
                            utcnow(),
                        ),
                    )
                return AddCardResult(
                    status="unchanged",
                    card_id=card_id,
                    block_ids=tuple(block_ids),
                )

            now = utcnow()
            conn.execute(
                """
                INSERT INTO cards (
                    id, card_type, title, body, status, author,
                    period_start, period_end, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'draft', ?, ?, ?, ?, ?)
                """,
                (
                    card_id,
                    card_type,
                    card_title,
                    body,
                    author,
                    period_start,
                    period_end,
                    now,
                    now,
                ),
            )
            for row in selected:
                conn.execute(
                    """
                    INSERT INTO card_blocks (
                        card_id, block_id, quote_sha256, role, created_at
                    ) VALUES (?, ?, ?, 'evidence', ?)
                    """,
                    (
                        card_id,
                        str(row["id"]),
                        str(row["snapshot_sha256"]),
                        now,
                    ),
                )
            return AddCardResult(
                status="created",
                card_id=card_id,
                block_ids=tuple(block_ids),
            )
    finally:
        conn.close()


def add_claim(
    *,
    claim_id: str,
    claim_type: str,
    period_start: str,
    period_end: str,
    granularity: str,
    entities: list[str],
    evidence: list[str],
    confidence: str,
    body: str,
    report_id: str | None = None,
    author: str | None = None,
    workspace: str | None = None,
) -> AddClaimResult:
    if isinstance(entities, str):
        entities = [entities]
    if isinstance(evidence, str):
        evidence = [evidence]
    fields = {
        "claim_id": (claim_id,),
        "claim_type": (claim_type,),
        "period_start": (period_start,),
        "period_end": (period_end,),
        "granularity": (granularity,),
        "entities": tuple(entities),
        "evidence": tuple(evidence),
        "confidence": (confidence,),
        "body": (body,),
    }
    draft = draft_from_fields(
        fields,
        report_type=None,
        report_id=report_id,
        author=author,
        require_type_fields=False,
        where=f"claim {claim_id}",
    )

    conn = _workspace_conn(workspace)
    try:
        with transaction(conn):
            if report_id is not None:
                report = conn.execute(
                    "SELECT 1 FROM reports WHERE id = ?", (report_id,)
                ).fetchone()
                if report is None:
                    raise BusinessError(f"report not found: {report_id}")

            existing = conn.execute(
                "SELECT * FROM claims WHERE id = ?", (claim_id,)
            ).fetchone()
            if existing is not None:
                stored_report = existing["report_id"]
                if (stored_report if stored_report else None) != report_id:
                    raise ValidationError(
                        "CLAIM_ID_CONFLICT",
                        f"claim id {claim_id} already belongs to another report",
                    )
                if str(existing["content_sha256"]) == draft.content_sha256:
                    return AddClaimResult(
                        status="unchanged",
                        claim_id=claim_id,
                        evidence_count=len(draft.evidence),
                    )
                if existing["status"] != "draft":
                    raise ValidationError(
                        "CLAIM_IMMUTABLE",
                        f"claim {claim_id} is {existing['status']} and cannot change",
                    )
                update_claim_row(conn, draft)
                delete_evidence(conn, claim_id)
                count = _write_evidence(conn, draft)
                return AddClaimResult(
                    status="updated", claim_id=claim_id, evidence_count=count
                )

            insert_claim_row(conn, draft)
            count = _write_evidence(conn, draft)
            return AddClaimResult(
                status="created", claim_id=claim_id, evidence_count=count
            )
    finally:
        conn.close()


def add_relation(
    *,
    from_card: str,
    to_card: str,
    relation_type: str,
    claim_type: str | None = None,
    workspace: str | None = None,
) -> AddRelationResult:
    if relation_type not in CARD_RELATION_TYPES:
        raise ValidationError(
            "CARD_RELATION_INVALID",
            f"relation type must be one of: {', '.join(CARD_RELATION_TYPES)}",
        )
    if from_card == to_card:
        raise ValidationError(
            "CARD_RELATION_INVALID", "a card cannot relate to itself"
        )
    resolved_claim_type = claim_type or DEFAULT_RELATION_CLAIM_TYPE[relation_type]
    if resolved_claim_type not in RELATION_CLAIM_TYPES:
        raise ValidationError(
            "CARD_RELATION_INVALID",
            f"claim type must be one of: {', '.join(RELATION_CLAIM_TYPES)}",
        )

    conn = _workspace_conn(workspace)
    try:
        with transaction(conn):
            for card_id in (from_card, to_card):
                if conn.execute(
                    "SELECT 1 FROM cards WHERE id = ?", (card_id,)
                ).fetchone() is None:
                    raise ValidationError(
                        "CARD_NOT_FOUND", f"card {card_id} does not exist"
                    )
            existing = conn.execute(
                """
                SELECT id FROM card_relations
                WHERE from_card_id = ? AND to_card_id = ? AND relation_type = ?
                """,
                (from_card, to_card, relation_type),
            ).fetchone()
            if existing is not None:
                return AddRelationResult(
                    status="unchanged",
                    relation_id=str(existing["id"]),
                    from_card_id=from_card,
                    to_card_id=to_card,
                    relation_type=relation_type,
                    claim_type=resolved_claim_type,
                )
            relation_id = "REL-" + hashlib.sha256(
                f"{from_card}\x00{to_card}\x00{relation_type}".encode("utf-8")
            ).hexdigest()[:16]
            conn.execute(
                """
                INSERT INTO card_relations (
                    id, from_card_id, to_card_id, relation_type,
                    claim_type, source, created_at
                ) VALUES (?, ?, ?, ?, ?, 'agent', ?)
                """,
                (
                    relation_id,
                    from_card,
                    to_card,
                    relation_type,
                    resolved_claim_type,
                    utcnow(),
                ),
            )
            return AddRelationResult(
                status="created",
                relation_id=relation_id,
                from_card_id=from_card,
                to_card_id=to_card,
                relation_type=relation_type,
                claim_type=resolved_claim_type,
            )
    finally:
        conn.close()


def _write_evidence(conn: sqlite3.Connection, draft: ClaimDraft) -> int:
    count = 0
    for ref in draft.evidence:
        target = resolve_evidence(
            conn, ref, report_document_id=None, sections=None,
            where=f"claim {draft.claim_id}",
        )
        insert_evidence_link(conn, draft.claim_id, target)
        count += 1
    return count
