"""Legacy JSONL import: mapping, idempotency, rollback and CLI behaviour."""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from stratum.cli import main
from stratum.db import open_db
from stratum.legacy import legacy_block_identity

FIXTURE_INDEX = (
    Path(__file__).resolve().parents[1] / "fixtures" / "legacy" / "index"
)

DATA_TABLES = (
    "documents",
    "blocks",
    "cards",
    "concepts",
    "card_blocks",
    "card_concepts",
    "card_relations",
    "reports",
    "report_blocks",
    "report_cards",
    "report_relations",
    "legacy_id_map",
)

EXPECTED_COUNTS = {
    "documents": 2,
    "blocks": 3,
    "cards": 2,
    "concepts": 2,
    "card_blocks": 2,
    "card_concepts": 2,
    "card_relations": 1,
    "reports": 1,
    "report_blocks": 1,
    "report_cards": 2,
    "report_relations": 1,
    "legacy_id_map": 8,
}


def _init_workspace(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "home"
    root = tmp_path / "notes"
    root.mkdir(parents=True)
    monkeypatch.setenv("STRATUM_HOME", str(home))
    monkeypatch.chdir(tmp_path)
    assert main(["init", "--root", str(root), "--name", "legacy-demo"]) == 0
    return root


def _db(root: Path) -> sqlite3.Connection:
    return open_db(root / ".stratum" / "stratum.db")


def _count(conn: sqlite3.Connection, table: str) -> int:
    return int(
        conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    )


def _data_counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {table: _count(conn, table) for table in DATA_TABLES}


def _copy_index(tmp_path: Path) -> Path:
    target = tmp_path / "legacy-index"
    shutil.copytree(FIXTURE_INDEX, target)
    return target


def _mutate_jsonl(path: Path, mutate) -> None:
    rows = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    mutate(rows)
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )


def _fixture_rows(name: str) -> list[dict]:
    return [
        json.loads(line)
        for line in (FIXTURE_INDEX / name).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_import_counts_legacy_ids_and_relations(tmp_path, monkeypatch, capsys):
    root = _init_workspace(tmp_path, monkeypatch)
    assert (
        main(["migrate-legacy", "--index", str(FIXTURE_INDEX)])
        == 0
    )
    out = capsys.readouterr().out

    assert "Imported legacy index:" in out
    for name, expected in EXPECTED_COUNTS.items():
        assert f"{name}: {expected}" in out
    assert not out.lstrip().startswith("{")

    conn = _db(root)
    try:
        assert _data_counts(conn) == EXPECTED_COUNTS

        documents = {
            row["id"]: row
            for row in conn.execute(
                "SELECT id, title, retrieval_type, contract_version "
                "FROM documents"
            )
        }
        assert set(documents) == {
            "data/daily/2026-01-01/理解-2026-01-01.md",
            "data/reports/回顾-2026-01-01.md",
        }
        assert all(
            row["retrieval_type"] == "legacy"
            and row["contract_version"] == 0
            for row in documents.values()
        )
        assert (
            documents["data/reports/回顾-2026-01-01.md"]["title"]
            == "合成日报"
        )
        assert (
            documents["data/daily/2026-01-01/理解-2026-01-01.md"]["title"]
            == "理解-2026-01-01.md"
        )

        blocks = {
            row["legacy_block_id"]: row
            for row in conn.execute(
                "SELECT id, legacy_block_id, document_id, anchor, "
                "selector_type, retrieval_type, occurrence, line_start, "
                "line_end, status, snapshot_text, snapshot_sha256, "
                "metadata_json FROM blocks"
            )
        }
        fixture_blocks = {row["block_id"]: row for row in _fixture_rows("blocks.jsonl")}
        assert set(blocks) == set(fixture_blocks)
        for legacy_id, fixture in fixture_blocks.items():
            block = blocks[legacy_id]
            assert block["id"] == legacy_block_identity(
                fixture["file"], legacy_id
            )
            assert block["id"].startswith("BLK-")
            assert block["document_id"] == fixture["file"]
            assert block["anchor"] == legacy_id
            assert block["selector_type"] == "legacy"
            assert block["retrieval_type"] == "legacy"
            assert block["occurrence"] == 0
            assert block["line_start"] == fixture["line_start"]
            assert block["line_end"] == fixture["line_end"]
            assert block["status"] == fixture["status"]
            assert block["snapshot_text"] == fixture["text"]
            assert block["snapshot_sha256"] == fixture["text_sha256"]
            metadata = json.loads(block["metadata_json"])
            assert metadata["source_kind"] == fixture["source_kind"]
            assert metadata["date"] == fixture["date"]
            assert metadata["heading"] == fixture["heading"]
            assert metadata["source_type"] == fixture["source_type"]

        cards = {
            row["id"]: row
            for row in conn.execute(
                "SELECT id, card_type, title, body, status, confidence, "
                "period_start, period_end, legacy_card_id, metadata_json "
                "FROM cards"
            )
        }
        assert set(cards) == {"C-20260101-0001", "C-20260101-0002"}
        assert cards["C-20260101-0001"]["card_type"] == "event"
        assert cards["C-20260101-0001"]["legacy_card_id"] == "C-20260101-0001"
        assert cards["C-20260101-0001"]["body"] == "上午完成合成任务。"
        assert (
            cards["C-20260101-0001"]["period_start"],
            cards["C-20260101-0001"]["period_end"],
        ) == ("2026-01-01", "2026-01-02")
        assert (
            cards["C-20260101-0002"]["period_start"],
            cards["C-20260101-0002"]["period_end"],
        ) == ("2026-01-01", "2026-01-03")
        metadata = json.loads(cards["C-20260101-0002"]["metadata_json"])
        assert metadata["slots"]["sessions"] == 2
        assert metadata["version"] == 2
        assert metadata["time"] is None

        concepts = {
            row["id"]: row
            for row in conn.execute(
                "SELECT id, path, name, parent_id, is_leaf, description "
                "FROM concepts"
            )
        }
        assert concepts["EVENT"]["path"] == "EVENT"
        assert concepts["EVENT"]["parent_id"] is None
        assert concepts["EVENT"]["is_leaf"] == 0
        assert concepts["FINDING"]["parent_id"] == "EVENT"
        assert concepts["FINDING"]["is_leaf"] == 1
        assert concepts["FINDING"]["description"] == "从事件推导的结论"

        card_blocks = conn.execute(
            "SELECT cb.card_id, b.legacy_block_id, cb.quote_sha256, cb.role "
            "FROM card_blocks cb JOIN blocks b ON b.id = cb.block_id "
            "ORDER BY cb.card_id"
        ).fetchall()
        assert [
            (row["card_id"], row["legacy_block_id"])
            for row in card_blocks
        ] == [
            ("C-20260101-0001", "L-0001"),
            ("C-20260101-0002", "R-0001"),
        ]
        assert card_blocks[1]["role"] == "corroboration"
        assert card_blocks[1]["quote_sha256"] == fixture_blocks["R-0001"]["text_sha256"]

        report = conn.execute(
            "SELECT id, report_type, title, period_start, period_end, "
            "document_id, status, contract_version, author, base_commit "
            "FROM reports"
        ).fetchone()
        assert report["id"] == "D-20260101"
        assert report["report_type"] == "daily"
        assert report["title"] == "合成日报"
        assert report["period_start"] == "2026-01-01"
        assert report["period_end"] == "2026-01-01"
        assert report["document_id"] == "data/reports/回顾-2026-01-01.md"
        assert report["status"] == "active"
        assert report["contract_version"] == 0
        assert report["author"] is None
        assert report["base_commit"] is None

        report_blocks = conn.execute(
            "SELECT rb.report_id, b.legacy_block_id, rb.role "
            "FROM report_blocks rb JOIN blocks b ON b.id = rb.block_id"
        ).fetchall()
        assert [
            (row["report_id"], row["legacy_block_id"], row["role"])
            for row in report_blocks
        ] == [("D-20260101", "R-0001", "evidence")]

        report_cards = conn.execute(
            "SELECT report_id, card_id, role FROM report_cards "
            "ORDER BY card_id"
        ).fetchall()
        assert [
            (row["report_id"], row["card_id"], row["role"])
            for row in report_cards
        ] == [
            ("D-20260101", "C-20260101-0001", "reference"),
            ("D-20260101", "C-20260101-0002", "reference"),
        ]

        report_relations = conn.execute(
            "SELECT from_report_id, to_report_id, relation_type "
            "FROM report_relations"
        ).fetchall()
        assert [
            (
                row["from_report_id"],
                row["to_report_id"],
                row["relation_type"],
            )
            for row in report_relations
        ] == [("D-20260101", "D-20260101", "covers")]

        entity_counts = {
            row["entity_type"]: row["count"]
            for row in conn.execute(
                "SELECT entity_type, COUNT(*) AS count FROM legacy_id_map "
                "GROUP BY entity_type"
            )
        }
        assert entity_counts == {
            "block": 3,
            "card": 2,
            "concept": 2,
            "report": 1,
        }
        block_map = conn.execute(
            "SELECT new_id FROM legacy_id_map "
            "WHERE entity_type = 'block' AND legacy_id = 'L-0001'"
        ).fetchone()
        assert block_map["new_id"] == blocks["L-0001"]["id"]

        run = conn.execute(
            "SELECT status, stats_json, error FROM import_runs"
        ).fetchone()
        assert run["status"] == "completed"
        assert run["error"] is None
        stats = json.loads(run["stats_json"])
        assert stats["counts"] == EXPECTED_COUNTS
        assert len(stats["index_sha256"]) == 64
    finally:
        conn.close()


def test_report_card_relations_preserve_relation_roles(
    tmp_path, monkeypatch, capsys
):
    root = _init_workspace(tmp_path, monkeypatch)
    index = _copy_index(tmp_path)
    _mutate_jsonl(
        index / "cards.jsonl",
        lambda rows: rows.append(
            {
                "card_id": "C-20260101-0003",
                "kind": "event",
                "title": "合成关联事件",
                "statement": "合成关联材料。",
                "date": "2026-01-01",
                "confidence": "high",
                "status": "active",
                "version": 1,
            }
        ),
    )
    _mutate_jsonl(
        index / "report_relations.jsonl",
        lambda rows: rows.append(
            {
                "from_report": "D-20260101",
                "relation": "reports_event",
                "to_card": "C-20260101-0003",
            }
        ),
    )

    assert main(["migrate-legacy", "--index", str(index)]) == 0
    capsys.readouterr()

    conn = _db(root)
    try:
        report_cards = conn.execute(
            "SELECT report_id, card_id, role FROM report_cards "
            "ORDER BY card_id"
        ).fetchall()
        assert [
            (row["report_id"], row["card_id"], row["role"])
            for row in report_cards
        ] == [
            ("D-20260101", "C-20260101-0001", "reference"),
            ("D-20260101", "C-20260101-0002", "reference"),
            ("D-20260101", "C-20260101-0003", "reports_event"),
        ]

        report_relations = conn.execute(
            "SELECT from_report_id, to_report_id, relation_type "
            "FROM report_relations"
        ).fetchall()
        assert [
            (
                row["from_report_id"],
                row["to_report_id"],
                row["relation_type"],
            )
            for row in report_relations
        ] == [("D-20260101", "D-20260101", "covers")]
        report_ids = {
            row["id"] for row in conn.execute("SELECT id FROM reports")
        }
        assert {
            row["from_report_id"] for row in report_relations
        } <= report_ids
        assert {
            row["to_report_id"] for row in report_relations
        } <= report_ids
    finally:
        conn.close()


@pytest.mark.parametrize("reports_event_first", [False, True])
def test_reports_event_wins_over_observes_for_same_card(
    tmp_path, monkeypatch, capsys, reports_event_first
):
    root = _init_workspace(tmp_path, monkeypatch)
    index = _copy_index(tmp_path)

    def add_relations(rows):
        rows[:] = [
            row
            for row in rows
            if row.get("to_card") != "C-20260101-0001"
        ]
        observes = {
            "from_report": "D-20260101",
            "relation": "observes",
            "note": "synthetic card link",
            "to_card": "C-20260101-0001",
        }
        reports_event = {
            "from_report": "D-20260101",
            "relation": "reports_event",
            "note": "synthetic event link",
            "to_card": "C-20260101-0001",
        }
        if reports_event_first:
            rows.extend([reports_event, observes])
        else:
            rows.extend([observes, reports_event])

    _mutate_jsonl(index / "report_relations.jsonl", add_relations)

    assert main(["migrate-legacy", "--index", str(index)]) == 0
    capsys.readouterr()

    conn = _db(root)
    try:
        roles = {
            row["card_id"]: row["role"]
            for row in conn.execute(
                "SELECT card_id, role FROM report_cards "
                "WHERE report_id = 'D-20260101'"
            )
        }
        assert roles == {
            "C-20260101-0001": "reports_event",
            "C-20260101-0002": "reference",
        }
        assert _count(conn, "report_cards") == 2
        run = conn.execute("SELECT stats_json FROM import_runs").fetchone()
        assert json.loads(run["stats_json"])["counts"]["report_cards"] == 2
    finally:
        conn.close()


def test_observes_only_keeps_reference_role(tmp_path, monkeypatch, capsys):
    root = _init_workspace(tmp_path, monkeypatch)
    assert main(["migrate-legacy", "--index", str(FIXTURE_INDEX)]) == 0
    capsys.readouterr()

    conn = _db(root)
    try:
        roles = {
            row["card_id"]: row["role"]
            for row in conn.execute(
                "SELECT card_id, role FROM report_cards"
            )
        }
        assert roles == {
            "C-20260101-0001": "reference",
            "C-20260101-0002": "reference",
        }
        assert _count(conn, "report_cards") == 2
    finally:
        conn.close()


def test_reimport_same_index_is_unchanged(tmp_path, monkeypatch, capsys):
    root = _init_workspace(tmp_path, monkeypatch)
    assert main(["migrate-legacy", "--index", str(FIXTURE_INDEX)]) == 0
    first = capsys.readouterr().out
    assert "blocks: 3" in first

    assert main(["migrate-legacy", "--index", str(FIXTURE_INDEX)]) == 0
    second = capsys.readouterr().out
    assert second.strip() == "unchanged"

    conn = _db(root)
    try:
        assert _data_counts(conn) == EXPECTED_COUNTS
        assert _count(conn, "import_runs") == 1
    finally:
        conn.close()


def test_changed_index_after_completion_is_refused(tmp_path, monkeypatch, capsys):
    root = _init_workspace(tmp_path, monkeypatch)
    index = _copy_index(tmp_path)
    assert main(["migrate-legacy", "--index", str(index)]) == 0
    capsys.readouterr()

    _mutate_jsonl(
        index / "cards.jsonl",
        lambda rows: rows[0].update({"title": "改名事件"}),
    )

    assert main(["migrate-legacy", "--index", str(index)]) == 1
    err = capsys.readouterr().err
    assert "LEGACY_INDEX_CHANGED" in err

    conn = _db(root)
    try:
        assert _data_counts(conn) == EXPECTED_COUNTS
        assert _count(conn, "import_runs") == 1
    finally:
        conn.close()


def test_bad_block_hash_rolls_back_and_records_failed_run(
    tmp_path, monkeypatch, capsys
):
    root = _init_workspace(tmp_path, monkeypatch)
    index = _copy_index(tmp_path)
    _mutate_jsonl(
        index / "blocks.jsonl",
        lambda rows: rows[0].update({"text_sha256": "0" * 64}),
    )

    assert main(["migrate-legacy", "--index", str(index)]) == 1
    err = capsys.readouterr().err
    assert "LEGACY_HASH_MISMATCH" in err

    conn = _db(root)
    try:
        assert _data_counts(conn) == {table: 0 for table in DATA_TABLES}
        run = conn.execute(
            "SELECT status, error FROM import_runs"
        ).fetchone()
        assert run["status"] == "failed"
        assert "LEGACY_HASH_MISMATCH" in run["error"]
    finally:
        conn.close()


def test_retry_after_failed_import_succeeds(tmp_path, monkeypatch, capsys):
    root = _init_workspace(tmp_path, monkeypatch)
    index = _copy_index(tmp_path)
    blocks_path = index / "blocks.jsonl"
    original = blocks_path.read_text(encoding="utf-8")
    _mutate_jsonl(
        blocks_path,
        lambda rows: rows[0].update({"text_sha256": "0" * 64}),
    )

    assert main(["migrate-legacy", "--index", str(index)]) == 1
    capsys.readouterr()
    blocks_path.write_text(original, encoding="utf-8")

    assert main(["migrate-legacy", "--index", str(index)]) == 0
    assert "blocks: 3" in capsys.readouterr().out

    conn = _db(root)
    try:
        assert _data_counts(conn) == EXPECTED_COUNTS
        statuses = [
            row["status"]
            for row in conn.execute(
                "SELECT status FROM import_runs ORDER BY rowid"
            )
        ]
        assert statuses == ["failed", "completed"]
    finally:
        conn.close()


def test_bad_quote_hash_rolls_back(tmp_path, monkeypatch, capsys):
    root = _init_workspace(tmp_path, monkeypatch)
    index = _copy_index(tmp_path)
    _mutate_jsonl(
        index / "card_blocks.jsonl",
        lambda rows: rows[1].update({"quote_sha": "0" * 64}),
    )

    assert main(["migrate-legacy", "--index", str(index)]) == 1
    assert "LEGACY_HASH_MISMATCH" in capsys.readouterr().err

    conn = _db(root)
    try:
        assert _data_counts(conn) == {table: 0 for table in DATA_TABLES}
    finally:
        conn.close()


def test_invalid_jsonl_rolls_back(tmp_path, monkeypatch, capsys):
    root = _init_workspace(tmp_path, monkeypatch)
    index = _copy_index(tmp_path)
    with (index / "cards.jsonl").open("a", encoding="utf-8") as handle:
        handle.write("{not json\n")

    assert main(["migrate-legacy", "--index", str(index)]) == 1
    err = capsys.readouterr().err
    assert "LEGACY_JSON_INVALID" in err
    assert "cards.jsonl" in err

    conn = _db(root)
    try:
        assert _data_counts(conn) == {table: 0 for table in DATA_TABLES}
    finally:
        conn.close()


def test_dangling_reference_rolls_back(tmp_path, monkeypatch, capsys):
    root = _init_workspace(tmp_path, monkeypatch)
    index = _copy_index(tmp_path)
    _mutate_jsonl(
        index / "card_relations.jsonl",
        lambda rows: rows[0].update({"to_card": "C-99999999-9999"}),
    )

    assert main(["migrate-legacy", "--index", str(index)]) == 1
    assert "LEGACY_REFERENCE_MISSING" in capsys.readouterr().err

    conn = _db(root)
    try:
        assert _data_counts(conn) == {table: 0 for table in DATA_TABLES}
    finally:
        conn.close()


def test_bad_meta_schema_version_is_rejected(tmp_path, monkeypatch, capsys):
    root = _init_workspace(tmp_path, monkeypatch)
    index = _copy_index(tmp_path)
    meta = json.loads((index / "index.meta.json").read_text(encoding="utf-8"))
    meta["schema_version"] = 2
    (index / "index.meta.json").write_text(
        json.dumps(meta), encoding="utf-8"
    )

    assert main(["migrate-legacy", "--index", str(index)]) == 1
    assert "LEGACY_META_INVALID" in capsys.readouterr().err

    conn = _db(root)
    try:
        assert _data_counts(conn) == {table: 0 for table in DATA_TABLES}
        run = conn.execute(
            "SELECT status, error FROM import_runs"
        ).fetchone()
        assert run["status"] == "failed"
        assert "LEGACY_META_INVALID" in run["error"]
    finally:
        conn.close()


def test_missing_index_file_is_rejected(tmp_path, monkeypatch, capsys):
    root = _init_workspace(tmp_path, monkeypatch)
    index = _copy_index(tmp_path)
    (index / "reports.jsonl").unlink()

    assert main(["migrate-legacy", "--index", str(index)]) == 1
    err = capsys.readouterr().err
    assert "LEGACY_FILE_MISSING" in err
    assert "reports.jsonl" in err

    conn = _db(root)
    try:
        assert _data_counts(conn) == {table: 0 for table in DATA_TABLES}
        run = conn.execute(
            "SELECT status FROM import_runs"
        ).fetchone()
        assert run["status"] == "failed"
    finally:
        conn.close()


def test_missing_index_directory_exits_one(tmp_path, monkeypatch, capsys):
    root = _init_workspace(tmp_path, monkeypatch)

    assert (
        main(["migrate-legacy", "--index", str(tmp_path / "does-not-exist")])
        == 1
    )
    assert "LEGACY_INDEX_MISSING" in capsys.readouterr().err

    conn = _db(root)
    try:
        run = conn.execute(
            "SELECT status FROM import_runs"
        ).fetchone()
        assert run["status"] == "failed"
    finally:
        conn.close()


def test_cli_requires_index(capsys):
    assert main(["migrate-legacy"]) == 2
    assert "--index" in capsys.readouterr().err


def test_workspace_flag_after_subcommand(tmp_path, monkeypatch, capsys):
    root = _init_workspace(tmp_path, monkeypatch)

    assert (
        main(
            [
                "migrate-legacy",
                "--index",
                str(FIXTURE_INDEX),
                "--workspace",
                str(root),
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "blocks: 3" in out

    conn = _db(root)
    try:
        assert _count(conn, "blocks") == 3
    finally:
        conn.close()
