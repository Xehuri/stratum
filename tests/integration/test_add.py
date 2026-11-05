"""Manual draft creation: add card and add claim idempotency and rules."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from stratum.cli import main
from stratum.db import open_db
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


def _structured_block(root: Path, capsys) -> str:
    source = _write(root, "source.md", "# Source\n\nStable block body.\n")
    assert main(["ingest", "source", str(source)]) == 0
    capsys.readouterr()
    conn = _db(root)
    try:
        return conn.execute(
            "SELECT id FROM blocks WHERE retrieval_type = 'structured' LIMIT 1"
        ).fetchone()[0]
    finally:
        conn.close()


def _seed_card(conn: sqlite3.Connection, card_id: str = "C-SEED-0001") -> str:
    conn.execute(
        """
        INSERT INTO cards (id, card_type, title, body, status)
        VALUES (?, 'event', 'Seed', 'Seed body.', 'draft')
        """,
        (card_id,),
    )
    conn.commit()
    return card_id


def _claim_args(
    card_id: str,
    *,
    claim_id: str = "OBS-D-2026-01-05-01",
    body: str = "Manual observation.",
) -> list[str]:
    return [
        "add",
        "claim",
        "--claim-id",
        claim_id,
        "--claim-type",
        "observation",
        "--period-start",
        "2026-01-05",
        "--period-end",
        "2026-01-06",
        "--granularity",
        "day",
        "--entity",
        "self",
        "--evidence",
        card_id,
        "--confidence",
        "medium",
        "--body",
        body,
    ]


def test_add_card_creates_draft_with_block_link(tmp_path, monkeypatch, capsys):
    root = _init(tmp_path, monkeypatch)
    block_id = _structured_block(root, capsys)

    assert main(
        [
            "add",
            "card",
            "--block",
            block_id,
            "--type",
            "event",
            "--body",
            "Synthetic card body.",
            "--title",
            "Synthetic title",
            "--author",
            "02-daily-review",
        ]
    ) == 0
    out = capsys.readouterr().out
    assert "Added card: C-" in out
    assert "Blocks: 1" in out
    assert not out.lstrip().startswith("{")

    conn = _db(root)
    try:
        card = conn.execute("SELECT * FROM cards").fetchone()
        assert card["status"] == "draft"
        assert card["card_type"] == "event"
        assert card["title"] == "Synthetic title"
        assert card["author"] == "02-daily-review"
        link = conn.execute("SELECT * FROM card_blocks").fetchone()
        assert link["card_id"] == card["id"]
        assert link["block_id"] == block_id
        assert link["quote_sha256"]
    finally:
        conn.close()


def test_add_card_is_idempotent(tmp_path, monkeypatch, capsys):
    root = _init(tmp_path, monkeypatch)
    block_id = _structured_block(root, capsys)
    args = ["add", "card", "--block", block_id, "--type", "event", "--body", "Same."]

    assert main(args) == 0
    capsys.readouterr()
    assert main(args) == 0
    assert capsys.readouterr().out.strip() == "unchanged"

    conn = _db(root)
    try:
        assert conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM card_blocks").fetchone()[0] == 1
    finally:
        conn.close()


def test_add_card_same_body_different_title_is_one_card(
    tmp_path, monkeypatch, capsys
):
    root = _init(tmp_path, monkeypatch)
    block_id = _structured_block(root, capsys)
    assert main(
        [
            "add", "card", "--block", block_id, "--type", "event",
            "--body", "Same body.", "--title", "First title",
        ]
    ) == 0
    capsys.readouterr()
    assert main(
        [
            "add", "card", "--block", block_id, "--type", "event",
            "--body", "Same body.", "--title", "Second title",
        ]
    ) == 0
    assert capsys.readouterr().out.strip() == "unchanged"

    conn = _db(root)
    try:
        assert conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 1
    finally:
        conn.close()


def test_add_card_rejects_missing_block(tmp_path, monkeypatch, capsys):
    root = _init(tmp_path, monkeypatch)

    assert main(
        ["add", "card", "--block", "BLK-missing", "--type", "event", "--body", "x"]
    ) == 1
    assert "EVIDENCE_UNRESOLVED" in capsys.readouterr().err

    conn = _db(root)
    try:
        assert conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 0
    finally:
        conn.close()


def test_add_card_rejects_prose_block(tmp_path, monkeypatch, capsys):
    root = _init(tmp_path, monkeypatch)
    prose = _write(root, "prose.md", "# Prose\n\nA prose block.\n")
    assert main(["ingest", "source", str(prose), "--profile", "whole-document"]) == 0
    capsys.readouterr()

    conn = _db(root)
    try:
        block_id = conn.execute(
            "SELECT id FROM blocks WHERE retrieval_type = 'prose'"
        ).fetchone()[0]
    finally:
        conn.close()

    assert main(
        ["add", "card", "--block", block_id, "--type", "event", "--body", "x"]
    ) == 1
    assert "EVIDENCE_PROSE" in capsys.readouterr().err


def test_add_claim_creates_draft_with_evidence(tmp_path, monkeypatch, capsys):
    root = _init(tmp_path, monkeypatch)
    conn = _db(root)
    try:
        card_id = _seed_card(conn)
    finally:
        conn.close()

    assert main(_claim_args(card_id)) == 0
    out = capsys.readouterr().out
    assert "Added claim: OBS-D-2026-01-05-01" in out
    assert "Evidence: 1" in out

    conn = _db(root)
    try:
        claim = conn.execute("SELECT * FROM claims").fetchone()
        assert claim["status"] == "draft"
        assert claim["report_id"] is None
        assert claim["claim_type"] == "observation"
        link = conn.execute("SELECT * FROM evidence_links").fetchone()
        assert link["owner_type"] == "claim"
        assert link["owner_id"] == "OBS-D-2026-01-05-01"
        assert link["target_type"] == "card"
        assert link["target_id"] == card_id
    finally:
        conn.close()


def test_add_claim_accepts_repeated_entities(tmp_path, monkeypatch, capsys):
    root = _init(tmp_path, monkeypatch)
    conn = _db(root)
    try:
        card_id = _seed_card(conn)
    finally:
        conn.close()

    args = _claim_args(card_id)
    entity_index = args.index("--entity")
    args[entity_index + 2:entity_index + 2] = ["--entity", "other"]
    assert main(args) == 0

    conn = _db(root)
    try:
        claim = conn.execute("SELECT * FROM claims").fetchone()
        assert claim["entities_json"] == '["self","other"]'
    finally:
        conn.close()


def test_add_claim_is_idempotent_and_updates_drafts(tmp_path, monkeypatch, capsys):
    root = _init(tmp_path, monkeypatch)
    conn = _db(root)
    try:
        card_id = _seed_card(conn)
    finally:
        conn.close()

    assert main(_claim_args(card_id)) == 0
    capsys.readouterr()

    assert main(_claim_args(card_id)) == 0
    assert capsys.readouterr().out.strip() == "unchanged"

    assert main(_claim_args(card_id, body="Changed body.")) == 0
    out = capsys.readouterr().out
    assert "Added claim:" in out

    conn = _db(root)
    try:
        assert conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 1
        claim = conn.execute("SELECT * FROM claims").fetchone()
        assert claim["body"] == "Changed body."
        assert conn.execute("SELECT COUNT(*) FROM evidence_links").fetchone()[0] == 1
    finally:
        conn.close()


def test_add_claim_rejects_active_claim_update(tmp_path, monkeypatch, capsys):
    root = _init(tmp_path, monkeypatch)
    conn = _db(root)
    try:
        card_id = _seed_card(conn)
    finally:
        conn.close()

    assert main(_claim_args(card_id)) == 0
    capsys.readouterr()

    conn = _db(root)
    try:
        conn.execute("UPDATE claims SET status = 'active'")
        conn.commit()
    finally:
        conn.close()

    assert main(_claim_args(card_id, body="New body.")) == 1
    assert "CLAIM_IMMUTABLE" in capsys.readouterr().err


def test_add_claim_rejects_invalid_identifier(tmp_path, monkeypatch, capsys):
    root = _init(tmp_path, monkeypatch)
    conn = _db(root)
    try:
        card_id = _seed_card(conn)
    finally:
        conn.close()

    assert main(_claim_args(card_id, claim_id="OBS-X-2026-01-05-01")) == 1
    assert "CLAIM_ID_INVALID" in capsys.readouterr().err

    assert main(_claim_args(card_id, claim_id="CHG-D-2026-01-05-01")) == 1
    assert "CLAIM_SCALE_INVALID" in capsys.readouterr().err


def test_add_claim_rejects_empty_body(tmp_path, monkeypatch, capsys):
    root = _init(tmp_path, monkeypatch)
    conn = _db(root)
    try:
        card_id = _seed_card(conn)
    finally:
        conn.close()

    assert main(_claim_args(card_id, body="")) == 1
    assert "CLAIM_FIELD_MISSING" in capsys.readouterr().err


def test_add_claim_rejects_unresolved_evidence(tmp_path, monkeypatch, capsys):
    root = _init(tmp_path, monkeypatch)

    assert main(_claim_args("C-MISSING")) == 1
    assert "EVIDENCE_UNRESOLVED" in capsys.readouterr().err

    conn = _db(root)
    try:
        assert conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 0
    finally:
        conn.close()


def test_add_claim_requires_evidence_flag(tmp_path, monkeypatch, capsys):
    root = _init(tmp_path, monkeypatch)
    assert main(
        [
            "add",
            "claim",
            "--claim-id",
            "OBS-D-2026-01-05-01",
            "--claim-type",
            "observation",
            "--period-start",
            "2026-01-05",
            "--period-end",
            "2026-01-06",
            "--granularity",
            "day",
            "--entity",
            "self",
            "--confidence",
            "medium",
            "--body",
            "x",
        ]
    ) == 2
    assert "evidence" in capsys.readouterr().err.lower()


def test_add_claim_report_id_roundtrip_and_conflict(tmp_path, monkeypatch, capsys):
    root = _init(tmp_path, monkeypatch)
    conn = _db(root)
    try:
        card_id = _seed_card(conn)
        conn.execute(
            """
            INSERT INTO reports (id, report_type, title, status, contract_version)
            VALUES ('RPT-D-2026-01-05', 'daily', 'Report', 'draft', 1)
            """
        )
        conn.commit()
    finally:
        conn.close()

    args = _claim_args(card_id) + ["--report-id", "RPT-D-2026-01-05"]
    assert main(args) == 0

    conn = _db(root)
    try:
        claim = conn.execute("SELECT * FROM claims").fetchone()
        assert claim["report_id"] == "RPT-D-2026-01-05"
    finally:
        conn.close()

    capsys.readouterr()
    assert main(_claim_args(card_id)) == 1
    assert "CLAIM_ID_CONFLICT" in capsys.readouterr().err


def test_add_claim_unknown_report_exits_one(tmp_path, monkeypatch, capsys):
    root = _init(tmp_path, monkeypatch)
    conn = _db(root)
    try:
        card_id = _seed_card(conn)
    finally:
        conn.close()

    args = _claim_args(card_id) + ["--report-id", "RPT-D-2099-01-01"]
    assert main(args) == 1
    assert "report not found" in capsys.readouterr().err.lower()


def test_add_card_with_date_sets_period_and_is_bulk_promotable(
    tmp_path, monkeypatch, capsys
):
    root = _init(tmp_path, monkeypatch)
    block_id = _structured_block(root, capsys)

    assert main(
        [
            "add", "card", "--block", block_id, "--type", "event",
            "--body", "Dated card.", "--title", "Dated",
            "--date", "2026-01-05",
        ]
    ) == 0
    capsys.readouterr()

    conn = _db(root)
    try:
        card = conn.execute("SELECT * FROM cards").fetchone()
        assert card["period_start"] == "2026-01-05"
        assert card["period_end"] == "2026-01-06"
        card_id = card["id"]
    finally:
        conn.close()

    assert main(
        [
            "review", "bulk-promote", "--type", "card",
            "--date", "2026-01-05", "--author", "human", "--reason", "daily cards",
        ]
    ) == 0
    assert f"Card {card_id}: draft -> active" in capsys.readouterr().out

    conn = _db(root)
    try:
        assert conn.execute(
            "SELECT status FROM cards WHERE id = ?", (card_id,)
        ).fetchone()[0] == "active"
    finally:
        conn.close()


def test_add_card_same_body_different_dates_creates_two_cards(
    tmp_path, monkeypatch, capsys
):
    root = _init(tmp_path, monkeypatch)
    block_id = _structured_block(root, capsys)

    args = [
        "add", "card", "--block", block_id, "--type", "event",
        "--body", "Same dated body.", "--title", "Same",
    ]
    assert main(args + ["--date", "2026-01-05"]) == 0
    first = capsys.readouterr().out
    assert main(args + ["--date", "2026-01-06"]) == 0
    second = capsys.readouterr().out
    assert first != second

    conn = _db(root)
    try:
        cards = conn.execute(
            "SELECT id, period_start, period_end FROM cards ORDER BY period_start"
        ).fetchall()
        assert len(cards) == 2
        assert (cards[0]["period_start"], cards[0]["period_end"]) == (
            "2026-01-05", "2026-01-06",
        )
        assert (cards[1]["period_start"], cards[1]["period_end"]) == (
            "2026-01-06", "2026-01-07",
        )
    finally:
        conn.close()


def test_add_card_with_explicit_period_end(tmp_path, monkeypatch, capsys):
    root = _init(tmp_path, monkeypatch)
    block_id = _structured_block(root, capsys)

    assert main(
        [
            "add", "card", "--block", block_id, "--type", "event",
            "--body", "Range card.", "--date", "2026-01-05",
            "--period-end", "2026-01-08",
        ]
    ) == 0
    capsys.readouterr()

    conn = _db(root)
    try:
        card = conn.execute("SELECT * FROM cards").fetchone()
        assert card["period_start"] == "2026-01-05"
        assert card["period_end"] == "2026-01-08"
    finally:
        conn.close()


def test_add_card_without_date_keeps_null_period(tmp_path, monkeypatch, capsys):
    root = _init(tmp_path, monkeypatch)
    block_id = _structured_block(root, capsys)

    assert main(
        ["add", "card", "--block", block_id, "--type", "event", "--body", "Undated."]
    ) == 0
    capsys.readouterr()

    conn = _db(root)
    try:
        card = conn.execute("SELECT * FROM cards").fetchone()
        assert card["period_start"] is None
        assert card["period_end"] is None
    finally:
        conn.close()


def test_add_card_rejects_invalid_date_and_period_end(
    tmp_path, monkeypatch, capsys
):
    root = _init(tmp_path, monkeypatch)
    block_id = _structured_block(root, capsys)
    base = ["add", "card", "--block", block_id, "--type", "event", "--body", "x"]

    assert main(base + ["--date", "2026-02-30"]) == 2
    assert "invalid --date" in capsys.readouterr().err

    assert main(
        base + ["--date", "2026-01-05", "--period-end", "2026-01-05"]
    ) == 2
    assert "later than --date" in capsys.readouterr().err

    assert main(base + ["--date", "2026-01-05", "--period-end", "2026-01-01"]) == 2
    assert "later than --date" in capsys.readouterr().err

    assert main(base + ["--period-end", "2026-01-06"]) == 2
    assert "requires --date" in capsys.readouterr().err

    conn = _db(root)
    try:
        assert conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 0
    finally:
        conn.close()


def _seed_two_cards(root: Path) -> None:
    conn = _db(root)
    try:
        _seed_card(conn, "C-REL-0001")
        _seed_card(conn, "C-REL-0002")
    finally:
        conn.close()


def test_add_relation_creates_explicit_causal_edge(tmp_path, monkeypatch, capsys):
    root = _init(tmp_path, monkeypatch)
    _seed_two_cards(root)

    assert main(
        [
            "add",
            "relation",
            "--from-card",
            "C-REL-0001",
            "--to-card",
            "C-REL-0002",
            "--type",
            "triggered_by",
        ]
    ) == 0
    out = capsys.readouterr().out
    assert "Added relation: REL-" in out
    assert "triggered_by" in out

    conn = _db(root)
    try:
        relation = conn.execute("SELECT * FROM card_relations").fetchone()
        assert relation["from_card_id"] == "C-REL-0001"
        assert relation["to_card_id"] == "C-REL-0002"
        assert relation["relation_type"] == "triggered_by"
        assert relation["claim_type"] == "causal"
        assert relation["source"] == "agent"
    finally:
        conn.close()


def test_add_relation_is_idempotent(tmp_path, monkeypatch, capsys):
    root = _init(tmp_path, monkeypatch)
    _seed_two_cards(root)
    args = [
        "add", "relation",
        "--from-card", "C-REL-0001",
        "--to-card", "C-REL-0002",
        "--type", "precedes",
    ]

    assert main(args) == 0
    capsys.readouterr()
    assert main(args) == 0
    assert capsys.readouterr().out.strip() == "unchanged"

    conn = _db(root)
    try:
        assert conn.execute("SELECT COUNT(*) FROM card_relations").fetchone()[0] == 1
    finally:
        conn.close()


def test_add_relation_rejects_invalid_edge(tmp_path, monkeypatch, capsys):
    root = _init(tmp_path, monkeypatch)
    _seed_two_cards(root)

    assert main(
        [
            "add", "relation",
            "--from-card", "C-REL-0001",
            "--to-card", "C-REL-0002",
            "--type", "not-a-relation",
        ]
    ) == 1
    assert "CARD_RELATION_INVALID" in capsys.readouterr().err

    assert main(
        [
            "add", "relation",
            "--from-card", "C-REL-0001",
            "--to-card", "C-REL-0001",
            "--type", "precedes",
        ]
    ) == 1
    assert "CARD_RELATION_INVALID" in capsys.readouterr().err

    assert main(
        [
            "add", "relation",
            "--from-card", "C-REL-0001",
            "--to-card", "C-MISSING",
            "--type", "precedes",
        ]
    ) == 1
    assert "CARD_NOT_FOUND" in capsys.readouterr().err
