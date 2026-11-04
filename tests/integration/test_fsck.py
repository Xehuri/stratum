"""fsck: clean state, seeded corruption, safe fixes and exit codes."""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

from stratum.cli import main
from stratum.db import open_db
from stratum.workspace import init_workspace

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
SOURCE = FIXTURES / "history" / "source.md"

CLAIM_ID = "OBS-D-2026-01-05-01"


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


def _setup(root: Path, capsys) -> tuple[str, str, str]:
    source = root / "source.md"
    shutil.copyfile(SOURCE, source)
    assert main(["ingest", "source", str(source)]) == 0
    capsys.readouterr()

    conn = _db(root)
    try:
        block_id = str(
            conn.execute(
                "SELECT id FROM blocks WHERE retrieval_type = 'structured' "
                "ORDER BY line_start LIMIT 1"
            ).fetchone()[0]
        )
    finally:
        conn.close()

    assert main(
        [
            "add", "card", "--block", block_id, "--type", "event",
            "--body", "Fsck card body.", "--title", "Fsck card",
            "--author", "02-daily-review", "--date", "2026-01-05",
        ]
    ) == 0
    capsys.readouterr()

    conn = _db(root)
    try:
        card_id = str(conn.execute("SELECT id FROM cards").fetchone()[0])
    finally:
        conn.close()

    assert main(
        [
            "add", "claim", "--claim-id", CLAIM_ID,
            "--claim-type", "observation",
            "--period-start", "2026-01-05", "--period-end", "2026-01-06",
            "--granularity", "day", "--entity", "self",
            "--evidence", card_id, "--confidence", "medium",
            "--body", "Fsck claim body.", "--author", "02-daily-review",
        ]
    ) == 0
    capsys.readouterr()
    assert main(
        ["review", "promote", card_id, "--author", "human", "--reason", "qa"]
    ) == 0
    assert main(
        ["review", "promote", CLAIM_ID, "--author", "human", "--reason", "qa"]
    ) == 0
    capsys.readouterr()
    return CLAIM_ID, card_id, block_id


def _seed_card(
    conn: sqlite3.Connection,
    card_id: str = "C-SEED-0001",
    *,
    status: str = "draft",
) -> str:
    conn.execute(
        """
        INSERT INTO cards (id, card_type, title, body, status)
        VALUES (?, 'event', 'Seed card', 'Seed body.', ?)
        """,
        (card_id, status),
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
    data_gap: str | None = None,
    report_id: str | None = None,
) -> str:
    conn.execute(
        """
        INSERT INTO claims (
            id, report_id, claim_type, period_start, period_end, granularity,
            entities_json, body, confidence, status, content_sha256, data_gap
        ) VALUES (?, ?, ?, ?, ?, ?, '["self"]', 'Seed claim body.', 'medium',
                  ?, 'seed-sha', ?)
        """,
        (
            claim_id,
            report_id,
            claim_type,
            period_start,
            period_end,
            granularity,
            status,
            data_gap,
        ),
    )
    return claim_id


def _seed_evidence(
    conn: sqlite3.Connection,
    claim_id: str,
    target_type: str,
    target_id: str,
    *,
    link_id: str | None = None,
) -> str:
    link = link_id or f"EVL-{claim_id}-{target_type}-{target_id}"
    conn.execute(
        """
        INSERT INTO evidence_links (
            id, owner_type, owner_id, target_type, target_id, role
        ) VALUES (?, 'claim', ?, ?, ?, 'evidence')
        """,
        (link, claim_id, target_type, target_id),
    )
    return link


def _seed_relation(
    conn: sqlite3.Connection,
    from_claim_id: str,
    to_claim_id: str,
    *,
    relation_type: str = "derived_from",
) -> None:
    conn.execute(
        """
        INSERT INTO claim_relations (
            id, from_claim_id, to_claim_id, relation_type
        ) VALUES (?, ?, ?, ?)
        """,
        (
            f"CLR-{from_claim_id}-{to_claim_id}-{relation_type}",
            from_claim_id,
            to_claim_id,
            relation_type,
        ),
    )


def _run_count(root: Path) -> int:
    conn = _db(root)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM fsck_runs").fetchone()[0])
    finally:
        conn.close()


def test_fsck_clean_workspace(tmp_path, monkeypatch, capsys):
    root = _init(tmp_path, monkeypatch)
    _setup(root, capsys)

    assert main(["fsck"]) == 0
    out = capsys.readouterr().out
    assert "Status: clean" in out
    assert "Findings: 0 (error 0, warning 0, info 0)" in out
    assert "{" not in out

    conn = _db(root)
    try:
        run = conn.execute("SELECT * FROM fsck_runs").fetchone()
        assert run["status"] == "clean"
        assert run["finding_count"] == 0
        assert conn.execute("SELECT COUNT(*) FROM fsck_findings").fetchone()[0] == 0
    finally:
        conn.close()


def test_fsck_detects_claim_without_evidence(tmp_path, monkeypatch, capsys):
    root = _init(tmp_path, monkeypatch)
    conn = _db(root)
    try:
        _seed_claim(conn, CLAIM_ID)
    finally:
        conn.close()

    assert main(["fsck"]) == 1
    out = capsys.readouterr().out
    assert "Status: failed" in out
    assert "CLAIM_EVIDENCE_MISSING" in out
    assert CLAIM_ID in out


def test_fsck_detects_active_claim_data_gap(tmp_path, monkeypatch, capsys):
    root = _init(tmp_path, monkeypatch)
    _setup(root, capsys)
    conn = _db(root)
    try:
        conn.execute(
            "UPDATE claims SET data_gap = 'missing data' WHERE id = ?",
            (CLAIM_ID,),
        )
    finally:
        conn.close()

    assert main(["fsck"]) == 1
    out = capsys.readouterr().out
    assert "CLAIM_DATA_GAP_ACTIVE" in out


def test_fsck_detects_inactive_card_evidence(tmp_path, monkeypatch, capsys):
    root = _init(tmp_path, monkeypatch)
    claim_id, card_id, _ = _setup(root, capsys)
    conn = _db(root)
    try:
        conn.execute(
            "UPDATE cards SET status = 'draft' WHERE id = ?", (card_id,)
        )
    finally:
        conn.close()

    assert main(["fsck"]) == 1
    out = capsys.readouterr().out
    assert "CLAIM_CARD_NOT_ACTIVE" in out
    assert card_id in out


def test_fsck_detects_block_snapshot_and_status_problems(
    tmp_path, monkeypatch, capsys
):
    root = _init(tmp_path, monkeypatch)
    _setup(root, capsys)
    conn = _db(root)
    try:
        block_id = str(
            conn.execute(
                "SELECT block_id FROM card_blocks LIMIT 1"
            ).fetchone()[0]
        )
        _seed_evidence(conn, CLAIM_ID, "block", block_id)
        conn.execute(
            "UPDATE blocks SET status = 'missing', snapshot_sha256 = ? WHERE id = ?",
            ("b" * 64, block_id),
        )
    finally:
        conn.close()

    assert main(["fsck"]) == 1
    out = capsys.readouterr().out
    assert "CLAIM_BLOCK_MISSING" in out
    assert "CARD_BLOCK_MISSING" in out


def test_fsck_detects_prose_block_evidence(tmp_path, monkeypatch, capsys):
    root = _init(tmp_path, monkeypatch)
    _setup(root, capsys)
    conn = _db(root)
    try:
        conn.execute(
            """
            INSERT INTO documents (id, path, retrieval_type)
            VALUES ('DOC-prose', 'prose.md', 'prose')
            """
        )
        conn.execute(
            """
            INSERT INTO blocks (
                id, document_id, retrieval_type, selector_type, anchor,
                occurrence, line_start, line_end, snapshot_text,
                snapshot_sha256, status
            ) VALUES ('BLK-prose-01', 'DOC-prose', 'prose', 'paragraph',
                      'anchor', 0, 1, 2, 'prose text', ?, 'active')
            """,
            ("c" * 64,),
        )
        _seed_evidence(conn, CLAIM_ID, "block", "BLK-prose-01")
    finally:
        conn.close()

    assert main(["fsck"]) == 1
    out = capsys.readouterr().out
    assert "CLAIM_BLOCK_PROSE" in out


def test_fsck_detects_derived_from_problems(tmp_path, monkeypatch, capsys):
    root = _init(tmp_path, monkeypatch)
    conn = _db(root)
    try:
        card_id = _seed_card(conn, "C-SEED-0001", status="active")
        _seed_claim(
            conn, "OBS-D-2026-01-05-01", status="active",
            period_start="2026-01-05", period_end="2026-01-06",
        )
        _seed_claim(
            conn, "OBS-D-2026-01-05-02", status="draft",
            period_start="2026-01-05", period_end="2026-01-06",
        )
        _seed_claim(
            conn, "OBS-D-2026-01-06-01", status="active",
            period_start="2026-01-06", period_end="2026-01-09",
        )
        _seed_claim(
            conn, "OBS-W-2026-01-05-01", claim_type="observation",
            period_start="2026-01-05", period_end="2026-01-12",
            granularity="week", status="active",
        )
        _seed_claim(
            conn, "OBS-W-2026-01-05-02", claim_type="observation",
            period_start="2026-01-05", period_end="2026-01-07",
            granularity="week", status="active",
        )
        _seed_claim(
            conn, "OBS-W-2026-01-05-03", claim_type="observation",
            period_start="2026-01-05", period_end="2026-01-12",
            granularity="week", status="active",
        )
        _seed_claim(
            conn, "OBS-W-2026-01-05-04", claim_type="observation",
            period_start="2026-01-05", period_end="2026-01-12",
            granularity="week", status="active",
        )
        for claim_id in (
            "OBS-D-2026-01-05-01",
            "OBS-D-2026-01-05-02",
            "OBS-D-2026-01-06-01",
            "OBS-W-2026-01-05-01",
            "OBS-W-2026-01-05-02",
            "OBS-W-2026-01-05-03",
            "OBS-W-2026-01-05-04",
        ):
            _seed_evidence(conn, claim_id, "card", card_id)

        _seed_relation(conn, "OBS-W-2026-01-05-01", "OBS-D-2026-01-05-02")
        _seed_relation(conn, "OBS-W-2026-01-05-01", "OBS-D-2026-01-05-01")
        _seed_relation(conn, "OBS-W-2026-01-05-02", "OBS-D-2026-01-06-01")
        _seed_relation(conn, "OBS-W-2026-01-05-03", "OBS-W-2026-01-05-01")
    finally:
        conn.close()

    assert main(["fsck"]) == 1
    out = capsys.readouterr().out
    assert "CLAIM_DERIVED_FROM_NOT_ACTIVE" in out
    assert "CLAIM_DERIVED_FROM_PERIOD" in out
    assert "CLAIM_DERIVED_FROM_LAYER" in out
    assert "CLAIM_DERIVED_FROM_MISSING" in out


def test_fsck_detects_relation_cycle(tmp_path, monkeypatch, capsys):
    root = _init(tmp_path, monkeypatch)
    conn = _db(root)
    try:
        card_id = _seed_card(conn, "C-SEED-0001")
        _seed_claim(conn, "OBS-D-2026-01-05-01")
        _seed_claim(conn, "OBS-D-2026-01-05-02")
        _seed_evidence(conn, "OBS-D-2026-01-05-01", "card", card_id)
        _seed_evidence(conn, "OBS-D-2026-01-05-02", "card", card_id)
        _seed_relation(conn, "OBS-D-2026-01-05-01", "OBS-D-2026-01-05-02")
        _seed_relation(conn, "OBS-D-2026-01-05-02", "OBS-D-2026-01-05-01")
    finally:
        conn.close()

    assert main(["fsck"]) == 1
    out = capsys.readouterr().out
    assert "RELATION_CYCLE" in out


def test_fsck_detects_report_without_document(tmp_path, monkeypatch, capsys):
    root = _init(tmp_path, monkeypatch)
    conn = _db(root)
    try:
        conn.execute(
            """
            INSERT INTO reports (id, report_type, title, document_id)
            VALUES ('RPT-D-2026-01-05', 'daily', 'Seed report', NULL)
            """
        )
    finally:
        conn.close()

    assert main(["fsck"]) == 1
    out = capsys.readouterr().out
    assert "REPORT_DOCUMENT_MISSING" in out
    assert "RPT-D-2026-01-05" in out


def test_fsck_detects_orphan_evidence_and_fixes_safely(
    tmp_path, monkeypatch, capsys
):
    root = _init(tmp_path, monkeypatch)
    _setup(root, capsys)
    conn = _db(root)
    try:
        _seed_evidence(
            conn, CLAIM_ID, "card", "C-MISSING", link_id="EVL-BAD"
        )
    finally:
        conn.close()

    assert main(["fsck"]) == 1
    out = capsys.readouterr().out
    assert "CLAIM_CARD_MISSING" in out
    assert "EVIDENCE_TARGET_MISSING" in out

    assert main(["fsck", "--fix"]) == 0
    fixed_out = capsys.readouterr().out
    assert "Status: clean" in fixed_out
    assert "Fixed: claim" in fixed_out
    assert "active -> stale" in fixed_out

    conn = _db(root)
    try:
        assert (
            conn.execute(
                "SELECT status FROM claims WHERE id = ?", (CLAIM_ID,)
            ).fetchone()[0]
            == "stale"
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM evidence_links WHERE id = 'EVL-BAD'"
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM claims WHERE id = ?", (CLAIM_ID,)
            ).fetchone()[0]
            == 1
        )
        assert conn.execute("SELECT COUNT(*) FROM fsck_runs").fetchone()[0] == 2
    finally:
        conn.close()

    assert main(["fsck"]) == 0
    final_out = capsys.readouterr().out
    assert "Status: clean" in final_out
    assert "EVIDENCE_TARGET_MISSING" in final_out


def test_fsck_fix_cascades_from_card_to_claim(tmp_path, monkeypatch, capsys):
    root = _init(tmp_path, monkeypatch)
    claim_id, card_id, _ = _setup(root, capsys)
    conn = _db(root)
    try:
        block_id = str(
            conn.execute(
                "SELECT block_id FROM card_blocks WHERE card_id = ?",
                (card_id,),
            ).fetchone()[0]
        )
        conn.execute(
            "UPDATE blocks SET status = 'missing' WHERE id = ?", (block_id,)
        )
    finally:
        conn.close()

    assert main(["fsck", "--fix"]) == 0
    out = capsys.readouterr().out
    assert "Fixed: card" in out
    assert "Fixed: claim" in out

    conn = _db(root)
    try:
        assert (
            conn.execute(
                "SELECT status FROM cards WHERE id = ?", (card_id,)
            ).fetchone()[0]
            == "stale"
        )
        assert (
            conn.execute(
                "SELECT status FROM claims WHERE id = ?", (claim_id,)
            ).fetchone()[0]
            == "stale"
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM blocks WHERE id = ?", (block_id,)
            ).fetchone()[0]
            == 1
        )
    finally:
        conn.close()


def test_fsck_fix_marks_card_stale_and_does_not_delete(tmp_path, monkeypatch, capsys):
    root = _init(tmp_path, monkeypatch)
    conn = _db(root)
    try:
        _seed_card(conn, "C-SEED-0001", status="active")
    finally:
        conn.close()

    assert main(["fsck", "--fix"]) == 0
    out = capsys.readouterr().out
    assert "CARD_EVIDENCE_MISSING" in out or "Fixed: card" in out
    assert "Fixed: card C-SEED-0001 active -> stale" in out

    conn = _db(root)
    try:
        assert (
            conn.execute(
                "SELECT status FROM cards WHERE id = 'C-SEED-0001'"
            ).fetchone()[0]
            == "stale"
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM cards WHERE id = 'C-SEED-0001'"
            ).fetchone()[0]
            == 1
        )
    finally:
        conn.close()


def test_fsck_records_review_event_for_missing_entity(
    tmp_path, monkeypatch, capsys
):
    root = _init(tmp_path, monkeypatch)
    conn = _db(root)
    try:
        conn.execute(
            """
            INSERT INTO review_events (
                entity_type, entity_id, action, from_status, to_status,
                author, reason
            ) VALUES ('claim', 'OBS-D-2026-01-05-01', 'promote',
                      'draft', 'active', 'human', 'seed')
            """
        )
    finally:
        conn.close()

    assert main(["fsck"]) == 0
    out = capsys.readouterr().out
    assert "Status: clean" in out
    assert "REVIEW_EVENT_ENTITY_MISSING" in out
    assert "warning 1" in out


def test_fsck_detects_foreign_key_violation(tmp_path, monkeypatch, capsys):
    root = _init(tmp_path, monkeypatch)
    raw = sqlite3.connect(root / ".stratum" / "stratum.db")
    try:
        raw.execute(
            """
            INSERT INTO claims (
                id, report_id, claim_type, period_start, period_end,
                granularity, entities_json, body, confidence, status,
                content_sha256
            ) VALUES ('OBS-D-2026-01-05-01', 'RPT-MISSING', 'observation',
                      '2026-01-05', '2026-01-06', 'day', '["self"]',
                      'body', 'medium', 'draft', 'seed')
            """
        )
        raw.commit()
    finally:
        raw.close()

    assert main(["fsck"]) == 1
    out = capsys.readouterr().out
    assert "FOREIGN_KEY_VIOLATION" in out


def test_fsck_fix_rolls_back_on_failure(tmp_path, monkeypatch, capsys):
    root = _init(tmp_path, monkeypatch)
    conn = _db(root)
    try:
        _seed_card(conn, "C-SEED-0001", status="active")
        conn.execute(
            """
            CREATE TRIGGER fail_fix BEFORE UPDATE ON cards
            WHEN NEW.status = 'stale'
            BEGIN
                SELECT RAISE(ABORT, 'injected fix failure');
            END
            """
        )
    finally:
        conn.close()

    assert main(["fsck", "--fix"]) == 1
    assert "database failure" in capsys.readouterr().err
    assert _run_count(root) == 0

    conn = _db(root)
    try:
        assert (
            conn.execute(
                "SELECT status FROM cards WHERE id = 'C-SEED-0001'"
            ).fetchone()[0]
            == "active"
        )
    finally:
        conn.close()
