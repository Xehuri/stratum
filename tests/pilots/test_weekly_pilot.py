"""Weekly pilot: active daily claims become weekly observations."""

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

WEEK_START = "2026-03-02"
WEEK_END = "2026-03-09"
DAILY_IDS = (
    "OBS-D-2026-03-02-01",
    "OBS-D-2026-03-03-01",
    "OBS-D-2026-03-04-01",
)


def _init(tmp_path: Path, monkeypatch):
    home = tmp_path / "home"
    root = tmp_path / "notes"
    root.mkdir(parents=True)
    monkeypatch.setenv("STRATUM_HOME", str(home))
    monkeypatch.chdir(tmp_path)
    result = init_workspace(root, name="weekly-pilot")
    return root, result.workspace


def _write(root: Path, relative: str, text: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def _report(*, claims: str) -> str:
    return f"""---
report_contract: v1
report_type: weekly
report_id: RPT-W-{WEEK_START}
period_start: {WEEK_START}
period_end: {WEEK_END}
granularity: week
author: 04-weekly-review
base_commit: none
created_at: 2026-03-09T01:00:00+08:00
timezone: Asia/Shanghai
---

## 叙事

Synthetic weekly narrative for the pilot.

## 检索条目

{claims}

## 运维

- 命令: stratum status
"""


def test_weekly_pilot_end_to_end(tmp_path, monkeypatch):
    root, workspace = _init(tmp_path, monkeypatch)

    source = _write(
        root,
        "sources/week.md",
        "# Weekly source\n\n## OBS-2026-03-02-01 Synthetic source block\n\n"
        "A synthetic source paragraph used as card evidence.\n",
    )
    ingest_source_file(source, workspace=workspace.workspace_id)

    conn = open_db(workspace.db_path)
    try:
        block_id = str(
            conn.execute(
                "SELECT id FROM blocks ORDER BY line_start LIMIT 1"
            ).fetchone()["id"]
        )
    finally:
        conn.close()

    card = add_card(
        blocks=[block_id],
        card_type="observation",
        body="Synthetic weekly evidence card.",
        title="Synthetic weekly card",
        author="04-weekly-review",
        date=WEEK_START,
        workspace=workspace.workspace_id,
    )
    conn = open_db(workspace.db_path)
    try:
        promote(conn, card.card_id, author="04-weekly-review", reason="pilot")
    finally:
        conn.close()

    for index, claim_id in enumerate(DAILY_IDS):
        day = f"2026-03-0{2 + index}"
        add_claim(
            claim_id=claim_id,
            claim_type="observation",
            period_start=day,
            period_end=f"2026-03-0{3 + index}",
            granularity="day",
            entities=["synthetic-reader"],
            evidence=[card.card_id],
            confidence="medium",
            body=f"Synthetic daily observation {index + 1}.",
            author="02-daily-review",
            workspace=workspace.workspace_id,
        )
        conn = open_db(workspace.db_path)
        try:
            outcome = promote(
                conn, claim_id, author="02-daily-review", reason="pilot"
            )
            assert outcome.to_status == "active"
        finally:
            conn.close()

    weekly_claims = "\n\n".join(
        (
            f"### OBS-W-{WEEK_START}-0{index + 1} Synthetic weekly observation {index + 1}\n"
            f"- claim_id: OBS-W-{WEEK_START}-0{index + 1}\n"
            "- claim_type: observation\n"
            f"- period_start: {WEEK_START}\n"
            f"- period_end: {WEEK_END}\n"
            "- granularity: week\n"
            "- entities: synthetic-reader\n"
            f"- derived_from: {DAILY_IDS[index]}\n"
            f"- evidence: {card.card_id}\n"
            "- confidence: medium\n"
            f"- body: Synthetic weekly aggregation {index + 1}.\n"
        )
        for index in range(len(DAILY_IDS))
    )
    report_path = _write(
        root,
        f"reports/weekly/{WEEK_START}.md",
        _report(claims=weekly_claims),
    )
    result = ingest_report_file(report_path, workspace=workspace.workspace_id)
    assert result.claims_created == 3
    assert result.evidence_count == 3
    assert result.relations == 3

    conn = open_db(workspace.db_path)
    try:
        for index in range(len(DAILY_IDS)):
            claim_id = f"OBS-W-{WEEK_START}-0{index + 1}"
            outcome = promote(
                conn, claim_id, author="04-weekly-review", reason="pilot"
            )
            assert outcome.to_status == "active"

        active = query_items(conn, entity_type="claim", status="active")
        active_ids = {row.entity_id for row in active}
        for claim_id in DAILY_IDS:
            assert claim_id in active_ids
        for index in range(len(DAILY_IDS)):
            assert f"OBS-W-{WEEK_START}-0{index + 1}" in active_ids

        relations = conn.execute(
            "SELECT from_claim_id, to_claim_id FROM claim_relations "
            "WHERE relation_type = 'derived_from' "
            "ORDER BY from_claim_id"
        ).fetchall()
        assert len(relations) == 3
        assert {row["to_claim_id"] for row in relations} == set(DAILY_IDS)

        fsck = run_fsck(conn)
        assert fsck.findings == ()
        assert fsck.has_errors is False
    finally:
        conn.close()
