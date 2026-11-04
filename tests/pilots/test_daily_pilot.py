"""Daily pilot: source ingest, card, daily report, promotion and query."""

from __future__ import annotations

from pathlib import Path

from stratum.add import add_card
from stratum.db import open_db
from stratum.fsck import run_fsck
from stratum.ingest_report import ingest_report_file
from stratum.ingest_source import ingest_source_file
from stratum.query import query_items
from stratum.review import promote
from stratum.workspace import init_workspace

DAY = "2026-03-02"
NEXT_DAY = "2026-03-03"


def _init(tmp_path: Path, monkeypatch):
    home = tmp_path / "home"
    root = tmp_path / "notes"
    root.mkdir(parents=True)
    monkeypatch.setenv("STRATUM_HOME", str(home))
    monkeypatch.chdir(tmp_path)
    result = init_workspace(root, name="daily-pilot")
    return root, result.workspace


def _write(root: Path, relative: str, text: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def _report(*, claims: str) -> str:
    return f"""---
report_contract: v1
report_type: daily
report_id: RPT-D-{DAY}
period_start: {DAY}
period_end: {NEXT_DAY}
granularity: day
author: 02-daily-review
base_commit: none
created_at: 2026-03-03T00:30:00+08:00
timezone: Asia/Shanghai
---

## 叙事

Synthetic daily narrative for the pilot.

## 检索条目

{claims}

## 运维

- 命令: stratum status
"""


def test_daily_pilot_end_to_end(tmp_path, monkeypatch):
    root, workspace = _init(tmp_path, monkeypatch)
    assert workspace.workspace_id

    source_one = _write(
        root,
        "sources/morning.md",
        "# Morning batch\n\n## OBS-2026-03-02-01 Morning sample\n\n"
        "The synthetic morning sample was recorded.\n",
    )
    source_two = _write(
        root,
        "sources/evening.md",
        "# Evening batch\n\n## OBS-2026-03-02-02 Evening sample\n\n"
        "The synthetic evening sample was recorded.\n",
    )
    ingest_source_file(source_one, workspace=workspace.workspace_id)
    ingest_source_file(source_two, workspace=workspace.workspace_id)

    conn = open_db(workspace.db_path)
    try:
        blocks = conn.execute(
            "SELECT id FROM blocks ORDER BY document_id, line_start"
        ).fetchall()
        block_ids = [str(row["id"]) for row in blocks]
    finally:
        conn.close()
    assert len(block_ids) == 4

    card = add_card(
        blocks=block_ids[:2],
        card_type="observation",
        body="Synthetic daily batch card with two source blocks.",
        title="Synthetic daily batch",
        author="02-daily-review",
        date=DAY,
        workspace=workspace.workspace_id,
    )
    assert card.status == "created"

    report_path = _write(
        root,
        f"reports/daily/{DAY}.md",
        _report(
            claims=(
                f"### OBS-D-{DAY}-01 Synthetic morning observation\n"
                f"- claim_id: OBS-D-{DAY}-01\n"
                "- claim_type: observation\n"
                f"- period_start: {DAY}\n"
                f"- period_end: {NEXT_DAY}\n"
                "- granularity: day\n"
                "- entities: synthetic-reader\n"
                f"- evidence: {card.card_id}\n"
                "- confidence: medium\n"
                "- body: The synthetic morning batch was indexed.\n\n"
                f"### CHG-D-{DAY}-01 Synthetic evening change\n"
                f"- claim_id: CHG-D-{DAY}-01\n"
                "- claim_type: change\n"
                f"- period_start: {DAY}\n"
                f"- period_end: {NEXT_DAY}\n"
                "- granularity: day\n"
                "- entities: synthetic-reader\n"
                f"- evidence: {block_ids[2]}\n"
                "- confidence: low\n"
                "- baseline: one synthetic batch\n"
                "- delta: two synthetic batches\n"
                "- body: A second synthetic batch was added.\n"
            )
        ),
    )
    result = ingest_report_file(report_path, workspace=workspace.workspace_id)
    assert result.status == "created"
    assert result.claims_created == 2
    assert result.evidence_count == 2

    conn = open_db(workspace.db_path)
    try:
        promote(conn, card.card_id, author="02-daily-review", reason="pilot")
        for claim_id in (f"OBS-D-{DAY}-01", f"CHG-D-{DAY}-01"):
            outcome = promote(
                conn, claim_id, author="02-daily-review", reason="pilot"
            )
            assert outcome.to_status == "active"

        active = query_items(conn, entity_type="claim", status="active")
        assert [row.entity_id for row in active] == [
            f"CHG-D-{DAY}-01",
            f"OBS-D-{DAY}-01",
        ]
        assert all(row.status == "active" for row in active)

        fsck = run_fsck(conn)
        assert fsck.findings == ()
        assert fsck.has_errors is False

        report = conn.execute(
            "SELECT id, report_type, period_start, period_end FROM reports"
        ).fetchone()
        assert report["id"] == f"RPT-D-{DAY}"
        assert report["report_type"] == "daily"
        assert report["period_start"] == DAY
        assert report["period_end"] == NEXT_DAY

        links = conn.execute(
            "SELECT owner_id, target_type, target_id FROM evidence_links "
            "ORDER BY owner_id, target_type, target_id"
        ).fetchall()
        assert len(links) == 2
        assert {row["target_type"] for row in links} == {"card", "block"}
    finally:
        conn.close()
