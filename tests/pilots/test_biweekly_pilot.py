"""Biweekly pilot: two weeks of weekly observations become PAT/HYP drafts."""

from __future__ import annotations

from pathlib import Path

from stratum.add import add_card, add_claim
from stratum.db import open_db
from stratum.fsck import run_fsck
from stratum.ingest_report import ingest_report_file
from stratum.ingest_source import ingest_source_file
from stratum.query import query_items
from stratum.review import promote
from stratum.workspace import init_workspace

WEEK_ONE_START = "2026-03-02"
WEEK_ONE_END = "2026-03-09"
WEEK_TWO_START = "2026-03-09"
WEEK_TWO_END = "2026-03-16"
BIWEEK_START = "2026-03-02"
BIWEEK_END = "2026-03-16"

DAILY_IDS = (
    "OBS-D-2026-03-02-01",
    "OBS-D-2026-03-03-01",
    "OBS-D-2026-03-09-01",
    "OBS-D-2026-03-10-01",
)
WEEKLY_IDS = (
    "OBS-W-2026-03-02-01",
    "OBS-W-2026-03-02-02",
    "OBS-W-2026-03-09-01",
    "OBS-W-2026-03-02-03",
    "OBS-W-2026-03-09-02",
    "OBS-W-2026-03-09-03",
)


def _init(tmp_path: Path, monkeypatch):
    home = tmp_path / "home"
    root = tmp_path / "notes"
    root.mkdir(parents=True)
    monkeypatch.setenv("STRATUM_HOME", str(home))
    monkeypatch.chdir(tmp_path)
    result = init_workspace(root, name="biweekly-pilot")
    return root, result.workspace


def _write(root: Path, relative: str, text: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def _weekly_report(*, start: str, end: str, claims: str) -> str:
    return f"""---
report_contract: v1
report_type: weekly
report_id: RPT-W-{start}
period_start: {start}
period_end: {end}
granularity: week
author: 04-weekly-review
base_commit: none
created_at: {end}T01:00:00+08:00
timezone: Asia/Shanghai
---

## 叙事

Synthetic weekly narrative for the biweekly pilot.

## 检索条目

{claims}

## 运维

- 命令: stratum status
"""


def _biweekly_report(*, claims: str) -> str:
    return f"""---
report_contract: v1
report_type: biweekly
report_id: RPT-B-{BIWEEK_START}
period_start: {BIWEEK_START}
period_end: {BIWEEK_END}
granularity: range
author: 03-biweekly-review
base_commit: none
created_at: {BIWEEK_END}T01:00:00+08:00
timezone: Asia/Shanghai
---

## 叙事

Synthetic biweekly narrative for the pilot.

## 检索条目

{claims}

## 运维

- 命令: stratum status
"""


def test_biweekly_pilot_end_to_end(tmp_path, monkeypatch):
    root, workspace = _init(tmp_path, monkeypatch)

    source = _write(
        root,
        "sources/biweekly.md",
        "# Biweekly source\n\n## OBS-2026-03-02-01 Synthetic first block\n\n"
        "First synthetic evidence paragraph.\n\n"
        "## OBS-2026-03-02-02 Synthetic second block\n\n"
        "Second synthetic evidence paragraph.\n",
    )
    ingest_source_file(source, workspace=workspace.workspace_id)

    conn = open_db(workspace.db_path)
    try:
        blocks = conn.execute(
            "SELECT id FROM blocks ORDER BY line_start"
        ).fetchall()
        block_ids = [str(row["id"]) for row in blocks]
    finally:
        conn.close()
    assert len(block_ids) == 3

    evidence_card = add_card(
        blocks=[block_ids[1]],
        card_type="observation",
        body="Synthetic positive evidence card.",
        title="Synthetic positive evidence",
        author="04-weekly-review",
        date=WEEK_ONE_START,
        workspace=workspace.workspace_id,
    )
    counterexample_card = add_card(
        blocks=[block_ids[2]],
        card_type="observation",
        body="Synthetic counterexample card.",
        title="Synthetic counterexample",
        author="04-weekly-review",
        date=WEEK_ONE_START,
        workspace=workspace.workspace_id,
    )
    conn = open_db(workspace.db_path)
    try:
        for card_id in (evidence_card.card_id, counterexample_card.card_id):
            promote(conn, card_id, author="04-weekly-review", reason="pilot")
    finally:
        conn.close()

    daily_days = (
        ("2026-03-02", "2026-03-03"),
        ("2026-03-03", "2026-03-04"),
        ("2026-03-09", "2026-03-10"),
        ("2026-03-10", "2026-03-11"),
    )
    for claim_id, (start, end) in zip(DAILY_IDS, daily_days):
        add_claim(
            claim_id=claim_id,
            claim_type="observation",
            period_start=start,
            period_end=end,
            granularity="day",
            entities=["synthetic-reader"],
            evidence=[evidence_card.card_id],
            confidence="medium",
            body=f"Synthetic daily observation {start}.",
            author="02-daily-review",
            workspace=workspace.workspace_id,
        )
        conn = open_db(workspace.db_path)
        try:
            assert (
                promote(
                    conn, claim_id, author="02-daily-review", reason="pilot"
                ).to_status
                == "active"
            )
        finally:
            conn.close()

    week_one_path = _write(
        root,
        f"reports/weekly/{WEEK_ONE_START}.md",
        _weekly_report(
            start=WEEK_ONE_START,
            end=WEEK_ONE_END,
            claims=(
                f"### {WEEKLY_IDS[0]} Synthetic week one observation A\n"
                f"- claim_id: {WEEKLY_IDS[0]}\n"
                "- claim_type: observation\n"
                f"- period_start: {WEEK_ONE_START}\n"
                f"- period_end: {WEEK_ONE_END}\n"
                "- granularity: week\n"
                "- entities: synthetic-reader\n"
                f"- derived_from: {DAILY_IDS[0]}\n"
                f"- evidence: {evidence_card.card_id}\n"
                "- confidence: medium\n"
                "- body: Synthetic week one observation A.\n\n"
                f"### {WEEKLY_IDS[1]} Synthetic week one observation B\n"
                f"- claim_id: {WEEKLY_IDS[1]}\n"
                "- claim_type: observation\n"
                f"- period_start: {WEEK_ONE_START}\n"
                f"- period_end: {WEEK_ONE_END}\n"
                "- granularity: week\n"
                "- entities: synthetic-reader\n"
                f"- derived_from: {DAILY_IDS[1]}\n"
                f"- evidence: {evidence_card.card_id}\n"
                "- confidence: low\n"
                "- body: Synthetic week one observation B.\n\n"
                f"### {WEEKLY_IDS[3]} Synthetic week one observation C\n"
                f"- claim_id: {WEEKLY_IDS[3]}\n"
                "- claim_type: observation\n"
                f"- period_start: {WEEK_ONE_START}\n"
                f"- period_end: {WEEK_ONE_END}\n"
                "- granularity: week\n"
                "- entities: synthetic-reader\n"
                f"- derived_from: {DAILY_IDS[0]}\n"
                f"- evidence: {evidence_card.card_id}\n"
                "- confidence: medium\n"
                "- body: Synthetic week one observation C.\n"
            ),
        ),
    )
    assert (
        ingest_report_file(week_one_path, workspace=workspace.workspace_id)
        .claims_created
        == 3
    )

    week_two_path = _write(
        root,
        f"reports/weekly/{WEEK_TWO_START}.md",
        _weekly_report(
            start=WEEK_TWO_START,
            end=WEEK_TWO_END,
            claims=(
                f"### {WEEKLY_IDS[2]} Synthetic week two observation\n"
                f"- claim_id: {WEEKLY_IDS[2]}\n"
                "- claim_type: observation\n"
                f"- period_start: {WEEK_TWO_START}\n"
                f"- period_end: {WEEK_TWO_END}\n"
                "- granularity: week\n"
                "- entities: synthetic-reader\n"
                f"- derived_from: {DAILY_IDS[2]}\n"
                f"- evidence: {evidence_card.card_id}\n"
                "- confidence: medium\n"
                "- body: Synthetic week two observation A.\n\n"
                f"### {WEEKLY_IDS[4]} Synthetic week two observation B\n"
                f"- claim_id: {WEEKLY_IDS[4]}\n"
                "- claim_type: observation\n"
                f"- period_start: {WEEK_TWO_START}\n"
                f"- period_end: {WEEK_TWO_END}\n"
                "- granularity: week\n"
                "- entities: synthetic-reader\n"
                f"- derived_from: {DAILY_IDS[3]}\n"
                f"- evidence: {evidence_card.card_id}\n"
                "- confidence: low\n"
                "- body: Synthetic week two observation B.\n\n"
                f"### {WEEKLY_IDS[5]} Synthetic week two observation C\n"
                f"- claim_id: {WEEKLY_IDS[5]}\n"
                "- claim_type: observation\n"
                f"- period_start: {WEEK_TWO_START}\n"
                f"- period_end: {WEEK_TWO_END}\n"
                "- granularity: week\n"
                "- entities: synthetic-reader\n"
                f"- derived_from: {DAILY_IDS[2]}\n"
                f"- evidence: {evidence_card.card_id}\n"
                "- confidence: medium\n"
                "- body: Synthetic week two observation C.\n"
            ),
        ),
    )
    assert (
        ingest_report_file(week_two_path, workspace=workspace.workspace_id)
        .claims_created
        == 3
    )

    conn = open_db(workspace.db_path)
    try:
        for claim_id in WEEKLY_IDS:
            assert (
                promote(
                    conn, claim_id, author="04-weekly-review", reason="pilot"
                ).to_status
                == "active"
            )
    finally:
        conn.close()

    pattern_one = (
        f"### PAT-B-{BIWEEK_START}-01 Synthetic pattern one\n"
        f"- claim_id: PAT-B-{BIWEEK_START}-01\n"
        "- claim_type: pattern\n"
        f"- period_start: {BIWEEK_START}\n"
        f"- period_end: {BIWEEK_END}\n"
        "- granularity: range\n"
        "- entities: synthetic-reader\n"
        f"- derived_from: {WEEKLY_IDS[0]}\n"
        f"- derived_from: {WEEKLY_IDS[1]}\n"
        f"- evidence: {evidence_card.card_id}\n"
        "- conditions: two synthetic weeks agree\n"
        "- outcome: the synthetic pattern appears\n"
        "- support_count: 2\n"
        "- opportunity_count: 3\n"
        "- counterexample_count: 1\n"
        f"- counterexample_cards: {counterexample_card.card_id}\n"
        "- confidence: medium\n"
        "- body: Synthetic pattern one across two weeks.\n"
    )
    pattern_two = (
        f"### PAT-B-{BIWEEK_START}-02 Synthetic pattern two\n"
        f"- claim_id: PAT-B-{BIWEEK_START}-02\n"
        "- claim_type: pattern\n"
        f"- period_start: {BIWEEK_START}\n"
        f"- period_end: {BIWEEK_END}\n"
        "- granularity: range\n"
        "- entities: synthetic-reader\n"
        f"- derived_from: {WEEKLY_IDS[1]}\n"
        f"- derived_from: {WEEKLY_IDS[2]}\n"
        f"- evidence: {evidence_card.card_id}\n"
        "- conditions: synthetic condition holds\n"
        "- outcome: synthetic outcome appears\n"
        "- support_count: 2\n"
        "- opportunity_count: 3\n"
        "- counterexample_count: 1\n"
        f"- counterexample_cards: {counterexample_card.card_id}\n"
        "- confidence: low\n"
        "- body: Synthetic pattern two across two weeks.\n"
    )
    hypothesis = (
        f"### HYP-B-{BIWEEK_START}-01 Synthetic hypothesis\n"
        f"- claim_id: HYP-B-{BIWEEK_START}-01\n"
        "- claim_type: hypothesis\n"
        f"- period_start: {BIWEEK_START}\n"
        f"- period_end: {BIWEEK_END}\n"
        "- granularity: range\n"
        "- entities: synthetic-reader\n"
        f"- derived_from: {WEEKLY_IDS[0]}\n"
        f"- derived_from: {WEEKLY_IDS[2]}\n"
        f"- evidence: {evidence_card.card_id}\n"
        "- conditions: synthetic condition may hold\n"
        "- outcome: synthetic outcome may appear\n"
        "- support_count: 1\n"
        "- opportunity_count: 2\n"
        "- counterexample_count: 0\n"
        "- counterexample_note: No counterexample in the synthetic sample.\n"
        "- confidence: low\n"
        "- body: Synthetic hypothesis for a future biweekly review.\n"
    )
    biweekly_path = _write(
        root,
        f"reports/biweekly/{BIWEEK_START}.md",
        _biweekly_report(
            claims="\n\n".join((pattern_one, pattern_two, hypothesis))
        ),
    )
    result = ingest_report_file(biweekly_path, workspace=workspace.workspace_id)
    assert result.claims_created == 3
    assert result.evidence_count == 3
    assert result.relations == 6

    conn = open_db(workspace.db_path)
    try:
        for claim_id in (
            f"PAT-B-{BIWEEK_START}-01",
            f"PAT-B-{BIWEEK_START}-02",
            f"HYP-B-{BIWEEK_START}-01",
        ):
            assert (
                promote(conn, claim_id, author="human", reason="pilot review")
                .to_status
                == "active"
            )

        active = query_items(conn, entity_type="claim", status="active")
        active_ids = {row.entity_id for row in active}
        assert f"PAT-B-{BIWEEK_START}-01" in active_ids
        assert f"PAT-B-{BIWEEK_START}-02" in active_ids
        assert f"HYP-B-{BIWEEK_START}-01" in active_ids
        assert all(row.status == "active" for row in active)

        report = conn.execute(
            "SELECT report_type, period_start, period_end FROM reports "
            "WHERE id = ?",
            (f"RPT-B-{BIWEEK_START}",),
        ).fetchone()
        assert report["report_type"] == "biweekly"
        assert report["period_start"] == BIWEEK_START
        assert report["period_end"] == BIWEEK_END

        fsck = run_fsck(conn)
        assert fsck.findings == ()
        assert fsck.has_errors is False
    finally:
        conn.close()
