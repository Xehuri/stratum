"""Review CLI: lifecycle, bulk promotion, filters and text output."""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

from stratum.cli import main
from stratum.db import open_db
from stratum.workspace import init_workspace

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


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
    source = root / "source.md"
    shutil.copyfile(FIXTURES / "review" / "source.md", source)
    assert main(["ingest", "source", str(source)]) == 0
    capsys.readouterr()
    conn = _db(root)
    try:
        return conn.execute(
            "SELECT id FROM blocks WHERE retrieval_type = 'structured' "
            "ORDER BY line_start LIMIT 1"
        ).fetchone()[0]
    finally:
        conn.close()


def _add_card(root: Path, block_id: str, capsys, **kwargs) -> str:
    args = [
        "add", "card", "--block", block_id, "--type", kwargs.get("card_type", "event"),
        "--body", kwargs.get("body", "Card body."),
        "--title", kwargs.get("title", "Card title"),
        "--author", kwargs.get("author", "02-daily-review"),
    ]
    if kwargs.get("date") is not None:
        args.extend(["--date", kwargs["date"]])
    if kwargs.get("period_end") is not None:
        args.extend(["--period-end", kwargs["period_end"]])
    assert main(args) == 0
    capsys.readouterr()
    conn = _db(root)
    try:
        rows = conn.execute("SELECT id FROM cards ORDER BY created_at").fetchall()
        return rows[-1][0]
    finally:
        conn.close()


def _add_claim(
    root: Path,
    card_id: str,
    capsys,
    *,
    claim_id: str = "OBS-D-2026-01-05-01",
    claim_type: str = "observation",
    period_start: str = "2026-01-05",
    period_end: str = "2026-01-06",
    granularity: str = "day",
    body: str = "Claim body.",
    author: str = "02-daily-review",
) -> None:
    assert main(
        [
            "add", "claim",
            "--claim-id", claim_id,
            "--claim-type", claim_type,
            "--period-start", period_start,
            "--period-end", period_end,
            "--granularity", granularity,
            "--entity", "self",
            "--evidence", card_id,
            "--confidence", "medium",
            "--body", body,
            "--author", author,
        ]
    ) == 0
    capsys.readouterr()


def test_review_promotes_card_then_claim_and_query_sees_it(
    tmp_path, monkeypatch, capsys
):
    root = _init(tmp_path, monkeypatch)
    block_id = _structured_block(root, capsys)
    card_id = _add_card(root, block_id, capsys)

    assert main(["review", "list", "--type", "card"]) == 0
    listed = capsys.readouterr().out
    assert card_id in listed
    assert "draft" in listed
    assert "card" in listed
    assert not listed.lstrip().startswith("{")

    assert main(
        ["review", "promote", card_id, "--author", "human", "--reason", "checked"]
    ) == 0
    promoted = capsys.readouterr().out
    assert f"Card {card_id}: draft -> active" in promoted

    _add_claim(root, card_id, capsys, body="Promoted observation body.")

    assert main(["query", "--type", "claim"]) == 0
    assert capsys.readouterr().out.strip() == "0 results"

    assert main(["review", "list"]) == 0
    drafts = capsys.readouterr().out
    assert "OBS-D-2026-01-05-01" in drafts
    assert "draft" in drafts

    assert main(
        [
            "review", "promote", "OBS-D-2026-01-05-01",
            "--author", "human", "--reason", "daily review",
        ]
    ) == 0
    assert "draft -> active" in capsys.readouterr().out

    assert main(["query", "--type", "claim"]) == 0
    queried = capsys.readouterr().out
    assert "OBS-D-2026-01-05-01" in queried
    assert "active" in queried
    assert "2026-01-05..2026-01-06" in queried
    assert "Promoted observation body." in queried
    assert "1 results" in queried
    assert not queried.lstrip().startswith("{")

    assert main(["review", "show", "OBS-D-2026-01-05-01"]) == 0
    shown = capsys.readouterr().out
    assert "Claim OBS-D-2026-01-05-01" in shown
    assert "Status: active" in shown
    assert f"card {card_id}" in shown
    assert "promote draft -> active" in shown
    assert "daily review" in shown

    conn = _db(root)
    try:
        assert conn.execute(
            "SELECT status FROM cards WHERE id = ?", (card_id,)
        ).fetchone()[0] == "active"
        assert conn.execute(
            "SELECT status FROM claims WHERE id = 'OBS-D-2026-01-05-01'"
        ).fetchone()[0] == "active"
        assert conn.execute("SELECT COUNT(*) FROM review_events").fetchone()[0] == 2
    finally:
        conn.close()


def test_review_reject_reopen_and_invalid_transitions(
    tmp_path, monkeypatch, capsys
):
    root = _init(tmp_path, monkeypatch)
    block_id = _structured_block(root, capsys)
    card_id = _add_card(root, block_id, capsys)

    assert main(
        ["review", "reject", card_id, "--author", "human", "--reason", "bad card"]
    ) == 0
    assert "draft -> rejected" in capsys.readouterr().out

    assert main(
        ["review", "reject", card_id, "--author", "human", "--reason", "again"]
    ) == 0
    assert "already rejected; no change" in capsys.readouterr().out

    assert main(["review", "list"]) == 0
    assert capsys.readouterr().out.strip() == "0 items"
    assert main(["review", "list", "--status", "rejected"]) == 0
    assert card_id in capsys.readouterr().out

    assert main(
        ["review", "reopen", card_id, "--author", "human", "--reason", "fixed"]
    ) == 0
    assert "rejected -> draft" in capsys.readouterr().out

    assert main(
        ["review", "promote", card_id, "--author", "human", "--reason", "checked"]
    ) == 0
    capsys.readouterr()

    assert main(
        ["review", "reject", card_id, "--author", "human", "--reason", "late"]
    ) == 1
    assert "STATE_INVALID" in capsys.readouterr().err

    conn = _db(root)
    try:
        assert conn.execute(
            "SELECT status FROM cards WHERE id = ?", (card_id,)
        ).fetchone()[0] == "active"
        actions = [
            row[0]
            for row in conn.execute(
                "SELECT action FROM review_events ORDER BY id"
            ).fetchall()
        ]
        assert actions == ["reject", "reopen", "promote"]
    finally:
        conn.close()


def test_review_bulk_promotes_cards_then_daily_claims(
    tmp_path, monkeypatch, capsys
):
    root = _init(tmp_path, monkeypatch)
    block_id = _structured_block(root, capsys)
    card_id = _add_card(root, block_id, capsys, date="2026-01-05")

    _add_claim(root, card_id, capsys, body="First daily claim.")
    _add_claim(
        root, card_id, capsys,
        claim_id="CHG-D-2026-01-05-01",
        claim_type="change",
        body="Second daily claim.",
    )

    assert main(
        [
            "review", "bulk-promote", "--type", "card", "--date", "2026-01-05",
            "--author", "human", "--reason", "daily cards",
        ]
    ) == 0
    out = capsys.readouterr().out
    assert f"Card {card_id}: draft -> active" in out
    assert "Promoted 1 cards for 2026-01-05" in out

    assert main(
        [
            "review", "bulk-promote", "--type", "claim", "--date", "2026-01-05",
            "--author", "human", "--reason", "daily claims",
        ]
    ) == 0
    out = capsys.readouterr().out
    assert "Promoted 2 claims for 2026-01-05" in out

    conn = _db(root)
    try:
        statuses = {
            row[0]: row[1]
            for row in conn.execute("SELECT id, status FROM claims").fetchall()
        }
        assert statuses == {
            "OBS-D-2026-01-05-01": "active",
            "CHG-D-2026-01-05-01": "active",
        }
        assert conn.execute(
            "SELECT status FROM cards WHERE id = ?", (card_id,)
        ).fetchone()[0] == "active"
        assert conn.execute("SELECT COUNT(*) FROM review_events").fetchone()[0] == 3
    finally:
        conn.close()


def test_review_bulk_promote_rejects_weekly_claims(
    tmp_path, monkeypatch, capsys
):
    root = _init(tmp_path, monkeypatch)
    block_id = _structured_block(root, capsys)
    card_id = _add_card(root, block_id, capsys)
    assert main(
        ["review", "promote", card_id, "--author", "human", "--reason", "card"]
    ) == 0
    capsys.readouterr()

    _add_claim(
        root, card_id, capsys,
        claim_id="OBS-W-2026-01-05-01",
        claim_type="observation",
        period_start="2026-01-05",
        period_end="2026-01-12",
        granularity="week",
    )

    assert main(
        [
            "review", "bulk-promote", "--type", "claim",
            "--date", "2026-01-05", "--author", "human", "--reason", "bulk",
        ]
    ) == 1
    err = capsys.readouterr().err
    assert "BULK_PROMOTION_INVALID" in err
    assert "individually" in err

    conn = _db(root)
    try:
        assert conn.execute(
            "SELECT status FROM claims WHERE id = 'OBS-W-2026-01-05-01'"
        ).fetchone()[0] == "draft"
        assert conn.execute(
            "SELECT COUNT(*) FROM review_events WHERE entity_id = "
            "'OBS-W-2026-01-05-01'"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_review_list_filters(tmp_path, monkeypatch, capsys):
    root = _init(tmp_path, monkeypatch)
    block_id = _structured_block(root, capsys)
    card_id = _add_card(root, block_id, capsys, date="2026-01-05")
    _add_claim(root, card_id, capsys)

    assert main(["review", "list"]) == 0
    assert "2 items" in capsys.readouterr().out

    assert main(["review", "list", "--type", "claim"]) == 0
    out = capsys.readouterr().out
    assert "OBS-D-2026-01-05-01" in out
    assert card_id not in out

    assert main(["review", "list", "--date", "2026-01-05"]) == 0
    assert "2 items" in capsys.readouterr().out

    assert main(["review", "list", "--limit", "1"]) == 0
    assert "1 items" in capsys.readouterr().out

    assert main(["review", "list", "--status", "active"]) == 0
    assert capsys.readouterr().out.strip() == "0 items"


def test_review_cli_usage_and_unknown_ids(tmp_path, monkeypatch, capsys):
    _init(tmp_path, monkeypatch)

    assert main(["review"]) == 2
    assert main(["review", "list", "--type", "bogus"]) == 2
    assert main(["review", "list", "--limit", "0"]) == 2
    assert main(["review", "promote", "C-x"]) == 2
    assert main(["review", "promote", "C-x", "--author", "human"]) == 2
    assert main(
        ["review", "promote", "C-x", "--author", "", "--reason", "r"]
    ) == 2

    assert main(
        ["review", "promote", "C-unknown", "--author", "human", "--reason", "r"]
    ) == 1
    assert "NOT_FOUND" in capsys.readouterr().err
    assert main(["review", "show", "OBS-D-2026-01-05-99"]) == 1
    assert "NOT_FOUND" in capsys.readouterr().err
