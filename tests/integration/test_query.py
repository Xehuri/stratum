"""Query CLI: active defaults, draft opt-in, filters, context and text output."""

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
) -> None:
    conn.execute(
        """
        INSERT INTO claims (
            id, claim_type, period_start, period_end, granularity,
            entities_json, body, confidence, status, content_sha256
        ) VALUES (?, ?, ?, ?, ?, '["self"]', ?, 'medium', ?, ?)
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
        ),
    )


def _seed_card(
    conn: sqlite3.Connection,
    card_id: str,
    *,
    status: str = "active",
    title: str = "Card title",
    body: str = "Card body.",
    period_start: str | None = "2026-01-05",
    period_end: str | None = "2026-01-06",
    claim_type: str = "observation",
) -> None:
    conn.execute(
        """
        INSERT INTO cards (
            id, card_type, title, body, status, claim_type,
            period_start, period_end, granularity
        ) VALUES (?, 'event', ?, ?, ?, ?, ?, ?, 'day')
        """,
        (
            card_id,
            title,
            body,
            status,
            claim_type,
            period_start,
            period_end,
        ),
    )


def _seed_report(
    conn: sqlite3.Connection,
    report_id: str,
    *,
    status: str = "draft",
    title: str = "Daily report text",
    period_start: str = "2026-01-05",
    period_end: str = "2026-01-06",
) -> None:
    conn.execute(
        """
        INSERT INTO reports (
            id, report_type, title, period_start, period_end, status,
            contract_version
        ) VALUES (?, 'daily', ?, ?, ?, ?, 1)
        """,
        (report_id, title, period_start, period_end, status),
    )


def _run(capsys, *args: str) -> str:
    assert main(list(args)) == 0
    return capsys.readouterr().out


def test_query_active_default_and_include_draft(tmp_path, monkeypatch, capsys):
    root = _init(tmp_path, monkeypatch)
    conn = _db(root)
    try:
        _seed_claim(conn, "OBS-D-2026-01-05-01", body="active body")
        _seed_claim(conn, "OBS-D-2026-01-05-02", status="draft", body="draft body")
        _seed_claim(
            conn, "OBS-D-2026-01-06-01", status="rejected", body="rejected body",
            period_start="2026-01-06", period_end="2026-01-07",
        )
        _seed_claim(
            conn, "OBS-D-2026-01-07-01", status="stale", body="stale body",
            period_start="2026-01-07", period_end="2026-01-08",
        )
        _seed_claim(
            conn, "OBS-D-2026-01-08-01", status="superseded",
            body="superseded body",
            period_start="2026-01-08", period_end="2026-01-09",
        )
        conn.commit()
    finally:
        conn.close()

    out = _run(capsys, "query", "--type", "claim")
    assert "OBS-D-2026-01-05-01" in out
    assert "active body" in out
    assert "draft body" not in out
    assert "rejected body" not in out
    assert "stale body" not in out
    assert "superseded body" not in out
    assert "1 results" in out
    assert not out.lstrip().startswith("{")

    out = _run(capsys, "query", "--type", "claim", "--include-draft")
    assert "draft body" in out
    assert "rejected body" in out
    assert "stale body" in out
    assert "superseded body" not in out
    assert "4 results" in out

    out = _run(capsys, "query", "--type", "claim", "--status", "superseded")
    assert "superseded body" in out
    assert "active body" not in out
    assert "1 results" in out

    out = _run(
        capsys,
        "query", "--type", "claim", "--include-draft", "--status", "rejected",
    )
    assert "rejected body" in out
    assert "draft body" not in out


def test_query_claim_type_date_and_grep_filters(tmp_path, monkeypatch, capsys):
    root = _init(tmp_path, monkeypatch)
    conn = _db(root)
    try:
        _seed_claim(
            conn, "OBS-D-2026-01-05-01",
            body="潮间带 observation with 100% coverage",
        )
        _seed_claim(
            conn, "CHG-D-2026-01-06-01",
            claim_type="change",
            period_start="2026-01-06",
            period_end="2026-01-07",
            body="A later change.",
        )
        conn.commit()
    finally:
        conn.close()

    out = _run(capsys, "query", "--type", "claim", "--claim-type", "observation")
    assert "潮间带" in out
    assert "later change" not in out

    out = _run(
        capsys,
        "query", "--type", "claim",
        "--from", "2026-01-05", "--to", "2026-01-06",
    )
    assert "潮间带" in out
    assert "later change" not in out

    out = _run(
        capsys,
        "query", "--type", "claim",
        "--from", "2026-01-06", "--to", "2026-01-07",
    )
    assert "later change" in out
    assert "潮间带" not in out

    out = _run(capsys, "query", "--type", "claim", "--grep", "潮间带")
    assert "1 results" in out
    assert "later change" not in out

    out = _run(capsys, "query", "--type", "claim", "--grep", "100%")
    assert "1 results" in out


def test_query_cards_reports_blocks_and_aliases(tmp_path, monkeypatch, capsys):
    root = _init(tmp_path, monkeypatch)
    conn = _db(root)
    try:
        _seed_card(conn, "C-ACTIVE-0001", body="Active card body")
        _seed_card(conn, "C-DRAFT-0001", status="draft", body="Draft card body")
        _seed_report(conn, "RPT-D-2026-01-05", title="Visible report title")
        conn.commit()
    finally:
        conn.close()

    structured = _write(
        root,
        "structured.md",
        "# One\n\nFirst structured body.\n\n# Two\n\nSecond structured body.\n",
    )
    prose = _write(root, "prose.md", "# Prose\n\nA prose-only body.\n")
    assert main(["ingest", "source", str(structured)]) == 0
    assert main(["ingest", "source", str(prose), "--profile", "whole-document"]) == 0
    capsys.readouterr()

    out = _run(capsys, "query", "--type", "card")
    assert "Active card body" in out
    assert "Draft card body" not in out
    out = _run(capsys, "query", "--type", "card", "--include-draft")
    assert "Draft card body" in out

    out = _run(capsys, "query", "--type", "report")
    assert "Visible report title" in out
    assert "1 results" in out

    out = _run(capsys, "query", "--type", "structured")
    assert "First structured body" in out
    assert "Second structured body" in out
    assert "A prose-only body" not in out

    out = _run(capsys, "query", "--type", "prose")
    assert "A prose-only body" in out
    assert "First structured body" not in out

    out = _run(capsys, "query", "--type", "block")
    assert "First structured body" in out
    assert "A prose-only body" in out

    conn = _db(root)
    try:
        stale_id = conn.execute(
            "SELECT id FROM blocks WHERE snapshot_text LIKE '%First structured%'"
        ).fetchone()[0]
        conn.execute("UPDATE blocks SET status = 'stale' WHERE id = ?", (stale_id,))
        conn.commit()
    finally:
        conn.close()
    out = _run(capsys, "query", "--type", "structured")
    assert "First structured body" not in out
    out = _run(capsys, "query", "--type", "structured", "--include-draft")
    assert "First structured body" in out


def test_query_block_context_includes_neighbours(tmp_path, monkeypatch, capsys):
    root = _init(tmp_path, monkeypatch)
    source = _write(
        root,
        "context.md",
        "# First\n\nfirst body\n\n# Middle\n\nmiddle unique body\n\n"
        "# Last\n\nlast body\n",
    )
    assert main(["ingest", "source", str(source)]) == 0
    capsys.readouterr()

    conn = _db(root)
    try:
        rows = conn.execute(
            "SELECT id FROM blocks ORDER BY line_start"
        ).fetchall()
        first_id, middle_id, last_id = [row[0] for row in rows]
    finally:
        conn.close()

    out = _run(
        capsys,
        "query", "--type", "structured", "--grep", "middle unique",
        "--context", "1",
    )
    assert middle_id in out
    assert first_id in out
    assert last_id in out
    assert "1 results" in out
    assert "  " + first_id in out


def test_query_no_results_and_read_only(tmp_path, monkeypatch, capsys):
    root = _init(tmp_path, monkeypatch)
    conn = _db(root)
    try:
        _seed_claim(conn, "OBS-D-2026-01-05-01")
        conn.commit()
        before = {
            "claims": conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0],
            "events": conn.execute(
                "SELECT COUNT(*) FROM review_events"
            ).fetchone()[0],
        }
    finally:
        conn.close()

    out = _run(capsys, "query", "--type", "claim", "--grep", "not-present")
    assert out.strip() == "0 results"

    conn = _db(root)
    try:
        after = {
            "claims": conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0],
            "events": conn.execute(
                "SELECT COUNT(*) FROM review_events"
            ).fetchone()[0],
        }
    finally:
        conn.close()
    assert after == before


def test_query_bad_arguments_exit_two(tmp_path, monkeypatch, capsys):
    _init(tmp_path, monkeypatch)

    assert main(["query", "--type", "bogus"]) == 2
    assert main(["query", "--from", "2026-13-01"]) == 2
    assert main(["query", "--from", "2026-01-07", "--to", "2026-01-05"]) == 2
    assert main(["query", "--context", "1"]) == 2
    assert main(["query", "--type", "block", "--claim-type", "observation"]) == 2
    assert main(["query", "--limit", "0"]) == 2
    assert main(["query", "--status", "bogus"]) == 2


def test_query_follows_card_relations(tmp_path, monkeypatch, capsys):
    root = _init(tmp_path, monkeypatch)
    conn = _db(root)
    try:
        _seed_card(conn, "C-ROOT", title="Root", body="root card")
        _seed_card(conn, "C-RELATED", title="Related", body="related card")
        conn.execute(
            "INSERT INTO card_relations "
            "(id, from_card_id, to_card_id, relation_type, claim_type, source) "
            "VALUES ('REL-1', 'C-ROOT', 'C-RELATED', 'triggered_by', 'causal', 'agent')"
        )
        conn.commit()
    finally:
        conn.close()

    out = _run(capsys, "query", "--type", "card", "--grep", "root", "--follow")
    assert "C-ROOT" in out
    assert "C-RELATED" in out
    assert "via triggered_by from C-ROOT" in out
    assert "1 results" in out


def test_query_follows_claim_relations(tmp_path, monkeypatch, capsys):
    root = _init(tmp_path, monkeypatch)
    conn = _db(root)
    try:
        _seed_claim(conn, "OBS-D-2026-01-05-01", body="root claim")
        _seed_claim(conn, "OBS-D-2026-01-05-02", body="related claim")
        conn.execute(
            "INSERT INTO claim_relations "
            "(id, from_claim_id, to_claim_id, relation_type) "
            "VALUES ('CREL-1', 'OBS-D-2026-01-05-02', "
            "'OBS-D-2026-01-05-01', 'derived_from')"
        )
        conn.commit()
    finally:
        conn.close()

    out = _run(capsys, "query", "--type", "claim", "--grep", "root", "--follow")
    assert "OBS-D-2026-01-05-01" in out
    assert "OBS-D-2026-01-05-02" in out
    assert "via derived_from from OBS-D-2026-01-05-01" in out
    assert "1 results" in out


def test_query_follows_report_cards_and_reports(tmp_path, monkeypatch, capsys):
    root = _init(tmp_path, monkeypatch)
    conn = _db(root)
    try:
        _seed_report(conn, "RPT-ROOT", title="root report")
        _seed_report(
            conn, "RPT-CHILD", title="child report",
            period_start="2026-01-06", period_end="2026-01-07",
        )
        _seed_card(conn, "C-REPORT-CARD", title="Report card", body="report card")
        conn.execute(
            "INSERT INTO report_cards (report_id, card_id, role) "
            "VALUES ('RPT-ROOT', 'C-REPORT-CARD', 'reports_event')"
        )
        conn.execute(
            "INSERT INTO report_relations "
            "(id, from_report_id, to_report_id, relation_type) "
            "VALUES ('RREL-1', 'RPT-ROOT', 'RPT-CHILD', 'covers')"
        )
        conn.commit()
    finally:
        conn.close()

    out = _run(capsys, "query", "--type", "report", "--grep", "root", "--follow")
    assert "RPT-ROOT" in out
    assert "C-REPORT-CARD" in out
    assert "RPT-CHILD" in out
    assert "via report_card:reports_event from RPT-ROOT" in out
    assert "via covers from RPT-ROOT" in out
    assert "1 results" in out


def test_query_follow_rejects_blocks(tmp_path, monkeypatch, capsys):
    _init(tmp_path, monkeypatch)
    assert main(["query", "--type", "block", "--follow"]) == 2
    assert "follow" in capsys.readouterr().err.lower()


def test_query_sort_relevance_orders_causal_before_temporal(tmp_path, monkeypatch, capsys):
    root = _init(tmp_path, monkeypatch)
    conn = _db(root)
    try:
        _seed_card(conn, "C-SORT-ROOT", title="Root", body="sort root")
        _seed_card(conn, "C-SORT-TRIGGER", title="Triggered", body="triggered")
        _seed_card(conn, "C-SORT-PRECEDE", title="Precede", body="precede")
        conn.execute(
            "INSERT INTO card_relations "
            "(id, from_card_id, to_card_id, relation_type, claim_type, source) "
            "VALUES ('REL-SORT-T', 'C-SORT-ROOT', 'C-SORT-TRIGGER', "
            "'triggered_by', 'causal', 'agent')"
        )
        conn.execute(
            "INSERT INTO card_relations "
            "(id, from_card_id, to_card_id, relation_type, claim_type, source) "
            "VALUES ('REL-SORT-P', 'C-SORT-ROOT', 'C-SORT-PRECEDE', "
            "'precedes', 'temporal', 'agent')"
        )
        conn.commit()
    finally:
        conn.close()

    out = _run(
        capsys,
        "query", "--type", "card", "--grep", "sort root",
        "--follow", "--sort", "relevance",
    )
    root_at = out.index("C-SORT-ROOT")
    trigger_at = out.index("C-SORT-TRIGGER")
    precede_at = out.index("C-SORT-PRECEDE")
    assert root_at < trigger_at < precede_at
    assert "1 results" in out


def test_query_sort_invalid_exits_two(tmp_path, monkeypatch, capsys):
    _init(tmp_path, monkeypatch)
    assert main(["query", "--sort", "bogus"]) == 2
    assert "sort" in capsys.readouterr().err.lower()
