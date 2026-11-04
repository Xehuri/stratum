"""Promotion, rejection, reopen and bulk-promotion state machine tests."""

from __future__ import annotations

import sqlite3

import pytest

from stratum.claims import EvidenceTarget, insert_evidence_link, insert_relation
from stratum.db import open_db
from stratum.errors import ValidationError
from stratum.review import (
    bulk_promote,
    promote,
    reject,
    reopen,
)


def _conn(tmp_path) -> sqlite3.Connection:
    conn = open_db(tmp_path / "review.db")
    conn.row_factory = sqlite3.Row
    return conn


def _document(conn: sqlite3.Connection, doc_id: str = "doc-1") -> str:
    conn.execute(
        """
        INSERT OR IGNORE INTO documents (id, path)
        VALUES (?, ?)
        """,
        (doc_id, f"source/{doc_id}.md"),
    )
    return doc_id


def _block(
    conn: sqlite3.Connection,
    block_id: str = "BLK-TEST-0001",
    *,
    retrieval_type: str = "structured",
    status: str = "active",
    snapshot: str = "a" * 64,
    document_id: str = "doc-1",
) -> str:
    _document(conn, document_id)
    conn.execute(
        """
        INSERT INTO blocks (
            id, document_id, retrieval_type, selector_type, anchor,
            line_start, line_end, snapshot_text, snapshot_sha256, status
        ) VALUES (?, ?, ?, 'paragraph', ?, 1, 1, 'snapshot text', ?, ?)
        """,
        (block_id, document_id, retrieval_type, block_id, snapshot, status),
    )
    return block_id


def _card(
    conn: sqlite3.Connection,
    card_id: str = "C-TEST-0001",
    *,
    status: str = "draft",
    period_start: str | None = None,
    period_end: str | None = None,
) -> str:
    conn.execute(
        """
        INSERT INTO cards (
            id, card_type, title, body, status, period_start, period_end,
            granularity
        ) VALUES (?, 'event', 'Test card', 'Card body.', ?, ?, ?, 'day')
        """,
        (card_id, status, period_start, period_end),
    )
    return card_id


def _card_block(
    conn: sqlite3.Connection,
    card_id: str,
    block_id: str,
    *,
    quote: str | None = "a" * 64,
) -> None:
    conn.execute(
        """
        INSERT INTO card_blocks (card_id, block_id, quote_sha256, role)
        VALUES (?, ?, ?, 'evidence')
        """,
        (card_id, block_id, quote),
    )


def _claim(
    conn: sqlite3.Connection,
    claim_id: str,
    *,
    claim_type: str = "observation",
    period_start: str = "2026-01-05",
    period_end: str = "2026-01-06",
    granularity: str = "day",
    status: str = "draft",
    body: str = "Claim body.",
    data_gap: str | None = None,
) -> str:
    conn.execute(
        """
        INSERT INTO claims (
            id, claim_type, period_start, period_end, granularity,
            entities_json, body, confidence, status, content_sha256, data_gap
        ) VALUES (?, ?, ?, ?, ?, '["self"]', ?, 'medium', ?, ?, ?)
        """,
        (
            claim_id,
            claim_type,
            period_start,
            period_end,
            granularity,
            body,
            status,
            claim_id,
            data_gap,
        ),
    )
    return claim_id


def _evidence(
    conn: sqlite3.Connection,
    claim_id: str,
    *,
    target_type: str,
    target_id: str,
    quote: str | None = None,
) -> None:
    insert_evidence_link(
        conn,
        claim_id,
        EvidenceTarget(
            ref=target_id,
            target_type=target_type,
            target_id=target_id,
            quote_sha256=quote,
        ),
    )


def _events(conn: sqlite3.Connection) -> list[tuple]:
    return [
        tuple(row)
        for row in conn.execute(
            """
            SELECT entity_type, entity_id, action, from_status, to_status,
                   author, reason
            FROM review_events ORDER BY id
            """
        )
    ]


def _status(conn: sqlite3.Connection, table: str, entity_id: str) -> str:
    row = conn.execute(
        f"SELECT status FROM {table} WHERE id = ?", (entity_id,)
    ).fetchone()
    assert row is not None
    return str(row["status"])


def test_claim_promotion_requires_evidence_and_records_audit(tmp_path):
    conn = _conn(tmp_path)
    try:
        _claim(conn, "OBS-D-2026-01-05-01")
        conn.commit()

        with pytest.raises(ValidationError, match="EVIDENCE_MISSING"):
            promote(conn, "OBS-D-2026-01-05-01", author="human", reason="checked")
        assert _status(conn, "claims", "OBS-D-2026-01-05-01") == "draft"
        assert _events(conn) == []

        card = _card(conn, status="active")
        _evidence(conn, "OBS-D-2026-01-05-01", target_type="card", target_id=card)
        conn.commit()

        result = promote(
            conn, "OBS-D-2026-01-05-01", author="human", reason="checked"
        )
        assert result.changed is True
        assert result.from_status == "draft"
        assert result.to_status == "active"
        assert _status(conn, "claims", "OBS-D-2026-01-05-01") == "active"
        assert _events(conn) == [
            (
                "claim",
                "OBS-D-2026-01-05-01",
                "promote",
                "draft",
                "active",
                "human",
                "checked",
            )
        ]
        created = conn.execute(
            "SELECT created_at FROM review_events"
        ).fetchone()[0]
        assert created
    finally:
        conn.close()


def test_data_gap_blocks_promotion(tmp_path):
    conn = _conn(tmp_path)
    try:
        card = _card(conn, status="active")
        _claim(conn, "OBS-D-2026-01-05-01", data_gap="no usable evidence")
        _evidence(conn, "OBS-D-2026-01-05-01", target_type="card", target_id=card)
        conn.commit()

        with pytest.raises(ValidationError, match="EVIDENCE_MISSING"):
            promote(conn, "OBS-D-2026-01-05-01", author="human", reason="x")
        assert _events(conn) == []
    finally:
        conn.close()


def test_claim_promotion_requires_active_card_evidence(tmp_path):
    conn = _conn(tmp_path)
    try:
        card = _card(conn, status="draft")
        _claim(conn, "OBS-D-2026-01-05-01")
        _evidence(conn, "OBS-D-2026-01-05-01", target_type="card", target_id=card)
        conn.commit()

        with pytest.raises(ValidationError, match="CARD_EVIDENCE_NOT_ACTIVE"):
            promote(conn, "OBS-D-2026-01-05-01", author="human", reason="x")
        assert _status(conn, "claims", "OBS-D-2026-01-05-01") == "draft"

        block = _block(conn)
        _card_block(conn, card, block, quote="a" * 64)
        conn.commit()
        promote(conn, card, author="human", reason="card checked")
        promote(conn, "OBS-D-2026-01-05-01", author="human", reason="claim checked")
        assert _status(conn, "claims", "OBS-D-2026-01-05-01") == "active"
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("retrieval_type", "status", "quote", "code"),
    [
        ("structured", "missing", None, "EVIDENCE_UNRESOLVED"),
        ("prose", "active", None, "EVIDENCE_PROSE"),
        ("structured", "active", "b" * 64, "EVIDENCE_SNAPSHOT_MISMATCH"),
    ],
)
def test_claim_block_evidence_rules(
    tmp_path, retrieval_type, status, quote, code
):
    conn = _conn(tmp_path)
    try:
        block = _block(
            conn,
            retrieval_type=retrieval_type,
            status=status,
            snapshot="a" * 64,
        )
        _claim(conn, "OBS-D-2026-01-05-01")
        _evidence(
            conn,
            "OBS-D-2026-01-05-01",
            target_type="block",
            target_id=block,
            quote=quote,
        )
        conn.commit()

        with pytest.raises(ValidationError, match=code):
            promote(conn, "OBS-D-2026-01-05-01", author="human", reason="x")
        assert _status(conn, "claims", "OBS-D-2026-01-05-01") == "draft"
    finally:
        conn.close()


def test_claim_promotion_missing_block_evidence(tmp_path):
    conn = _conn(tmp_path)
    try:
        _claim(conn, "OBS-D-2026-01-05-01")
        _evidence(
            conn,
            "OBS-D-2026-01-05-01",
            target_type="block",
            target_id="BLK-DOES-NOT-EXIST",
        )
        conn.commit()
        with pytest.raises(ValidationError, match="EVIDENCE_UNRESOLVED"):
            promote(conn, "OBS-D-2026-01-05-01", author="human", reason="x")
    finally:
        conn.close()


def test_derived_from_must_be_active_and_in_period(tmp_path):
    conn = _conn(tmp_path)
    try:
        card = _card(conn, status="active")
        _claim(conn, "OBS-D-2026-01-05-01", status="active")
        _claim(
            conn,
            "OBS-W-2026-01-05-01",
            claim_type="observation",
            period_start="2026-01-05",
            period_end="2026-01-12",
            granularity="week",
        )
        _evidence(conn, "OBS-W-2026-01-05-01", target_type="card", target_id=card)
        insert_relation(
            conn, "OBS-W-2026-01-05-01", "OBS-D-2026-01-05-01", "derived_from"
        )
        conn.commit()

        # Active dependency requirement is checked before the dependency's
        # own status changes. Mark it draft to exercise the code path.
        conn.execute(
            "UPDATE claims SET status = 'draft' WHERE id = 'OBS-D-2026-01-05-01'"
        )
        conn.commit()
        with pytest.raises(ValidationError, match="DERIVED_FROM_NOT_ACTIVE"):
            promote(conn, "OBS-W-2026-01-05-01", author="human", reason="x")

        conn.execute(
            "UPDATE claims SET status = 'active' WHERE id = 'OBS-D-2026-01-05-01'"
        )
        conn.commit()
        promote(conn, "OBS-W-2026-01-05-01", author="human", reason="x")
        assert _status(conn, "claims", "OBS-W-2026-01-05-01") == "active"
    finally:
        conn.close()


def test_derived_from_rejects_wrong_layer_and_outside_period(tmp_path):
    conn = _conn(tmp_path)
    try:
        card = _card(conn, status="active")
        _claim(
            conn,
            "OBS-W-2026-01-05-01",
            claim_type="observation",
            period_start="2026-01-05",
            period_end="2026-01-12",
            granularity="week",
            status="active",
        )
        _claim(
            conn,
            "OBS-W-2026-01-05-02",
            claim_type="observation",
            period_start="2026-01-05",
            period_end="2026-01-12",
            granularity="week",
        )
        _evidence(conn, "OBS-W-2026-01-05-02", target_type="card", target_id=card)
        insert_relation(
            conn, "OBS-W-2026-01-05-02", "OBS-W-2026-01-05-01", "derived_from"
        )
        conn.commit()
        with pytest.raises(ValidationError, match="DERIVED_FROM_LAYER"):
            promote(conn, "OBS-W-2026-01-05-02", author="human", reason="x")

        conn.execute(
            "DELETE FROM claim_relations WHERE from_claim_id = 'OBS-W-2026-01-05-02'"
        )
        _claim(
            conn,
            "OBS-D-2026-01-19-01",
            status="active",
            period_start="2026-01-19",
            period_end="2026-01-20",
        )
        insert_relation(
            conn, "OBS-W-2026-01-05-02", "OBS-D-2026-01-19-01", "derived_from"
        )
        conn.commit()
        with pytest.raises(ValidationError, match="DERIVED_FROM_LAYER"):
            promote(conn, "OBS-W-2026-01-05-02", author="human", reason="x")
    finally:
        conn.close()


def test_pattern_self_promotion_is_forbidden_and_human_allowed(tmp_path):
    conn = _conn(tmp_path)
    try:
        card = _card(conn, status="active")
        _claim(
            conn,
            "OBS-W-2026-01-05-01",
            claim_type="observation",
            period_start="2026-01-05",
            period_end="2026-01-12",
            granularity="week",
            status="active",
        )
        _claim(
            conn,
            "PAT-B-2026-01-05-01",
            claim_type="pattern",
            period_start="2026-01-05",
            period_end="2026-01-19",
            granularity="range",
        )
        _evidence(conn, "PAT-B-2026-01-05-01", target_type="card", target_id=card)
        insert_relation(
            conn, "PAT-B-2026-01-05-01", "OBS-W-2026-01-05-01", "derived_from"
        )
        conn.commit()

        with pytest.raises(ValidationError, match="SELF_PROMOTION_FORBIDDEN"):
            promote(
                conn,
                "PAT-B-2026-01-05-01",
                author="03-biweekly-review",
                reason="self review",
            )
        with pytest.raises(ValidationError, match="AUTHOR_NOT_ALLOWED"):
            promote(
                conn,
                "PAT-B-2026-01-05-01",
                author="02-daily-review",
                reason="not allowed",
            )
        assert _status(conn, "claims", "PAT-B-2026-01-05-01") == "draft"

        promote(conn, "PAT-B-2026-01-05-01", author="human", reason="approved")
        assert _status(conn, "claims", "PAT-B-2026-01-05-01") == "active"
        assert _events(conn)[-1][5] == "human"
    finally:
        conn.close()


def test_active_claim_promotion_is_noop(tmp_path):
    conn = _conn(tmp_path)
    try:
        card = _card(conn, status="active")
        _claim(conn, "OBS-D-2026-01-05-01", status="active")
        _evidence(conn, "OBS-D-2026-01-05-01", target_type="card", target_id=card)
        conn.commit()

        before = _events(conn)
        result = promote(conn, "OBS-D-2026-01-05-01", author="human", reason="again")
        assert result.changed is False
        assert result.to_status == "active"
        assert _events(conn) == before == []
    finally:
        conn.close()


def test_supersedes_marks_active_target_superseded(tmp_path):
    conn = _conn(tmp_path)
    try:
        card = _card(conn, status="active")
        _claim(conn, "OBS-D-2026-01-05-01", status="active")
        _claim(conn, "OBS-D-2026-01-05-02")
        _evidence(conn, "OBS-D-2026-01-05-02", target_type="card", target_id=card)
        insert_relation(
            conn, "OBS-D-2026-01-05-02", "OBS-D-2026-01-05-01", "supersedes"
        )
        conn.commit()

        promote(conn, "OBS-D-2026-01-05-02", author="human", reason="correction")
        assert _status(conn, "claims", "OBS-D-2026-01-05-02") == "active"
        assert _status(conn, "claims", "OBS-D-2026-01-05-01") == "superseded"
        events = _events(conn)
        assert any(
            event[:5]
            == ("claim", "OBS-D-2026-01-05-02", "promote", "draft", "active")
            for event in events
        )
        assert any(
            event[:5]
            == ("claim", "OBS-D-2026-01-05-01", "supersede", "active", "superseded")
            for event in events
        )
    finally:
        conn.close()


def test_relation_cycle_blocks_promotion(tmp_path):
    conn = _conn(tmp_path)
    try:
        card = _card(conn, status="active")
        _claim(conn, "OBS-D-2026-01-05-01")
        _claim(conn, "OBS-D-2026-01-05-02")
        _evidence(conn, "OBS-D-2026-01-05-01", target_type="card", target_id=card)
        insert_relation(
            conn, "OBS-D-2026-01-05-01", "OBS-D-2026-01-05-02", "derived_from"
        )
        insert_relation(
            conn, "OBS-D-2026-01-05-02", "OBS-D-2026-01-05-01", "derived_from"
        )
        conn.commit()

        with pytest.raises(ValidationError, match="CYCLE_DETECTED"):
            promote(conn, "OBS-D-2026-01-05-01", author="human", reason="x")
        assert _events(conn) == []
    finally:
        conn.close()


def test_reject_and_reopen_state_machine(tmp_path):
    conn = _conn(tmp_path)
    try:
        card = _card(conn, status="active")
        _claim(conn, "OBS-D-2026-01-05-01")
        _evidence(conn, "OBS-D-2026-01-05-01", target_type="card", target_id=card)
        conn.commit()

        reject(conn, "OBS-D-2026-01-05-01", author="human", reason="not useful")
        assert _status(conn, "claims", "OBS-D-2026-01-05-01") == "rejected"
        again = reject(conn, "OBS-D-2026-01-05-01", author="human", reason="again")
        assert again.changed is False

        with pytest.raises(ValidationError, match="STATE_INVALID"):
            promote(conn, "OBS-D-2026-01-05-01", author="human", reason="x")
        with pytest.raises(ValidationError, match="STATE_INVALID"):
            promote(conn, "OBS-D-2026-01-05-01", author="human", reason="x")

        reopen(conn, "OBS-D-2026-01-05-01", author="human", reason="reconsider")
        assert _status(conn, "claims", "OBS-D-2026-01-05-01") == "draft"
        with pytest.raises(ValidationError, match="STATE_INVALID"):
            reopen(conn, "OBS-D-2026-01-05-01", author="human", reason="again")

        promote(conn, "OBS-D-2026-01-05-01", author="human", reason="approved")
        with pytest.raises(ValidationError, match="STATE_INVALID"):
            reject(conn, "OBS-D-2026-01-05-01", author="human", reason="late")
        with pytest.raises(ValidationError, match="STATE_INVALID"):
            reopen(conn, "OBS-D-2026-01-05-01", author="human", reason="late")

        actions = [event[2] for event in _events(conn)]
        assert actions == ["reject", "reopen", "promote"]
    finally:
        conn.close()


def test_card_promotion_prerequisites_and_audit(tmp_path):
    conn = _conn(tmp_path)
    try:
        card = _card(conn)
        conn.commit()
        with pytest.raises(ValidationError, match="EVIDENCE_MISSING"):
            promote(conn, card, author="human", reason="x")

        block = _block(conn, status="stale")
        _card_block(conn, card, block, quote="a" * 64)
        conn.commit()
        with pytest.raises(ValidationError, match="BLOCK_NOT_ACTIVE"):
            promote(conn, card, author="human", reason="x")

        conn.execute("UPDATE blocks SET status = 'active' WHERE id = ?", (block,))
        conn.execute(
            "UPDATE card_blocks SET quote_sha256 = ? WHERE card_id = ?",
            ("b" * 64, card),
        )
        conn.commit()
        with pytest.raises(ValidationError, match="EVIDENCE_SNAPSHOT_MISMATCH"):
            promote(conn, card, author="human", reason="x")

        conn.execute(
            "UPDATE card_blocks SET quote_sha256 = ? WHERE card_id = ?",
            ("a" * 64, card),
        )
        conn.commit()
        result = promote(conn, card, author="human", reason="checked")
        assert result.to_status == "active"
        assert _status(conn, "cards", card) == "active"
        noop = promote(conn, card, author="human", reason="again")
        assert noop.changed is False
        assert [event[2] for event in _events(conn)] == ["promote"]

        with pytest.raises(ValidationError, match="STATE_INVALID"):
            reject(conn, card, author="human", reason="late")
    finally:
        conn.close()


def test_card_reject_and_reopen(tmp_path):
    conn = _conn(tmp_path)
    try:
        card = _card(conn)
        conn.commit()
        reject(conn, card, author="human", reason="bad card")
        assert _status(conn, "cards", card) == "rejected"
        reject(conn, card, author="human", reason="again")
        reopen(conn, card, author="human", reason="fixed")
        assert _status(conn, "cards", card) == "draft"
        assert [event[2] for event in _events(conn)] == ["reject", "reopen"]
    finally:
        conn.close()


def test_bulk_promote_daily_claims_is_all_or_nothing(tmp_path):
    conn = _conn(tmp_path)
    try:
        card = _card(conn, status="active")
        _claim(conn, "OBS-D-2026-01-05-01")
        _claim(conn, "OBS-D-2026-01-05-02")
        _evidence(conn, "OBS-D-2026-01-05-01", target_type="card", target_id=card)
        _evidence(
            conn,
            "OBS-D-2026-01-05-02",
            target_type="card",
            target_id="C-MISSING-0001",
        )
        conn.commit()

        with pytest.raises(ValidationError, match="EVIDENCE_UNRESOLVED"):
            bulk_promote(
                conn,
                entity_type="claim",
                day="2026-01-05",
                author="human",
                reason="daily review",
            )
        assert _status(conn, "claims", "OBS-D-2026-01-05-01") == "draft"
        assert _status(conn, "claims", "OBS-D-2026-01-05-02") == "draft"
        assert _events(conn) == []

        conn.execute(
            """
            UPDATE evidence_links SET target_id = ?
            WHERE owner_id = 'OBS-D-2026-01-05-02'
            """,
            (card,),
        )
        conn.commit()
        result = bulk_promote(
            conn,
            entity_type="claim",
            day="2026-01-05",
            author="human",
            reason="daily review",
        )
        assert result.promoted_count == 2
        assert _status(conn, "claims", "OBS-D-2026-01-05-01") == "active"
        assert _status(conn, "claims", "OBS-D-2026-01-05-02") == "active"
        assert len(_events(conn)) == 2
    finally:
        conn.close()


def test_bulk_promote_cards_by_period(tmp_path):
    conn = _conn(tmp_path)
    try:
        block = _block(conn)
        card = _card(
            conn,
            period_start="2026-01-05",
            period_end="2026-01-06",
        )
        _card_block(conn, card, block, quote="a" * 64)
        conn.commit()

        result = bulk_promote(
            conn,
            entity_type="card",
            day="2026-01-05",
            author="human",
            reason="daily cards",
        )
        assert result.promoted == (card,)
        assert _status(conn, "cards", card) == "active"
    finally:
        conn.close()


def test_bulk_promote_rejects_weekly_and_biweekly(tmp_path):
    conn = _conn(tmp_path)
    try:
        card = _card(conn, status="active")
        _claim(
            conn,
            "OBS-W-2026-01-05-01",
            claim_type="observation",
            period_start="2026-01-05",
            period_end="2026-01-12",
            granularity="week",
        )
        _evidence(conn, "OBS-W-2026-01-05-01", target_type="card", target_id=card)
        conn.commit()

        with pytest.raises(ValidationError, match="BULK_PROMOTION_INVALID"):
            bulk_promote(
                conn,
                entity_type="claim",
                day="2026-01-05",
                author="human",
                reason="bulk",
            )
        assert _status(conn, "claims", "OBS-W-2026-01-05-01") == "draft"
    finally:
        conn.close()


def test_reject_keeps_evidence_but_rejected_claim_cannot_support_promotion(tmp_path):
    conn = _conn(tmp_path)
    try:
        card = _card(conn, status="active")
        _claim(conn, "OBS-D-2026-01-05-01")
        _evidence(conn, "OBS-D-2026-01-05-01", target_type="card", target_id=card)
        reject(conn, "OBS-D-2026-01-05-01", author="human", reason="no")
        count = conn.execute("SELECT COUNT(*) FROM evidence_links").fetchone()[0]
        assert count == 1

        _claim(conn, "OBS-W-2026-01-05-01", claim_type="observation",
               period_start="2026-01-05", period_end="2026-01-12",
               granularity="week")
        _evidence(conn, "OBS-W-2026-01-05-01", target_type="card", target_id=card)
        insert_relation(
            conn, "OBS-W-2026-01-05-01", "OBS-D-2026-01-05-01", "derived_from"
        )
        conn.commit()
        with pytest.raises(ValidationError, match="DERIVED_FROM_NOT_ACTIVE"):
            promote(conn, "OBS-W-2026-01-05-01", author="human", reason="x")
    finally:
        conn.close()


def test_mark_stale_propagates_and_stale_can_be_repromoted(tmp_path):
    conn = _conn(tmp_path)
    try:
        from stratum.review import mark_stale

        card = _card(conn, status="active")
        _claim(conn, "OBS-D-2026-01-05-01", status="active")
        _evidence(conn, "OBS-D-2026-01-05-01", target_type="card", target_id=card)
        _claim(
            conn,
            "OBS-W-2026-01-05-01",
            claim_type="observation",
            period_start="2026-01-05",
            period_end="2026-01-12",
            granularity="week",
            status="active",
        )
        _evidence(conn, "OBS-W-2026-01-05-01", target_type="card", target_id=card)
        insert_relation(
            conn, "OBS-W-2026-01-05-01", "OBS-D-2026-01-05-01", "derived_from"
        )
        conn.commit()

        results = mark_stale(
            conn,
            "OBS-D-2026-01-05-01",
            author="human",
            reason="evidence changed",
        )
        assert {result.entity_id for result in results} == {
            "OBS-D-2026-01-05-01",
            "OBS-W-2026-01-05-01",
        }
        assert _status(conn, "claims", "OBS-D-2026-01-05-01") == "stale"
        assert _status(conn, "claims", "OBS-W-2026-01-05-01") == "stale"

        # A stale claim whose evidence and dependencies are valid again can
        # be re-promoted.
        promote(conn, "OBS-D-2026-01-05-01", author="human", reason="evidence back")
        promote(conn, "OBS-W-2026-01-05-01", author="human", reason="dependency back")
        assert _status(conn, "claims", "OBS-D-2026-01-05-01") == "active"
        assert _status(conn, "claims", "OBS-W-2026-01-05-01") == "active"
    finally:
        conn.close()
