"""Review state machine for claims and cards.

The database status on ``claims.status`` and ``cards.status`` is the state.
``review_events`` is the durable audit trail for every promotion, rejection,
reopen and supersede. Promotions are checked against the frozen claim
contract: evidence must resolve to active cards or non-missing, non-prose
blocks, snapshots must match, and every ``derived_from`` target must already
be active. All writes happen inside one SQLite transaction.

Only ``draft`` and ``stale`` claims can be promoted. Active claims are
immutable; callers must create a new claim with ``supersedes`` and promote
that claim instead.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from typing import Mapping

from .claims import parse_claim_id
from .errors import UsageProblem, ValidationError
from .ingest_source import transaction
from .migrations import utcnow

__all__ = [
    "BulkReviewResult",
    "ReviewDetail",
    "ReviewListItem",
    "ReviewResult",
    "bulk_promote",
    "list_items",
    "mark_stale",
    "promote",
    "reject",
    "reopen",
    "show_item",
]

CARD_STATUSES = ("draft", "active", "superseded", "rejected", "stale")
CLAIM_STATUSES = ("draft", "active", "superseded", "rejected", "stale")
ALL_ENTITY_STATUSES = (
    "draft",
    "active",
    "superseded",
    "rejected",
    "stale",
    "archived",
    "missing",
)

PROMOTION_AUTHORS = ("human", "main")
SELF_PROMOTION_AUTHOR = "03-biweekly-review"

# The transitions that this module implements. ``active -> stale`` is handled
# by mark_stale; the review CLI intentionally exposes only promote/reject/
# reopen because stale propagation is an evidence-integrity operation.
CLAIM_TRANSITIONS = frozenset(
    {
        ("draft", "active"),
        ("draft", "rejected"),
        ("rejected", "draft"),
        ("active", "stale"),
        ("stale", "active"),
        ("active", "superseded"),
        ("stale", "superseded"),
    }
)
CARD_TRANSITIONS = frozenset(
    {
        ("draft", "active"),
        ("draft", "rejected"),
        ("rejected", "draft"),
        ("active", "superseded"),
    }
)


@dataclass(frozen=True)
class ReviewResult:
    entity_type: str
    entity_id: str
    action: str
    from_status: str
    to_status: str
    changed: bool


@dataclass(frozen=True)
class BulkReviewResult:
    entity_type: str
    day: str
    promoted: tuple[str, ...]
    unchanged: tuple[str, ...]
    results: tuple[ReviewResult, ...]

    @property
    def promoted_count(self) -> int:
        return len(self.promoted)


@dataclass(frozen=True)
class ReviewListItem:
    entity_type: str
    entity_id: str
    status: str
    type_label: str
    period_start: str | None
    period_end: str | None
    text: str

    @property
    def period(self) -> str:
        if self.period_start and self.period_end:
            return f"{self.period_start}..{self.period_end}"
        if self.period_start:
            return f"{self.period_start}.."
        return "-"


@dataclass(frozen=True)
class ReviewDetail:
    entity_type: str
    row: Mapping[str, object]
    evidence: tuple[Mapping[str, object], ...]
    relations: tuple[Mapping[str, object], ...]
    events: tuple[Mapping[str, object], ...]

    @property
    def entity_id(self) -> str:
        return str(self.row["id"])

    @property
    def status(self) -> str:
        return str(self.row["status"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_day(value: str) -> str:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise UsageProblem(f"invalid date {value!r}; expected YYYY-MM-DD") from exc
    return parsed.isoformat()


def _require_author_reason(author: str, reason: str) -> tuple[str, str]:
    author_text = (author or "").strip()
    reason_text = (reason or "").strip()
    if not author_text:
        raise UsageProblem("--author is required and must not be empty")
    if not reason_text:
        raise UsageProblem("--reason is required and must not be empty")
    return author_text, reason_text


def _claim_row(conn: sqlite3.Connection, claim_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM claims WHERE id = ?", (claim_id,)).fetchone()


def _card_row(conn: sqlite3.Connection, card_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()


def _not_found(entity_type: str, entity_id: str) -> ValidationError:
    return ValidationError(
        "NOT_FOUND", f"{entity_type} {entity_id} does not exist"
    )


def _invalid_transition(
    entity_type: str, entity_id: str, status: str, action: str
) -> ValidationError:
    return ValidationError(
        "STATE_INVALID",
        f"{entity_type} {entity_id} is {status}; cannot {action}",
    )


def _record_event(
    conn: sqlite3.Connection,
    *,
    entity_type: str,
    entity_id: str,
    action: str,
    from_status: str,
    to_status: str,
    author: str,
    reason: str,
    now: str,
) -> None:
    conn.execute(
        """
        INSERT INTO review_events (
            entity_type, entity_id, action, from_status, to_status,
            author, reason, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            entity_type,
            entity_id,
            action,
            from_status,
            to_status,
            author,
            reason,
            now,
        ),
    )


def _update_status(
    conn: sqlite3.Connection,
    table: str,
    entity_id: str,
    status: str,
    now: str,
) -> None:
    # The table name is chosen from a closed internal set, never from input.
    if table not in {"claims", "cards"}:
        raise AssertionError(f"unexpected review table {table!r}")
    conn.execute(
        f"UPDATE {table} SET status = ?, updated_at = ? WHERE id = ?",
        (status, now, entity_id),
    )


def _card_id(entity_id: str) -> bool:
    return entity_id.startswith("C-")


# ---------------------------------------------------------------------------
# Promotion prerequisites
# ---------------------------------------------------------------------------


def _check_claim_evidence(conn: sqlite3.Connection, claim: sqlite3.Row) -> None:
    claim_id = str(claim["id"])
    if claim["data_gap"] is not None:
        raise ValidationError(
            "EVIDENCE_MISSING",
            f"claim {claim_id} has data_gap and cannot be promoted",
        )

    links = conn.execute(
        """
        SELECT * FROM evidence_links
        WHERE owner_type = 'claim' AND owner_id = ?
        ORDER BY target_type, target_id, role
        """,
        (claim_id,),
    ).fetchall()
    if not links:
        raise ValidationError(
            "EVIDENCE_MISSING", f"claim {claim_id} has no evidence"
        )

    for link in links:
        target_type = str(link["target_type"])
        target_id = str(link["target_id"])
        if target_type == "card":
            card = _card_row(conn, target_id)
            if card is None:
                raise ValidationError(
                    "EVIDENCE_UNRESOLVED",
                    f"claim {claim_id} references missing card {target_id}",
                )
            if str(card["status"]) != "active":
                raise ValidationError(
                    "CARD_EVIDENCE_NOT_ACTIVE",
                    f"claim {claim_id} references card {target_id} "
                    f"which is {card['status']}; promote the card first",
                )
            continue
        if target_type == "block":
            block = conn.execute(
                "SELECT * FROM blocks WHERE id = ?", (target_id,)
            ).fetchone()
            if block is None:
                raise ValidationError(
                    "EVIDENCE_UNRESOLVED",
                    f"claim {claim_id} references missing block {target_id}",
                )
            if str(block["retrieval_type"]) == "prose":
                raise ValidationError(
                    "EVIDENCE_PROSE",
                    f"claim {claim_id} references prose block {target_id}",
                )
            if str(block["status"]) == "missing":
                raise ValidationError(
                    "EVIDENCE_UNRESOLVED",
                    f"claim {claim_id} references missing block {target_id}",
                )
            quote = link["quote_sha256"]
            if quote is not None and str(quote) != str(block["snapshot_sha256"]):
                raise ValidationError(
                    "EVIDENCE_SNAPSHOT_MISMATCH",
                    f"claim {claim_id} snapshot for block {target_id} "
                    "no longer matches",
                )
            continue
        raise ValidationError(
            "EVIDENCE_UNRESOLVED",
            f"claim {claim_id} has unsupported evidence target {target_type}",
        )


def _relation_targets(
    conn: sqlite3.Connection, claim_id: str, relation_type: str
) -> list[str]:
    rows = conn.execute(
        """
        SELECT to_claim_id FROM claim_relations
        WHERE from_claim_id = ? AND relation_type = ?
        ORDER BY to_claim_id
        """,
        (claim_id, relation_type),
    ).fetchall()
    return [str(row["to_claim_id"]) for row in rows]


def _assert_no_cycles(conn: sqlite3.Connection, claim_id: str) -> None:
    rows = conn.execute(
        """
        SELECT from_claim_id, to_claim_id FROM claim_relations
        WHERE relation_type IN ('derived_from', 'supersedes')
        """
    ).fetchall()
    adjacency: dict[str, list[str]] = {}
    for row in rows:
        adjacency.setdefault(str(row["from_claim_id"]), []).append(
            str(row["to_claim_id"])
        )

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        if node in visiting:
            raise ValidationError(
                "CYCLE_DETECTED",
                f"promotion of {claim_id} would expose a relation cycle at {node}",
            )
        if node in visited:
            return
        visiting.add(node)
        for target in adjacency.get(node, ()):
            visit(target)
        visiting.remove(node)
        visited.add(node)

    visit(claim_id)


def _check_derived_from(conn: sqlite3.Connection, claim: sqlite3.Row) -> None:
    claim_id = str(claim["id"])
    parsed = parse_claim_id(claim_id)
    targets = _relation_targets(conn, claim_id, "derived_from")

    if parsed.scale == "D":
        if targets:
            raise ValidationError(
                "DERIVED_FROM_LAYER",
                f"daily claim {claim_id} cannot use derived_from",
            )
        return

    if not targets:
        raise ValidationError(
            "DERIVED_FROM_MISSING",
            f"claim {claim_id} requires at least one derived_from target",
        )

    expected_scale = "D" if parsed.scale == "W" else "W"
    for target_id in targets:
        target = _claim_row(conn, target_id)
        if target is None:
            raise ValidationError(
                "DERIVED_FROM_MISSING",
                f"claim {claim_id} derives from missing claim {target_id}",
            )
        target_parsed = parse_claim_id(target_id)
        if target_parsed.scale != expected_scale:
            raise ValidationError(
                "DERIVED_FROM_LAYER",
                f"claim {claim_id} must derive from {expected_scale} claims, "
                f"not {target_id}",
            )
        if parsed.scale == "B" and str(target["claim_type"]) != "observation":
            raise ValidationError(
                "DERIVED_FROM_LAYER",
                f"claim {claim_id} may only derive from weekly observations",
            )
        target_start = str(target["period_start"])
        target_end = str(target["period_end"])
        if (
            target_start < str(claim["period_start"])
            or target_end > str(claim["period_end"])
        ):
            raise ValidationError(
                "DERIVED_FROM_LAYER",
                f"claim {claim_id} derives from {target_id} outside its period",
            )
        if str(target["status"]) != "active":
            raise ValidationError(
                "DERIVED_FROM_NOT_ACTIVE",
                f"claim {claim_id} derives from {target_id} "
                f"which is {target['status']}; promote it first",
            )


def _check_pattern_promotion_author(claim: sqlite3.Row, author: str) -> None:
    if str(claim["claim_type"]) not in {"pattern", "hypothesis"}:
        return
    if author == SELF_PROMOTION_AUTHOR:
        raise ValidationError(
            "SELF_PROMOTION_FORBIDDEN",
            f"{author} cannot promote {claim['id']}; "
            "patterns and hypotheses require human or main",
        )
    if author not in PROMOTION_AUTHORS:
        raise ValidationError(
            "AUTHOR_NOT_ALLOWED",
            f"{author} cannot promote {claim['id']}; "
            "patterns and hypotheses require human or main",
        )


def _supersede_claims(
    conn: sqlite3.Connection,
    claim: sqlite3.Row,
    *,
    author: str,
    reason: str,
    now: str,
) -> list[ReviewResult]:
    results: list[ReviewResult] = []
    claim_id = str(claim["id"])
    for target_id in _relation_targets(conn, claim_id, "supersedes"):
        target = _claim_row(conn, target_id)
        if target is None:
            continue
        from_status = str(target["status"])
        if from_status not in {"active", "stale"}:
            continue
        _update_status(conn, "claims", target_id, "superseded", now)
        _record_event(
            conn,
            entity_type="claim",
            entity_id=target_id,
            action="supersede",
            from_status=from_status,
            to_status="superseded",
            author=author,
            reason=reason,
            now=now,
        )
        results.append(
            ReviewResult(
                entity_type="claim",
                entity_id=target_id,
                action="supersede",
                from_status=from_status,
                to_status="superseded",
                changed=True,
            )
        )
    return results


def _check_card_blocks(conn: sqlite3.Connection, card: sqlite3.Row) -> None:
    card_id = str(card["id"])
    rows = conn.execute(
        """
        SELECT cb.block_id, cb.quote_sha256, b.id AS found_id,
               b.retrieval_type, b.status, b.snapshot_sha256
        FROM card_blocks cb
        LEFT JOIN blocks b ON b.id = cb.block_id
        WHERE cb.card_id = ?
        ORDER BY cb.block_id
        """,
        (card_id,),
    ).fetchall()
    if not rows:
        raise ValidationError(
            "EVIDENCE_MISSING", f"card {card_id} has no card_blocks"
        )
    for row in rows:
        block_id = str(row["block_id"])
        if row["found_id"] is None:
            raise ValidationError(
                "EVIDENCE_UNRESOLVED",
                f"card {card_id} references missing block {block_id}",
            )
        if str(row["retrieval_type"]) == "prose":
            raise ValidationError(
                "EVIDENCE_PROSE",
                f"card {card_id} references prose block {block_id}",
            )
        if str(row["status"]) != "active":
            raise ValidationError(
                "BLOCK_NOT_ACTIVE",
                f"card {card_id} references block {block_id} "
                f"which is {row['status']}",
            )
        quote = row["quote_sha256"]
        if quote is None or str(quote) != str(row["snapshot_sha256"]):
            raise ValidationError(
                "EVIDENCE_SNAPSHOT_MISMATCH",
                f"card {card_id} snapshot for block {block_id} "
                "no longer matches",
            )


# ---------------------------------------------------------------------------
# Locked transitions (caller owns the transaction)
# ---------------------------------------------------------------------------


def _promote_claim_locked(
    conn: sqlite3.Connection,
    claim: sqlite3.Row,
    *,
    author: str,
    reason: str,
    now: str,
) -> ReviewResult:
    claim_id = str(claim["id"])
    from_status = str(claim["status"])
    if from_status == "active":
        return ReviewResult(
            entity_type="claim",
            entity_id=claim_id,
            action="promote",
            from_status=from_status,
            to_status=from_status,
            changed=False,
        )
    if (from_status, "active") not in CLAIM_TRANSITIONS:
        raise _invalid_transition("claim", claim_id, from_status, "promote")

    _check_pattern_promotion_author(claim, author)
    _check_claim_evidence(conn, claim)
    _assert_no_cycles(conn, claim_id)
    _check_derived_from(conn, claim)

    _update_status(conn, "claims", claim_id, "active", now)
    _record_event(
        conn,
        entity_type="claim",
        entity_id=claim_id,
        action="promote",
        from_status=from_status,
        to_status="active",
        author=author,
        reason=reason,
        now=now,
    )
    _supersede_claims(conn, claim, author=author, reason=reason, now=now)
    return ReviewResult(
        entity_type="claim",
        entity_id=claim_id,
        action="promote",
        from_status=from_status,
        to_status="active",
        changed=True,
    )


def _promote_card_locked(
    conn: sqlite3.Connection,
    card: sqlite3.Row,
    *,
    author: str,
    reason: str,
    now: str,
) -> ReviewResult:
    card_id = str(card["id"])
    from_status = str(card["status"])
    if from_status == "active":
        return ReviewResult(
            entity_type="card",
            entity_id=card_id,
            action="promote",
            from_status=from_status,
            to_status=from_status,
            changed=False,
        )
    if (from_status, "active") not in CARD_TRANSITIONS:
        raise _invalid_transition("card", card_id, from_status, "promote")

    _check_card_blocks(conn, card)
    _update_status(conn, "cards", card_id, "active", now)
    _record_event(
        conn,
        entity_type="card",
        entity_id=card_id,
        action="promote",
        from_status=from_status,
        to_status="active",
        author=author,
        reason=reason,
        now=now,
    )
    return ReviewResult(
        entity_type="card",
        entity_id=card_id,
        action="promote",
        from_status=from_status,
        to_status="active",
        changed=True,
    )


def _reject_locked(
    conn: sqlite3.Connection,
    entity_type: str,
    row: sqlite3.Row,
    *,
    author: str,
    reason: str,
    now: str,
) -> ReviewResult:
    entity_id = str(row["id"])
    from_status = str(row["status"])
    if from_status == "rejected":
        return ReviewResult(
            entity_type=entity_type,
            entity_id=entity_id,
            action="reject",
            from_status=from_status,
            to_status=from_status,
            changed=False,
        )
    if from_status != "draft":
        raise _invalid_transition(entity_type, entity_id, from_status, "reject")
    _update_status(
        conn, "claims" if entity_type == "claim" else "cards",
        entity_id, "rejected", now,
    )
    _record_event(
        conn,
        entity_type=entity_type,
        entity_id=entity_id,
        action="reject",
        from_status=from_status,
        to_status="rejected",
        author=author,
        reason=reason,
        now=now,
    )
    return ReviewResult(
        entity_type=entity_type,
        entity_id=entity_id,
        action="reject",
        from_status=from_status,
        to_status="rejected",
        changed=True,
    )


def _reopen_locked(
    conn: sqlite3.Connection,
    entity_type: str,
    row: sqlite3.Row,
    *,
    author: str,
    reason: str,
    now: str,
) -> ReviewResult:
    entity_id = str(row["id"])
    from_status = str(row["status"])
    if from_status != "rejected":
        raise _invalid_transition(entity_type, entity_id, from_status, "reopen")
    _update_status(
        conn, "claims" if entity_type == "claim" else "cards",
        entity_id, "draft", now,
    )
    _record_event(
        conn,
        entity_type=entity_type,
        entity_id=entity_id,
        action="reopen",
        from_status=from_status,
        to_status="draft",
        author=author,
        reason=reason,
        now=now,
    )
    return ReviewResult(
        entity_type=entity_type,
        entity_id=entity_id,
        action="reopen",
        from_status=from_status,
        to_status="draft",
        changed=True,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def promote(
    conn: sqlite3.Connection, entity_id: str, *, author: str, reason: str
) -> ReviewResult:
    author_text, reason_text = _require_author_reason(author, reason)
    with transaction(conn):
        now = utcnow()
        if _card_id(entity_id):
            card = _card_row(conn, entity_id)
            if card is None:
                raise _not_found("card", entity_id)
            return _promote_card_locked(
                conn, card, author=author_text, reason=reason_text, now=now
            )
        claim = _claim_row(conn, entity_id)
        if claim is None:
            raise _not_found("claim", entity_id)
        return _promote_claim_locked(
            conn, claim, author=author_text, reason=reason_text, now=now
        )


def reject(
    conn: sqlite3.Connection, entity_id: str, *, author: str, reason: str
) -> ReviewResult:
    author_text, reason_text = _require_author_reason(author, reason)
    with transaction(conn):
        now = utcnow()
        if _card_id(entity_id):
            card = _card_row(conn, entity_id)
            if card is None:
                raise _not_found("card", entity_id)
            return _reject_locked(
                conn, "card", card,
                author=author_text, reason=reason_text, now=now,
            )
        claim = _claim_row(conn, entity_id)
        if claim is None:
            raise _not_found("claim", entity_id)
        return _reject_locked(
            conn, "claim", claim,
            author=author_text, reason=reason_text, now=now,
        )


def reopen(
    conn: sqlite3.Connection, entity_id: str, *, author: str, reason: str
) -> ReviewResult:
    author_text, reason_text = _require_author_reason(author, reason)
    with transaction(conn):
        now = utcnow()
        if _card_id(entity_id):
            card = _card_row(conn, entity_id)
            if card is None:
                raise _not_found("card", entity_id)
            return _reopen_locked(
                conn, "card", card,
                author=author_text, reason=reason_text, now=now,
            )
        claim = _claim_row(conn, entity_id)
        if claim is None:
            raise _not_found("claim", entity_id)
        return _reopen_locked(
            conn, "claim", claim,
            author=author_text, reason=reason_text, now=now,
        )


def _mark_stale_recursive(
    conn: sqlite3.Connection, claim_id: str, *, now: str
) -> list[ReviewResult]:
    claim = _claim_row(conn, claim_id)
    if claim is None or str(claim["status"]) != "active":
        return []
    _update_status(conn, "claims", claim_id, "stale", now)
    results = [
        ReviewResult(
            entity_type="claim",
            entity_id=claim_id,
            action="stale",
            from_status="active",
            to_status="stale",
            changed=True,
        )
    ]
    dependents = conn.execute(
        """
        SELECT from_claim_id FROM claim_relations
        WHERE relation_type = 'derived_from' AND to_claim_id = ?
        ORDER BY from_claim_id
        """,
        (claim_id,),
    ).fetchall()
    for row in dependents:
        results.extend(
            _mark_stale_recursive(conn, str(row["from_claim_id"]), now=now)
        )
    return results


def mark_stale(
    conn: sqlite3.Connection, entity_id: str, *, author: str, reason: str
) -> tuple[ReviewResult, ...]:
    """Mark an active claim and its active dependents stale.

    This is the evidence-integrity transition, not a review command. It is
    exposed for future fsck/staleness work but deliberately does not write a
    ``review_events`` row because the audit vocabulary is limited to
    promote/reject/reopen/supersede.
    """

    _require_author_reason(author, reason)
    with transaction(conn):
        claim = _claim_row(conn, entity_id)
        if claim is None:
            raise _not_found("claim", entity_id)
        if str(claim["status"]) == "stale":
            return ()
        if str(claim["status"]) != "active":
            raise _invalid_transition(
                "claim", entity_id, str(claim["status"]), "mark stale"
            )
        return tuple(_mark_stale_recursive(conn, entity_id, now=utcnow()))


def bulk_promote(
    conn: sqlite3.Connection,
    *,
    entity_type: str,
    day: str,
    author: str,
    reason: str,
) -> BulkReviewResult:
    author_text, reason_text = _require_author_reason(author, reason)
    if entity_type not in {"claim", "card"}:
        raise UsageProblem("--type must be claim or card")
    day_text = _parse_day(day)

    with transaction(conn):
        now = utcnow()
        if entity_type == "claim":
            rows = conn.execute(
                """
                SELECT * FROM claims
                WHERE period_start = ?
                ORDER BY id
                """,
                (day_text,),
            ).fetchall()
            for row in rows:
                scale = parse_claim_id(str(row["id"])).scale
                if scale != "D":
                    raise ValidationError(
                        "BULK_PROMOTION_INVALID",
                        f"claim {row['id']} is a {scale}-scale claim; "
                        "weekly and biweekly claims must be promoted "
                        "individually",
                    )
            results: list[ReviewResult] = []
            promoted: list[str] = []
            unchanged: list[str] = []
            for row in rows:
                result = _promote_claim_locked(
                    conn, row,
                    author=author_text, reason=reason_text, now=now,
                )
                results.append(result)
                if result.changed:
                    promoted.append(result.entity_id)
                else:
                    unchanged.append(result.entity_id)
            return BulkReviewResult(
                entity_type="claim",
                day=day_text,
                promoted=tuple(promoted),
                unchanged=tuple(unchanged),
                results=tuple(results),
            )

        rows = conn.execute(
            """
            SELECT * FROM cards
            WHERE period_start IS NOT NULL
              AND period_start <= ?
              AND period_end IS NOT NULL
              AND period_end > ?
            ORDER BY id
            """,
            (day_text, day_text),
        ).fetchall()
        results = []
        promoted = []
        unchanged = []
        for row in rows:
            result = _promote_card_locked(
                conn, row,
                author=author_text, reason=reason_text, now=now,
            )
            results.append(result)
            if result.changed:
                promoted.append(result.entity_id)
            else:
                unchanged.append(result.entity_id)
        return BulkReviewResult(
            entity_type="card",
            day=day_text,
            promoted=tuple(promoted),
            unchanged=tuple(unchanged),
            results=tuple(results),
        )


def list_items(
    conn: sqlite3.Connection,
    *,
    entity_type: str | None = None,
    status: str | None = None,
    day: str | None = None,
    limit: int = 50,
) -> list[ReviewListItem]:
    if entity_type not in {None, "claim", "card"}:
        raise UsageProblem("--type must be claim or card")
    if limit < 1:
        raise UsageProblem("--limit must be at least 1")
    if status is not None and status not in ALL_ENTITY_STATUSES:
        raise UsageProblem(f"unknown status {status!r}")
    day_text = _parse_day(day) if day is not None else None
    selected_status = status or "draft"

    items: list[ReviewListItem] = []

    if entity_type in {None, "claim"}:
        sql = [
            "SELECT id, status, claim_type AS type_label,",
            "period_start, period_end, body AS text FROM claims WHERE status = ?",
        ]
        params: list[object] = [selected_status]
        if day_text is not None:
            sql.append("AND period_start <= ? AND period_end > ?")
            params.extend([day_text, day_text])
        sql.append("ORDER BY period_start, id")
        for row in conn.execute(" ".join(sql), params).fetchall():
            items.append(
                ReviewListItem(
                    entity_type="claim",
                    entity_id=str(row["id"]),
                    status=str(row["status"]),
                    type_label=str(row["type_label"]),
                    period_start=row["period_start"],
                    period_end=row["period_end"],
                    text=str(row["text"]),
                )
            )

    if entity_type in {None, "card"}:
        sql = [
            "SELECT id, status, card_type AS type_label,",
            "period_start, period_end, title AS text FROM cards WHERE status = ?",
        ]
        params = [selected_status]
        if day_text is not None:
            sql.append(
                "AND period_start IS NOT NULL AND period_start <= ?"
                " AND period_end IS NOT NULL AND period_end > ?"
            )
            params.extend([day_text, day_text])
        sql.append("ORDER BY COALESCE(period_start, ''), id")
        for row in conn.execute(" ".join(sql), params).fetchall():
            items.append(
                ReviewListItem(
                    entity_type="card",
                    entity_id=str(row["id"]),
                    status=str(row["status"]),
                    type_label=str(row["type_label"]),
                    period_start=row["period_start"],
                    period_end=row["period_end"],
                    text=str(row["text"]),
                )
            )

    items.sort(
        key=lambda item: (
            item.period_start or "",
            item.entity_id,
        )
    )
    return items[:limit]


def _entity_row(
    conn: sqlite3.Connection, entity_id: str
) -> tuple[str, sqlite3.Row]:
    if _card_id(entity_id):
        card = _card_row(conn, entity_id)
        if card is None:
            raise _not_found("card", entity_id)
        return "card", card
    claim = _claim_row(conn, entity_id)
    if claim is None:
        raise _not_found("claim", entity_id)
    return "claim", claim


def show_item(conn: sqlite3.Connection, entity_id: str) -> ReviewDetail:
    entity_type, row = _entity_row(conn, entity_id)
    events = tuple(
        conn.execute(
            """
            SELECT * FROM review_events
            WHERE entity_type = ? AND entity_id = ?
            ORDER BY id
            """,
            (entity_type, entity_id),
        ).fetchall()
    )
    if entity_type == "claim":
        evidence = tuple(
            conn.execute(
                """
                SELECT target_type, target_id, role, quote_sha256, created_at
                FROM evidence_links
                WHERE owner_type = 'claim' AND owner_id = ?
                ORDER BY role, target_type, target_id
                """,
                (entity_id,),
            ).fetchall()
        )
        relations = tuple(
            conn.execute(
                """
                SELECT relation_type, to_claim_id
                FROM claim_relations
                WHERE from_claim_id = ?
                ORDER BY relation_type, to_claim_id
                """,
                (entity_id,),
            ).fetchall()
        )
    else:
        evidence = tuple(
            conn.execute(
                """
                SELECT cb.block_id AS target_id, 'block' AS target_type,
                       cb.role, cb.quote_sha256, b.status AS block_status,
                       b.retrieval_type
                FROM card_blocks cb
                LEFT JOIN blocks b ON b.id = cb.block_id
                WHERE cb.card_id = ?
                ORDER BY cb.block_id
                """,
                (entity_id,),
            ).fetchall()
        )
        relations = ()
    return ReviewDetail(
        entity_type=entity_type,
        row=row,
        evidence=evidence,
        relations=relations,
        events=events,
    )
