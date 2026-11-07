"""Explicit claim relation writes and claim revision."""

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


def _seed_claim(
    conn: sqlite3.Connection,
    claim_id: str,
    *,
    status: str = "active",
    claim_type: str = "observation",
    period_start: str = "2026-01-05",
    period_end: str = "2026-01-06",
    granularity: str = "day",
    body: str = "Claim body.",
    data_gap: str | None = "test gap",
    evidence_card: str | None = None,
) -> None:
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
    if evidence_card is not None:
        conn.execute(
            """
            INSERT INTO evidence_links (
                id, owner_type, owner_id, target_type, target_id, role
            ) VALUES (?, 'claim', ?, 'card', ?, 'evidence')
            """,
            (f"EVL-{claim_id}", claim_id, evidence_card),
        )
    conn.commit()


def _claim_relation_args(
    from_claim: str, to_claim: str, relation_type: str
) -> list[str]:
    return [
        "add",
        "claim-relation",
        "--from-claim",
        from_claim,
        "--to-claim",
        to_claim,
        "--type",
        relation_type,
    ]


def test_claim_relation_derived_from_weekly_to_daily(
    tmp_path, monkeypatch, capsys
):
    root = _init(tmp_path, monkeypatch)
    conn = _db(root)
    try:
        _seed_claim(
            conn,
            "OBS-W-2026-01-05-01",
            status="draft",
            period_start="2026-01-05",
            period_end="2026-01-12",
            granularity="week",
        )
        _seed_claim(conn, "OBS-D-2026-01-05-01")
    finally:
        conn.close()

    args = _claim_relation_args(
        "OBS-W-2026-01-05-01",
        "OBS-D-2026-01-05-01",
        "derived_from",
    )
    assert main(args) == 0
    assert "Added claim relation: CLR-" in capsys.readouterr().out

    assert main(args) == 0
    assert capsys.readouterr().out.strip() == "unchanged"

    conn = _db(root)
    try:
        relation = conn.execute("SELECT * FROM claim_relations").fetchone()
        assert relation["from_claim_id"] == "OBS-W-2026-01-05-01"
        assert relation["to_claim_id"] == "OBS-D-2026-01-05-01"
        assert relation["relation_type"] == "derived_from"
    finally:
        conn.close()


def test_claim_relation_rejects_daily_derived_from(
    tmp_path, monkeypatch, capsys
):
    root = _init(tmp_path, monkeypatch)
    conn = _db(root)
    try:
        _seed_claim(conn, "OBS-D-2026-01-05-02", status="draft")
        _seed_claim(conn, "OBS-D-2026-01-05-01")
    finally:
        conn.close()

    assert main(
        _claim_relation_args(
            "OBS-D-2026-01-05-02",
            "OBS-D-2026-01-05-01",
            "derived_from",
        )
    ) == 1
    assert "DERIVED_FROM_LAYER" in capsys.readouterr().err


def test_claim_relation_rejects_wrong_layer_and_period(
    tmp_path, monkeypatch, capsys
):
    root = _init(tmp_path, monkeypatch)
    conn = _db(root)
    try:
        _seed_claim(
            conn,
            "OBS-W-2026-01-05-01",
            status="draft",
            period_start="2026-01-05",
            period_end="2026-01-12",
            granularity="week",
        )
        _seed_claim(
            conn,
            "OBS-W-2026-01-05-02",
            status="draft",
            period_start="2026-01-05",
            period_end="2026-01-12",
            granularity="week",
        )
        _seed_claim(
            conn,
            "OBS-D-2026-02-01-01",
            period_start="2026-02-01",
            period_end="2026-02-02",
        )
    finally:
        conn.close()

    assert main(
        _claim_relation_args(
            "OBS-W-2026-01-05-01",
            "OBS-W-2026-01-05-02",
            "derived_from",
        )
    ) == 1
    assert "DERIVED_FROM_LAYER" in capsys.readouterr().err

    assert main(
        _claim_relation_args(
            "OBS-W-2026-01-05-01",
            "OBS-D-2026-02-01-01",
            "derived_from",
        )
    ) == 1
    assert "DERIVED_FROM_LAYER" in capsys.readouterr().err


def test_claim_relation_supersedes_same_layer(
    tmp_path, monkeypatch, capsys
):
    root = _init(tmp_path, monkeypatch)
    conn = _db(root)
    try:
        _seed_claim(conn, "OBS-D-2026-01-05-02", status="draft")
        _seed_claim(conn, "OBS-D-2026-01-05-01")
    finally:
        conn.close()

    assert main(
        _claim_relation_args(
            "OBS-D-2026-01-05-02",
            "OBS-D-2026-01-05-01",
            "supersedes",
        )
    ) == 0

    conn = _db(root)
    try:
        relation = conn.execute("SELECT * FROM claim_relations").fetchone()
        assert relation["relation_type"] == "supersedes"
    finally:
        conn.close()


def test_revise_active_claim_creates_superseding_draft(
    tmp_path, monkeypatch, capsys
):
    root = _init(tmp_path, monkeypatch)
    conn = _db(root)
    try:
        _seed_claim(conn, "OBS-D-2026-01-05-01", body="Old conclusion.")
    finally:
        conn.close()

    assert main(
        [
            "revise",
            "claim",
            "--claim-id",
            "OBS-D-2026-01-05-01",
            "--body",
            "Weekly corrected conclusion.",
            "--author",
            "04-weekly-review",
            "--reason",
            "weekly cross-check corrected the daily conclusion",
        ]
    ) == 0
    out = capsys.readouterr().out
    assert "Added revised claim: OBS-D-2026-01-05-02" in out
    assert "Supersedes: OBS-D-2026-01-05-01" in out

    conn = _db(root)
    try:
        old = conn.execute(
            "SELECT * FROM claims WHERE id = 'OBS-D-2026-01-05-01'"
        ).fetchone()
        new = conn.execute(
            "SELECT * FROM claims WHERE id = 'OBS-D-2026-01-05-02'"
        ).fetchone()
        assert old["status"] == "active"
        assert new["status"] == "draft"
        assert new["body"] == "Weekly corrected conclusion."
        assert new["author"] == "04-weekly-review"
        relation = conn.execute(
            """
            SELECT relation_type FROM claim_relations
            WHERE from_claim_id = 'OBS-D-2026-01-05-02'
              AND to_claim_id = 'OBS-D-2026-01-05-01'
            """
        ).fetchone()
        assert relation["relation_type"] == "supersedes"
    finally:
        conn.close()


def test_revise_draft_updates_in_place(tmp_path, monkeypatch, capsys):
    root = _init(tmp_path, monkeypatch)
    conn = _db(root)
    try:
        _seed_claim(
            conn, "OBS-D-2026-01-05-01", status="draft", body="Draft body."
        )
    finally:
        conn.close()

    assert main(
        [
            "revise",
            "claim",
            "--claim-id",
            "OBS-D-2026-01-05-01",
            "--body",
            "Edited draft body.",
            "--author",
            "02-daily-review",
            "--reason",
            "fix draft before promotion",
        ]
    ) == 0
    assert "Updated claim: OBS-D-2026-01-05-01" in capsys.readouterr().out

    conn = _db(root)
    try:
        claim = conn.execute("SELECT * FROM claims").fetchone()
        assert claim["body"] == "Edited draft body."
        assert claim["status"] == "draft"
        assert conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM claim_relations").fetchone()[0] == 0
    finally:
        conn.close()


def test_claim_relation_rejects_missing_claim(tmp_path, monkeypatch, capsys):
    root = _init(tmp_path, monkeypatch)
    conn = _db(root)
    try:
        _seed_claim(conn, "OBS-D-2026-01-05-01")
    finally:
        conn.close()

    assert main(
        _claim_relation_args(
            "OBS-W-2026-01-05-01",
            "OBS-D-2026-01-05-01",
            "derived_from",
        )
    ) == 1
    assert "CLAIM_NOT_FOUND" in capsys.readouterr().err


def test_claim_relation_derived_from_biweekly_to_weekly(
    tmp_path, monkeypatch, capsys
):
    root = _init(tmp_path, monkeypatch)
    conn = _db(root)
    try:
        _seed_claim(
            conn,
            "PAT-B-2026-01-05-01",
            status="draft",
            claim_type="pattern",
            period_start="2026-01-05",
            period_end="2026-01-19",
            granularity="range",
        )
        _seed_claim(
            conn,
            "OBS-W-2026-01-05-01",
            period_start="2026-01-05",
            period_end="2026-01-12",
            granularity="week",
        )
    finally:
        conn.close()

    assert main(
        _claim_relation_args(
            "PAT-B-2026-01-05-01",
            "OBS-W-2026-01-05-01",
            "derived_from",
        )
    ) == 0
    assert "Added claim relation: CLR-" in capsys.readouterr().out


def test_claim_relation_allows_active_derived_source_with_active_target(
    tmp_path, monkeypatch, capsys
):
    root = _init(tmp_path, monkeypatch)
    conn = _db(root)
    try:
        _seed_claim(
            conn,
            "OBS-W-2026-01-05-01",
            status="active",
            period_start="2026-01-05",
            period_end="2026-01-12",
            granularity="week",
        )
        _seed_claim(conn, "OBS-D-2026-01-05-01")
    finally:
        conn.close()

    assert main(
        _claim_relation_args(
            "OBS-W-2026-01-05-01",
            "OBS-D-2026-01-05-01",
            "derived_from",
        )
    ) == 0
    assert "Added claim relation: CLR-" in capsys.readouterr().out


def test_claim_relation_rejects_active_derived_source_with_draft_target(
    tmp_path, monkeypatch, capsys
):
    root = _init(tmp_path, monkeypatch)
    conn = _db(root)
    try:
        _seed_claim(
            conn,
            "OBS-W-2026-01-05-01",
            status="active",
            period_start="2026-01-05",
            period_end="2026-01-12",
            granularity="week",
        )
        _seed_claim(conn, "OBS-D-2026-01-05-01", status="draft")
    finally:
        conn.close()

    assert main(
        _claim_relation_args(
            "OBS-W-2026-01-05-01",
            "OBS-D-2026-01-05-01",
            "derived_from",
        )
    ) == 1
    assert "RELATION_INVALID" in capsys.readouterr().err


def test_revise_and_promote_daily_claim_from_weekly_author(
    tmp_path, monkeypatch, capsys
):
    root = _init(tmp_path, monkeypatch)
    conn = _db(root)
    try:
        conn.execute(
            """
            INSERT INTO cards (id, card_type, title, body, status)
            VALUES ('C-EVID-0001', 'event', 'Evidence', 'Evidence body.', 'active')
            """
        )
        _seed_claim(
            conn,
            "OBS-D-2026-01-05-01",
            body="Original daily conclusion.",
            data_gap=None,
            evidence_card="C-EVID-0001",
        )
    finally:
        conn.close()

    assert main(
        [
            "revise",
            "claim",
            "--claim-id",
            "OBS-D-2026-01-05-01",
            "--body",
            "Corrected daily conclusion.",
            "--author",
            "04-weekly-review",
            "--reason",
            "weekly cross-check",
        ]
    ) == 0
    capsys.readouterr()

    assert main(
        [
            "review",
            "promote",
            "OBS-D-2026-01-05-02",
            "--author",
            "04-weekly-review",
            "--reason",
            "weekly corrected daily claim",
        ]
    ) == 0

    conn = _db(root)
    try:
        old = conn.execute(
            "SELECT status FROM claims WHERE id = 'OBS-D-2026-01-05-01'"
        ).fetchone()
        new = conn.execute(
            "SELECT status FROM claims WHERE id = 'OBS-D-2026-01-05-02'"
        ).fetchone()
        assert old["status"] == "superseded"
        assert new["status"] == "active"
    finally:
        conn.close()


def test_claim_relation_remains_idempotent_after_source_promotion(
    tmp_path, monkeypatch, capsys
):
    root = _init(tmp_path, monkeypatch)
    conn = _db(root)
    try:
        _seed_claim(
            conn,
            "OBS-W-2026-01-05-01",
            status="draft",
            period_start="2026-01-05",
            period_end="2026-01-12",
            granularity="week",
        )
        _seed_claim(conn, "OBS-D-2026-01-05-01")
    finally:
        conn.close()

    args = _claim_relation_args(
        "OBS-W-2026-01-05-01",
        "OBS-D-2026-01-05-01",
        "derived_from",
    )
    assert main(args) == 0
    capsys.readouterr()

    conn = _db(root)
    try:
        conn.execute(
            "UPDATE claims SET status = 'active' WHERE id = 'OBS-W-2026-01-05-01'"
        )
        conn.commit()
    finally:
        conn.close()

    assert main(args) == 0
    assert capsys.readouterr().out.strip() == "unchanged"
