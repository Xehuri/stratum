"""Report ingest: contracts, evidence, hierarchy, cycles and rollback."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from stratum.cli import main
from stratum.db import open_db
from stratum.segmentation.identity import compute_block_id
from stratum.workspace import init_workspace


def _init(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "home"
    root = tmp_path / "notes"
    root.mkdir(parents=True)
    monkeypatch.setenv("STRATUM_HOME", str(home))
    monkeypatch.chdir(tmp_path)
    init_workspace(root, name="demo")
    return root


def _db(root: Path) -> sqlite3.Connection:
    conn = open_db(root / ".stratum" / "stratum.db")
    conn.row_factory = sqlite3.Row
    return conn


def _write(root: Path, relative: str, text: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def _count(conn: sqlite3.Connection, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _seed_card(conn: sqlite3.Connection, card_id: str = "C-TEST-0001") -> str:
    conn.execute(
        """
        INSERT INTO cards (id, card_type, title, body, status)
        VALUES (?, 'event', 'Synthetic card', 'Synthetic body.', 'draft')
        """,
        (card_id,),
    )
    return card_id


def _seed_claim(
    conn: sqlite3.Connection,
    claim_id: str,
    *,
    claim_type: str = "observation",
    period_start: str = "2026-01-05",
    period_end: str = "2026-01-06",
    granularity: str = "day",
    status: str = "draft",
) -> str:
    conn.execute(
        """
        INSERT INTO claims (
            id, claim_type, period_start, period_end, granularity,
            entities_json, body, confidence, status, content_sha256
        ) VALUES (?, ?, ?, ?, ?, '["self"]', 'seed body', 'low', ?, 'seed')
        """,
        (claim_id, claim_type, period_start, period_end, granularity, status),
    )
    return claim_id


def _frontmatter(
    report_type: str,
    report_id: str,
    period_start: str,
    period_end: str,
    granularity: str,
    author: str,
) -> dict[str, str]:
    return {
        "report_contract": "v1",
        "report_type": report_type,
        "report_id": report_id,
        "period_start": period_start,
        "period_end": period_end,
        "granularity": granularity,
        "author": author,
        "base_commit": "none",
        "created_at": "2026-01-06T00:30:00+08:00",
        "timezone": "Asia/Shanghai",
    }


def _document(frontmatter: dict[str, str], claims: list[str], *, ops: str | None = None) -> str:
    fm = "\n".join(f"{key}: {value}" for key, value in frontmatter.items())
    body = "\n\n".join(claims)
    ops_text = ops if ops is not None else "- 命令: stratum status"
    return (
        "---\n"
        + fm
        + "\n---\n\n## 叙事\n\nSynthetic narrative.\n\n"
        + "## 检索条目\n\n"
        + body
        + "\n\n## 运维\n\n"
        + ops_text
        + "\n"
    )


def _observation(
    claim_id: str,
    evidence: list[str],
    *,
    body: str = "Synthetic observation.",
    claim_type: str = "observation",
    granularity: str = "day",
    period_start: str = "2026-01-05",
    period_end: str = "2026-01-06",
    derived_from: list[str] | None = None,
    extra: list[tuple[str, str]] | None = None,
) -> str:
    lines = [
        f"### {claim_id} Synthetic claim",
        f"- claim_id: {claim_id}",
        f"- claim_type: {claim_type}",
        f"- period_start: {period_start}",
        f"- period_end: {period_end}",
        f"- granularity: {granularity}",
        "- entities: self",
    ]
    for target in derived_from or []:
        lines.append(f"- derived_from: {target}")
    for ref in evidence:
        lines.append(f"- evidence: {ref}")
    lines.append("- confidence: medium")
    if claim_type == "change":
        lines.append("- baseline: baseline")
        lines.append("- delta: delta")
    for key, value in extra or []:
        lines.append(f"- {key}: {value}")
    lines.append(f"- body: {body}")
    return "\n".join(lines)


def _pattern(
    claim_id: str,
    derived_from: list[str],
    evidence: list[str],
    *,
    supersedes: list[str] | None = None,
    confidence: str = "low",
) -> str:
    lines = [
        f"### {claim_id} Synthetic pattern",
        f"- claim_id: {claim_id}",
        "- claim_type: pattern",
        "- period_start: 2026-01-05",
        "- period_end: 2026-01-19",
        "- granularity: range",
        "- entities: self",
    ]
    for target in derived_from:
        lines.append(f"- derived_from: {target}")
    for target in supersedes or []:
        lines.append(f"- supersedes: {target}")
    for ref in evidence:
        lines.append(f"- evidence: {ref}")
    lines.extend(
        [
            "- conditions: synthetic condition",
            "- outcome: synthetic outcome",
            "- support_count: 1",
            "- opportunity_count: 1",
            "- counterexample_count: 0",
            "- counterexample_note: checked all opportunities",
            f"- confidence: {confidence}",
            f"- body: Synthetic pattern {claim_id}.",
        ]
    )
    return "\n".join(lines)


DAILY_FM = _frontmatter(
    "daily", "RPT-D-2026-01-05", "2026-01-05", "2026-01-06", "day", "02-daily-review"
)
WEEKLY_FM = _frontmatter(
    "weekly", "RPT-W-2026-01-05", "2026-01-05", "2026-01-12", "week", "04-weekly-review"
)
BIWEEKLY_FM = _frontmatter(
    "biweekly", "RPT-B-2026-01-05", "2026-01-05", "2026-01-19", "range", "03-biweekly-review"
)


def _ingest(root: Path, relative: str, capsys) -> int:
    code = main(["ingest", "report", str(root / relative)])
    capsys.readouterr()
    return code


def _report_counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        table: _count(conn, table)
        for table in (
            "reports",
            "claims",
            "claim_relations",
            "evidence_links",
            "documents",
            "document_versions",
            "blocks",
            "segmentation_runs",
        )
    }


def test_report_ingest_persists_report_claims_evidence_and_document(
    tmp_path, monkeypatch, capsys
):
    root = _init(tmp_path, monkeypatch)
    conn = _db(root)
    try:
        card_id = _seed_card(conn)
    finally:
        conn.close()

    text = _document(
        DAILY_FM, [_observation("OBS-D-2026-01-05-01", [card_id])]
    )
    _write(root, "reports/daily/2026-01-05.md", text)

    assert main(["ingest", "report", str(root / "reports/daily/2026-01-05.md")]) == 0
    out = capsys.readouterr().out
    assert "Ingested report: RPT-D-2026-01-05" in out
    assert "Claims: created 1, updated 0, rejected 0" in out
    assert "Evidence: 1" in out
    assert not out.lstrip().startswith("{")

    conn = _db(root)
    try:
        report = conn.execute("SELECT * FROM reports").fetchone()
        assert report["id"] == "RPT-D-2026-01-05"
        assert report["status"] == "draft"
        assert report["contract_version"] == 1
        assert report["document_id"] == "reports/daily/2026-01-05.md"

        claim = conn.execute("SELECT * FROM claims").fetchone()
        assert claim["id"] == "OBS-D-2026-01-05-01"
        assert claim["report_id"] == "RPT-D-2026-01-05"
        assert claim["status"] == "draft"
        assert claim["entities_json"] == '["self"]'

        evidence = conn.execute("SELECT * FROM evidence_links").fetchone()
        assert evidence["owner_type"] == "claim"
        assert evidence["owner_id"] == "OBS-D-2026-01-05-01"
        assert evidence["target_type"] == "card"
        assert evidence["target_id"] == card_id

        document = conn.execute(
            "SELECT * FROM documents WHERE path = 'reports/daily/2026-01-05.md'"
        ).fetchone()
        assert document["contract_version"] == 0
        assert document["retrieval_type"] == "structured"
        assert document["source_sha256"]

        assert _count(conn, "document_versions") == 1
        assert _count(conn, "blocks") > 1
        assert _count(conn, "segmentation_runs") == 1
    finally:
        conn.close()


def test_report_ingest_is_idempotent(tmp_path, monkeypatch, capsys):
    root = _init(tmp_path, monkeypatch)
    conn = _db(root)
    try:
        card_id = _seed_card(conn)
    finally:
        conn.close()

    _write(
        root,
        "reports/daily/2026-01-05.md",
        _document(DAILY_FM, [_observation("OBS-D-2026-01-05-01", [card_id])]),
    )
    report_path = root / "reports/daily/2026-01-05.md"

    assert main(["ingest", "report", str(report_path)]) == 0
    capsys.readouterr()
    conn = _db(root)
    try:
        before = _report_counts(conn)
    finally:
        conn.close()

    assert main(["ingest", "report", str(report_path)]) == 0
    assert capsys.readouterr().out.strip() == "unchanged"

    conn = _db(root)
    try:
        assert _report_counts(conn) == before
    finally:
        conn.close()


def test_report_revision_updates_draft_and_rejects_removed_claim(
    tmp_path, monkeypatch, capsys
):
    root = _init(tmp_path, monkeypatch)
    conn = _db(root)
    try:
        card_id = _seed_card(conn)
    finally:
        conn.close()

    first = _document(
        DAILY_FM,
        [
            _observation("OBS-D-2026-01-05-01", [card_id], body="First body."),
            _observation("OBS-D-2026-01-05-02", [card_id], body="Second body."),
        ],
    )
    path = _write(root, "reports/daily/2026-01-05.md", first)
    assert main(["ingest", "report", str(path)]) == 0
    capsys.readouterr()

    revised = _document(
        DAILY_FM,
        [_observation("OBS-D-2026-01-05-01", [card_id], body="Revised body.")],
    )
    path.write_text(revised, encoding="utf-8")
    assert main(["ingest", "report", str(path)]) == 0
    out = capsys.readouterr().out
    assert "Status: revised" in out
    assert "rejected 1" in out

    conn = _db(root)
    try:
        rows = {
            row["id"]: row
            for row in conn.execute("SELECT * FROM claims ORDER BY id")
        }
        assert rows["OBS-D-2026-01-05-01"]["body"] == "Revised body."
        assert rows["OBS-D-2026-01-05-01"]["status"] == "draft"
        assert rows["OBS-D-2026-01-05-02"]["status"] == "rejected"
        assert _count(conn, "document_versions") == 2
        assert _count(conn, "evidence_links") == 2
    finally:
        conn.close()


def test_report_is_immutable_when_any_claim_is_active(
    tmp_path, monkeypatch, capsys
):
    root = _init(tmp_path, monkeypatch)
    conn = _db(root)
    try:
        card_id = _seed_card(conn)
    finally:
        conn.close()

    path = _write(
        root,
        "reports/daily/2026-01-05.md",
        _document(DAILY_FM, [_observation("OBS-D-2026-01-05-01", [card_id])]),
    )
    assert main(["ingest", "report", str(path)]) == 0
    capsys.readouterr()

    conn = _db(root)
    try:
        conn.execute("UPDATE claims SET status = 'active'")
        conn.commit()
        versions_before = _count(conn, "document_versions")
    finally:
        conn.close()

    path.write_text(
        _document(
            DAILY_FM,
            [_observation("OBS-D-2026-01-05-01", [card_id], body="Changed.")],
        ),
        encoding="utf-8",
    )
    assert main(["ingest", "report", str(path)]) == 1
    err = capsys.readouterr().err
    assert "REPORT_IMMUTABLE" in err

    conn = _db(root)
    try:
        claim = conn.execute("SELECT * FROM claims").fetchone()
        assert claim["status"] == "active"
        assert "Changed" not in claim["body"]
        assert _count(conn, "document_versions") == versions_before
    finally:
        conn.close()


def test_report_missing_evidence_writes_nothing(tmp_path, monkeypatch, capsys):
    root = _init(tmp_path, monkeypatch)
    path = _write(
        root,
        "reports/daily/2026-01-05.md",
        _document(
            DAILY_FM,
            [
                "\n".join(
                    [
                        "### OBS-D-2026-01-05-01 No evidence",
                        "- claim_id: OBS-D-2026-01-05-01",
                        "- claim_type: observation",
                        "- period_start: 2026-01-05",
                        "- period_end: 2026-01-06",
                        "- granularity: day",
                        "- entities: self",
                        "- confidence: low",
                        "- body: no evidence at all",
                    ]
                )
            ],
        ),
    )

    assert main(["ingest", "report", str(path)]) == 1
    err = capsys.readouterr().err
    assert "EVIDENCE_MISSING" in err

    conn = _db(root)
    try:
        assert _count(conn, "reports") == 0
        assert _count(conn, "claims") == 0
        assert _count(conn, "documents") == 0
    finally:
        conn.close()


def test_report_unresolved_evidence_rolls_back_document(tmp_path, monkeypatch, capsys):
    root = _init(tmp_path, monkeypatch)
    path = _write(
        root,
        "reports/daily/2026-01-05.md",
        _document(DAILY_FM, [_observation("OBS-D-2026-01-05-01", ["C-MISSING"])]),
    )

    assert main(["ingest", "report", str(path)]) == 1
    err = capsys.readouterr().err
    assert "EVIDENCE_UNRESOLVED" in err

    conn = _db(root)
    try:
        for table in ("reports", "claims", "claim_relations", "evidence_links",
                      "documents", "document_versions", "blocks", "segmentation_runs"):
            assert _count(conn, table) == 0, table
    finally:
        conn.close()


def test_report_rejects_prose_block_evidence(tmp_path, monkeypatch, capsys):
    root = _init(tmp_path, monkeypatch)
    prose = root / "prose.md"
    prose.write_text("# Prose\n\nA long prose paragraph.\n", encoding="utf-8")
    assert main(["ingest", "source", str(prose), "--profile", "whole-document"]) == 0
    capsys.readouterr()

    conn = _db(root)
    try:
        block_id = conn.execute(
            "SELECT id FROM blocks WHERE retrieval_type = 'prose'"
        ).fetchone()[0]
    finally:
        conn.close()

    path = _write(
        root,
        "reports/daily/2026-01-05.md",
        _document(DAILY_FM, [_observation("OBS-D-2026-01-05-01", [block_id])]),
    )
    assert main(["ingest", "report", str(path)]) == 1
    err = capsys.readouterr().err
    assert "EVIDENCE_PROSE" in err

    conn = _db(root)
    try:
        assert _count(conn, "reports") == 0
        assert _count(conn, "claims") == 0
    finally:
        conn.close()


def test_report_rejects_narrative_and_ops_blocks_as_evidence(
    tmp_path, monkeypatch, capsys
):
    root = _init(tmp_path, monkeypatch)
    relative = "reports/daily/2026-01-05.md"
    narrative_block = compute_block_id(
        relative, "generic-heading", "heading", "叙事", 0
    )
    path = _write(
        root,
        relative,
        _document(DAILY_FM, [_observation("OBS-D-2026-01-05-01", [narrative_block])]),
    )

    assert main(["ingest", "report", str(path)]) == 1
    err = capsys.readouterr().err
    assert "NARRATIVE_AS_CLAIM" in err

    ops_block = compute_block_id(relative, "generic-heading", "heading", "运维", 0)
    path.write_text(
        _document(DAILY_FM, [_observation("OBS-D-2026-01-05-01", [ops_block])]),
        encoding="utf-8",
    )
    assert main(["ingest", "report", str(path)]) == 1
    err = capsys.readouterr().err
    assert "OPS_AS_CLAIM" in err

    conn = _db(root)
    try:
        assert _count(conn, "reports") == 0
        assert _count(conn, "claims") == 0
    finally:
        conn.close()


def test_report_snapshot_evidence_forms(tmp_path, monkeypatch, capsys):
    root = _init(tmp_path, monkeypatch)
    source = root / "source.md"
    source.write_text("# Source\n\nStable body.\n", encoding="utf-8")
    assert main(["ingest", "source", str(source)]) == 0
    capsys.readouterr()

    conn = _db(root)
    try:
        block = conn.execute(
            "SELECT id, snapshot_sha256 FROM blocks WHERE retrieval_type = 'structured'"
        ).fetchone()
        _seed_card(conn)
    finally:
        conn.close()

    wrong_hash = "0" * 64
    path = _write(
        root,
        "reports/daily/2026-01-05.md",
        _document(
            DAILY_FM,
            [
                _observation(
                    "OBS-D-2026-01-05-01",
                    [f"{block['id']}#sha256:{wrong_hash}"],
                )
            ],
        ),
    )
    assert main(["ingest", "report", str(path)]) == 1
    assert "EVIDENCE_SNAPSHOT_MISMATCH" in capsys.readouterr().err

    matching = f"{block['id']}#sha256:{block['snapshot_sha256']}"
    path.write_text(
        _document(DAILY_FM, [_observation("OBS-D-2026-01-05-01", [matching])]),
        encoding="utf-8",
    )
    assert main(["ingest", "report", str(path)]) == 0

    snapshot_ref = f"snapshot:sha256:{block['snapshot_sha256']}"
    path.write_text(
        _document(DAILY_FM, [_observation("OBS-D-2026-01-05-02", [snapshot_ref])]),
        encoding="utf-8",
    )
    assert main(["ingest", "report", str(path)]) == 0


def test_report_weekly_derives_from_daily_claims(tmp_path, monkeypatch, capsys):
    root = _init(tmp_path, monkeypatch)
    conn = _db(root)
    try:
        card_id = _seed_card(conn)
        _seed_claim(conn, "OBS-D-2026-01-05-01")
        _seed_claim(
            conn,
            "CHG-D-2026-01-05-01",
            claim_type="change",
        )
        conn.commit()
    finally:
        conn.close()

    weekly_claim = _observation(
        "OBS-W-2026-01-05-01",
        [card_id],
        granularity="week",
        period_end="2026-01-12",
        derived_from=["OBS-D-2026-01-05-01", "CHG-D-2026-01-05-01"],
    )
    path = _write(
        root,
        "reports/weekly/2026-01-05.md",
        _document(WEEKLY_FM, [weekly_claim]),
    )

    assert main(["ingest", "report", str(path)]) == 0
    out = capsys.readouterr().out
    assert "Relations: 2" in out

    conn = _db(root)
    try:
        relations = conn.execute(
            "SELECT * FROM claim_relations ORDER BY to_claim_id"
        ).fetchall()
        assert [row["relation_type"] for row in relations] == [
            "derived_from",
            "derived_from",
        ]
    finally:
        conn.close()


def test_report_weekly_rejects_out_of_period_derived_from(
    tmp_path, monkeypatch, capsys
):
    root = _init(tmp_path, monkeypatch)
    conn = _db(root)
    try:
        card_id = _seed_card(conn)
        _seed_claim(
            conn,
            "OBS-D-2026-01-13-01",
            period_start="2026-01-13",
            period_end="2026-01-14",
        )
        conn.commit()
    finally:
        conn.close()

    weekly_claim = _observation(
        "OBS-W-2026-01-05-01",
        [card_id],
        granularity="week",
        period_end="2026-01-12",
        derived_from=["OBS-D-2026-01-13-01"],
    )
    path = _write(
        root,
        "reports/weekly/2026-01-05.md",
        _document(WEEKLY_FM, [weekly_claim]),
    )

    assert main(["ingest", "report", str(path)]) == 1
    err = capsys.readouterr().err
    assert "DERIVED_FROM_LAYER" in err

    conn = _db(root)
    try:
        assert _count(conn, "reports") == 0
        assert _count(conn, "claims") == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM documents WHERE path = "
            "'reports/weekly/2026-01-05.md'"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_report_biweekly_derives_from_weekly_observation(
    tmp_path, monkeypatch, capsys
):
    root = _init(tmp_path, monkeypatch)
    conn = _db(root)
    try:
        card_id = _seed_card(conn)
        _seed_claim(
            conn,
            "OBS-W-2026-01-05-01",
            period_start="2026-01-05",
            period_end="2026-01-12",
            granularity="week",
        )
        conn.commit()
    finally:
        conn.close()

    pattern = _pattern("PAT-B-2026-01-05-01", ["OBS-W-2026-01-05-01"], [card_id])
    path = _write(
        root,
        "reports/biweekly/2026-01-05.md",
        _document(BIWEEKLY_FM, [pattern]),
    )

    assert main(["ingest", "report", str(path)]) == 0
    conn = _db(root)
    try:
        relation = conn.execute("SELECT * FROM claim_relations").fetchone()
        assert relation["relation_type"] == "derived_from"
        assert relation["to_claim_id"] == "OBS-W-2026-01-05-01"
    finally:
        conn.close()


def test_report_biweekly_rejects_derived_from_daily(
    tmp_path, monkeypatch, capsys
):
    root = _init(tmp_path, monkeypatch)
    conn = _db(root)
    try:
        card_id = _seed_card(conn)
        _seed_claim(conn, "OBS-D-2026-01-05-01")
        conn.commit()
    finally:
        conn.close()

    pattern = _pattern("PAT-B-2026-01-05-01", ["OBS-D-2026-01-05-01"], [card_id])
    path = _write(
        root,
        "reports/biweekly/2026-01-05.md",
        _document(BIWEEKLY_FM, [pattern]),
    )

    assert main(["ingest", "report", str(path)]) == 1
    assert "DERIVED_FROM_LAYER" in capsys.readouterr().err


def test_report_cycle_detected(tmp_path, monkeypatch, capsys):
    root = _init(tmp_path, monkeypatch)
    conn = _db(root)
    try:
        card_id = _seed_card(conn)
        _seed_claim(
            conn,
            "OBS-W-2026-01-05-01",
            period_start="2026-01-05",
            period_end="2026-01-12",
            granularity="week",
        )
        conn.commit()
    finally:
        conn.close()

    first = _pattern(
        "PAT-B-2026-01-05-01",
        ["OBS-W-2026-01-05-01"],
        [card_id],
        supersedes=["PAT-B-2026-01-05-02"],
    )
    second = _pattern(
        "PAT-B-2026-01-05-02",
        ["OBS-W-2026-01-05-01"],
        [card_id],
        supersedes=["PAT-B-2026-01-05-01"],
    )
    path = _write(
        root,
        "reports/biweekly/2026-01-05.md",
        _document(BIWEEKLY_FM, [first, second]),
    )

    assert main(["ingest", "report", str(path)]) == 1
    assert "CYCLE_DETECTED" in capsys.readouterr().err

    conn = _db(root)
    try:
        assert _count(conn, "reports") == 0
        assert _count(conn, "claims") == 1
        assert _count(conn, "claim_relations") == 0
    finally:
        conn.close()


def test_report_self_reference_detected(tmp_path, monkeypatch, capsys):
    root = _init(tmp_path, monkeypatch)
    conn = _db(root)
    try:
        card_id = _seed_card(conn)
        conn.commit()
    finally:
        conn.close()

    pattern = _pattern(
        "PAT-B-2026-01-05-01",
        ["PAT-B-2026-01-05-01"],
        [card_id],
    )
    path = _write(
        root,
        "reports/biweekly/2026-01-05.md",
        _document(BIWEEKLY_FM, [pattern]),
    )

    assert main(["ingest", "report", str(path)]) == 1
    assert "CYCLE_DETECTED" in capsys.readouterr().err


def test_report_supersedes_must_stay_on_layer(tmp_path, monkeypatch, capsys):
    root = _init(tmp_path, monkeypatch)
    conn = _db(root)
    try:
        card_id = _seed_card(conn)
        _seed_claim(
            conn,
            "OBS-W-2026-01-05-01",
            period_start="2026-01-05",
            period_end="2026-01-12",
            granularity="week",
        )
        conn.commit()
    finally:
        conn.close()

    daily = _observation(
        "OBS-D-2026-01-05-01",
        [card_id],
        extra=[("supersedes", "OBS-W-2026-01-05-01")],
    )
    path = _write(
        root,
        "reports/daily/2026-01-05.md",
        _document(DAILY_FM, [daily]),
    )

    assert main(["ingest", "report", str(path)]) == 1
    assert "SUPERSEDES_INVALID" in capsys.readouterr().err


def test_duplicate_claim_id_across_reports_conflicts(
    tmp_path, monkeypatch, capsys
):
    root = _init(tmp_path, monkeypatch)
    conn = _db(root)
    try:
        card_id = _seed_card(conn)
    finally:
        conn.close()

    first_path = _write(
        root,
        "reports/daily/2026-01-05.md",
        _document(DAILY_FM, [_observation("OBS-D-2026-01-05-01", [card_id])]),
    )
    assert main(["ingest", "report", str(first_path)]) == 0
    capsys.readouterr()

    revision_fm = dict(DAILY_FM)
    revision_fm["report_id"] = "RPT-D-2026-01-05-r2"
    second_path = _write(
        root,
        "reports/daily/2026-01-05-r2.md",
        _document(revision_fm, [_observation("OBS-D-2026-01-05-01", [card_id])]),
    )
    assert main(["ingest", "report", str(second_path)]) == 1
    assert "CLAIM_ID_CONFLICT" in capsys.readouterr().err

    conn = _db(root)
    try:
        assert _count(conn, "reports") == 1
        assert _count(conn, "claims") == 1
    finally:
        conn.close()


def test_report_parse_error_is_plain_text_and_exit_one(
    tmp_path, monkeypatch, capsys
):
    root = _init(tmp_path, monkeypatch)
    path = _write(
        root,
        "reports/daily/2026-01-05.md",
        _document(DAILY_FM, [_observation("OBS-D-2026-01-05-01", ["C-TEST-0001"])])
        .replace("report_contract: v1", "report_contract: v9"),
    )

    assert main(["ingest", "report", str(path)]) == 1
    result = capsys.readouterr()
    assert result.out == ""
    assert result.err.startswith("stratum: error:")
    assert "CONTRACT_VERSION_UNKNOWN" in result.err
    assert not result.err.lstrip().startswith("{")
