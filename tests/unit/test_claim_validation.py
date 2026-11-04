"""Claim field validation, fingerprints and relation cycle detection."""

from __future__ import annotations

import pytest

from stratum.claims import (
    detect_cycles,
    draft_from_fields,
    fingerprint_fields,
    insert_relation,
    validate_relations,
)
from stratum.db import open_db
from stratum.errors import ValidationError


def _base(**overrides: object) -> dict[str, tuple[str, ...]]:
    fields: dict[str, tuple[str, ...]] = {
        "claim_id": ("OBS-D-2026-01-05-01",),
        "claim_type": ("observation",),
        "period_start": ("2026-01-05",),
        "period_end": ("2026-01-06",),
        "granularity": ("day",),
        "entities": ("self",),
        "evidence": ("C-EXAMPLE-0001",),
        "confidence": ("medium",),
        "body": ("A synthetic observation.",),
    }
    for key, value in overrides.items():
        if value is None:
            fields.pop(key, None)
        elif isinstance(value, tuple):
            fields[key] = value
        else:
            fields[key] = (str(value),)
    return fields


def _code(exc: pytest.ExceptionInfo[ValidationError]) -> str:
    return exc.value.code


def test_valid_observation_draft() -> None:
    draft = draft_from_fields(_base())
    assert draft.claim_id == "OBS-D-2026-01-05-01"
    assert draft.claim_type == "observation"
    assert draft.entities == ("self",)
    assert draft.content_sha256 == fingerprint_fields(_base())


def test_repeated_entities_are_preserved() -> None:
    draft = draft_from_fields(_base(entities=("self", "other")))
    assert draft.entities == ("self", "other")


def test_missing_required_field() -> None:
    with pytest.raises(ValidationError) as exc:
        draft_from_fields(_base(body=None))
    assert _code(exc) == "CLAIM_FIELD_MISSING"


def test_invalid_claim_type() -> None:
    with pytest.raises(ValidationError) as exc:
        draft_from_fields(_base(claim_type="estimate"))
    assert _code(exc) == "CLAIM_TYPE_INVALID"


def test_prefix_must_match_type() -> None:
    with pytest.raises(ValidationError) as exc:
        draft_from_fields(
            _base(claim_id=("CHG-D-2026-01-05-01",))
        )
    assert _code(exc) == "CLAIM_SCALE_INVALID"


def test_scale_must_be_allowed_for_type() -> None:
    with pytest.raises(ValidationError) as exc:
        draft_from_fields(
            _base(
                claim_id=("PAT-D-2026-01-05-01",),
                claim_type=("pattern",),
            )
        )
    assert _code(exc) == "CLAIM_SCALE_INVALID"


def test_granularity_must_match_scale() -> None:
    with pytest.raises(ValidationError) as exc:
        draft_from_fields(_base(granularity=("week",)))
    assert _code(exc) == "CLAIM_SCALE_INVALID"


def test_status_cannot_be_authored() -> None:
    with pytest.raises(ValidationError) as exc:
        draft_from_fields(_base(status=("active",)))
    assert _code(exc) == "STATUS_AUTHORED"


def test_unknown_field_for_type() -> None:
    with pytest.raises(ValidationError) as exc:
        draft_from_fields(_base(conditions=("condition",)))
    assert _code(exc) == "CLAIM_FIELD_UNKNOWN"


def test_duplicate_scalar_field() -> None:
    with pytest.raises(ValidationError) as exc:
        draft_from_fields(
            _base(confidence=("medium", "low"))
        )
    assert _code(exc) == "CLAIM_FIELD_DUPLICATE"


def test_evidence_or_data_gap_required() -> None:
    with pytest.raises(ValidationError) as exc:
        draft_from_fields(_base(evidence=None))
    assert _code(exc) == "EVIDENCE_MISSING"


def test_data_gap_only_is_accepted() -> None:
    draft = draft_from_fields(
        _base(evidence=None, data_gap=("no source coverage",))
    )
    assert draft.evidence == ()
    assert draft.data_gap == "no source coverage"


def test_hypothesis_cannot_be_high_confidence() -> None:
    with pytest.raises(ValidationError) as exc:
        draft_from_fields(
            _base(
                claim_id=("HYP-B-2026-01-05-01",),
                claim_type=("hypothesis",),
                period_end=("2026-01-19",),
                granularity=("range",),
                confidence=("high",),
                conditions=("condition",),
                outcome=("outcome",),
                support_count=("1",),
                opportunity_count=("1",),
                counterexample_count=("0",),
                counterexample_note=("checked",),
            ),
            report_type="biweekly",
        )
    assert _code(exc) == "CLAIM_CONFIDENCE_INVALID"


def test_change_requires_baseline_and_delta_or_outcome() -> None:
    fields = _base(
        claim_id=("CHG-D-2026-01-05-01",),
        claim_type=("change",),
    )
    with pytest.raises(ValidationError) as exc:
        draft_from_fields(fields)
    assert _code(exc) == "CLAIM_FIELD_MISSING"

    fields["baseline"] = ("old",)
    fields["delta"] = ("+1",)
    draft = draft_from_fields(fields)
    assert draft.baseline == "old"
    assert draft.delta == "+1"


def test_finding_rejects_pattern_fields() -> None:
    with pytest.raises(ValidationError) as exc:
        draft_from_fields(
            _base(
                claim_id=("FND-D-2026-01-05-01",),
                claim_type=("finding",),
                conditions=("condition",),
            )
        )
    assert _code(exc) == "CLAIM_FIELD_UNKNOWN"


def _pattern_fields(**overrides: object) -> dict[str, tuple[str, ...]]:
    fields = _base(
        claim_id=("PAT-B-2026-01-05-01",),
        claim_type=("pattern",),
        period_end=("2026-01-19",),
        granularity=("range",),
        derived_from=("OBS-W-2026-01-05-01",),
        conditions=("condition",),
        outcome=("outcome",),
        support_count=("2",),
        opportunity_count=("3",),
        counterexample_count=("0",),
        counterexample_note=("checked",),
        confidence=("low",),
    )
    for key, value in overrides.items():
        if value is None:
            fields.pop(key, None)
        elif isinstance(value, tuple):
            fields[key] = value
        else:
            fields[key] = (str(value),)
    return fields


def test_pattern_counterexample_rule() -> None:
    fields = _pattern_fields(counterexample_note=None)
    with pytest.raises(ValidationError) as exc:
        draft_from_fields(fields, report_type="biweekly")
    assert _code(exc) == "COUNTEREXAMPLE_MISSING"


def test_pattern_opportunity_count_rule() -> None:
    fields = _pattern_fields(opportunity_count=("2",), counterexample_count=("2",))
    fields["counterexample_cards"] = ("C-EXAMPLE-0002",)
    with pytest.raises(ValidationError) as exc:
        draft_from_fields(fields, report_type="biweekly")
    assert _code(exc) == "CLAIM_FIELD_INVALID"


def test_high_confidence_pattern_needs_three_supports() -> None:
    fields = _pattern_fields(confidence=("high",))
    with pytest.raises(ValidationError) as exc:
        draft_from_fields(fields, report_type="biweekly")
    assert _code(exc) == "CLAIM_CONFIDENCE_INVALID"


def test_period_end_must_follow_start() -> None:
    with pytest.raises(ValidationError) as exc:
        draft_from_fields(_base(period_end=("2026-01-05",)))
    assert _code(exc) == "CLAIM_FIELD_INVALID"


def test_entity_length_is_bounded() -> None:
    with pytest.raises(ValidationError) as exc:
        draft_from_fields(_base(entities=("x" * 81,)))
    assert _code(exc) == "CLAIM_FIELD_INVALID"


def test_add_context_skips_type_specific_requirements() -> None:
    fields = _base(
        claim_id=("PAT-B-2026-01-05-01",),
        claim_type=("pattern",),
        period_end=("2026-01-19",),
        granularity=("range",),
        confidence=("low",),
    )
    draft = draft_from_fields(fields, require_type_fields=False)
    assert draft.claim_type == "pattern"
    assert draft.conditions is None


def test_fingerprint_preserves_repeated_value_order() -> None:
    first = _base(evidence=("C-A", "C-B"))
    second = _base(evidence=("C-B", "C-A"))
    assert fingerprint_fields(first) != fingerprint_fields(second)


def _insert_claim(conn, claim_id: str, claim_type: str = "observation") -> None:
    conn.execute(
        """
        INSERT INTO claims (
            id, claim_type, period_start, period_end, granularity,
            entities_json, body, confidence, status, content_sha256
        ) VALUES (?, ?, '2026-01-05', '2026-01-06', 'day', '[]', 'body',
                  'low', 'draft', 'sha')
        """,
        (claim_id, claim_type),
    )


def test_cycle_detection_rejects_loops() -> None:
    conn = open_db(":memory:")
    try:
        _insert_claim(conn, "OBS-D-2026-01-05-01")
        _insert_claim(conn, "OBS-D-2026-01-05-02")
        insert_relation(
            conn, "OBS-D-2026-01-05-01", "OBS-D-2026-01-05-02", "supersedes"
        )
        insert_relation(
            conn, "OBS-D-2026-01-05-02", "OBS-D-2026-01-05-01", "supersedes"
        )
        with pytest.raises(ValidationError) as exc:
            detect_cycles(conn)
        assert _code(exc) == "CYCLE_DETECTED"
    finally:
        conn.close()


def test_cycle_detection_accepts_acyclic_graph() -> None:
    conn = open_db(":memory:")
    try:
        _insert_claim(conn, "OBS-D-2026-01-05-01")
        _insert_claim(conn, "OBS-D-2026-01-05-02")
        insert_relation(
            conn, "OBS-D-2026-01-05-01", "OBS-D-2026-01-05-02", "supersedes"
        )
        detect_cycles(conn)
    finally:
        conn.close()


def test_relation_layers_for_weekly_claims() -> None:
    conn = open_db(":memory:")
    try:
        _insert_claim(conn, "OBS-D-2026-01-05-01")
        _insert_claim(conn, "OBS-W-2026-01-05-01")
        conn.execute(
            "UPDATE claims SET period_start = '2026-01-05', "
            "period_end = '2026-01-12', granularity = 'week' "
            "WHERE id = 'OBS-W-2026-01-05-01'"
        )
        fields = _base(
            claim_id=("OBS-W-2026-01-05-01",),
            period_end=("2026-01-12",),
            granularity=("week",),
            derived_from=("OBS-D-2026-01-05-01",),
        )
        draft = draft_from_fields(fields, report_type="weekly")
        validate_relations(conn, draft, report_type="weekly")
    finally:
        conn.close()


def test_relation_rejects_reverse_layer() -> None:
    conn = open_db(":memory:")
    try:
        _insert_claim(conn, "OBS-W-2026-01-05-01")
        conn.execute(
            "UPDATE claims SET period_start = '2026-01-05', "
            "period_end = '2026-01-12', granularity = 'week' "
            "WHERE id = 'OBS-W-2026-01-05-01'"
        )
        fields = _base(derived_from=("OBS-W-2026-01-05-01",))
        draft = draft_from_fields(fields)
        with pytest.raises(ValidationError) as exc:
            validate_relations(conn, draft, report_type="daily")
        assert _code(exc) == "DERIVED_FROM_LAYER"
    finally:
        conn.close()
