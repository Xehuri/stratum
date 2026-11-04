"""Legacy mapping helpers: deterministic ids and period derivation."""

from __future__ import annotations

import pytest

from stratum.errors import ValidationError
from stratum.legacy import (
    card_relation_id,
    legacy_block_identity,
    legacy_card_period,
    legacy_map_id,
    map_report_type,
    report_relation_id,
)


def test_block_identity_is_deterministic_and_file_sensitive():
    first = legacy_block_identity("data/daily/2026-01-01/a.md", "L-0001")

    assert first.startswith("BLK-")
    assert first == legacy_block_identity(
        "data/daily/2026-01-01/a.md", "L-0001"
    )
    assert first != legacy_block_identity(
        "data/daily/2026-01-01/b.md", "L-0001"
    )
    assert first != legacy_block_identity(
        "data/daily/2026-01-01/a.md", "L-0002"
    )


def test_block_identity_normalises_windows_separators():
    assert legacy_block_identity(
        "data\\daily\\a.md", "L-0001"
    ) == legacy_block_identity("data/daily/a.md", "L-0001")


def test_card_period_falls_back_to_date_plus_one_day():
    assert legacy_card_period({"date": "2026-01-01"}) == (
        "2026-01-01",
        "2026-01-02",
    )


def test_card_period_prefers_explicit_period_fields():
    assert legacy_card_period(
        {
            "date": "2026-01-01",
            "period_start": "2026-01-01",
            "period_end": "2026-01-03",
        }
    ) == ("2026-01-01", "2026-01-03")


def test_card_period_returns_null_without_dates():
    assert legacy_card_period({}) == (None, None)


def test_card_period_rejects_reversed_period():
    with pytest.raises(ValidationError) as excinfo:
        legacy_card_period(
            {"period_start": "2026-01-03", "period_end": "2026-01-01"}
        )
    assert excinfo.value.code == "LEGACY_FIELD_INVALID"


def test_program_report_type_maps_to_summary():
    assert map_report_type("program") == "summary"
    assert map_report_type("daily") == "daily"


def test_unknown_report_type_is_rejected():
    with pytest.raises(ValidationError) as excinfo:
        map_report_type("mystery")
    assert excinfo.value.code == "LEGACY_FIELD_INVALID"


def test_relation_and_map_ids_are_deterministic_and_distinct():
    assert legacy_map_id("block", "L-0001") == legacy_map_id(
        "block", "L-0001"
    )
    assert legacy_map_id("block", "L-0001") != legacy_map_id(
        "card", "L-0001"
    )

    assert card_relation_id("C-1", "C-2", "precedes") == card_relation_id(
        "C-1", "C-2", "precedes"
    )
    assert card_relation_id("C-1", "C-2", "precedes") != card_relation_id(
        "C-2", "C-1", "precedes"
    )

    assert report_relation_id("D-1", "covers", "W-1") == report_relation_id(
        "D-1", "covers", "W-1"
    )
    assert report_relation_id("D-1", "covers", "W-1") != report_relation_id(
        "D-1", "covers", "W-2"
    )
