"""Source ingest: persistence, idempotency, lineage and transactions."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from stratum.cli import main
from stratum.db import open_db
from stratum.ingest_source import ingest_source_file


def _init(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "home"
    root = tmp_path / "notes"
    root.mkdir(parents=True)
    monkeypatch.setenv("STRATUM_HOME", str(home))
    monkeypatch.chdir(tmp_path)
    assert main(["init", "--root", str(root), "--name", "demo"]) == 0
    return root


def _db(root: Path) -> sqlite3.Connection:
    return open_db(root / ".stratum" / "stratum.db")


def _write(root: Path, relative: str, text: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def _counts(conn: sqlite3.Connection) -> dict[str, int]:
    tables = (
        "documents",
        "document_versions",
        "blobs",
        "profiles",
        "segmentation_runs",
        "blocks",
        "block_lineage",
    )
    return {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in tables
    }


SOURCE = """# Journal

First paragraph.

## Morning

Started early.

## Evening

Wrapped up.
"""


def test_source_ingest_persists_document_version_blocks_and_profile(
    tmp_path, monkeypatch, capsys
):
    root = _init(tmp_path, monkeypatch)
    path = _write(root, "journal/2026-01-05.md", SOURCE)

    assert main(["ingest", "source", str(path)]) == 0
    out = capsys.readouterr().out
    assert "Ingested source: journal/2026-01-05.md" in out
    assert "Blocks: created 3" in out
    assert not out.lstrip().startswith("{")

    conn = _db(root)
    try:
        counts = _counts(conn)
        assert counts["documents"] == 1
        assert counts["document_versions"] == 1
        assert counts["blobs"] == 1
        assert counts["profiles"] == 1
        assert counts["segmentation_runs"] == 1
        assert counts["blocks"] == 3

        document = conn.execute("SELECT * FROM documents").fetchone()
        assert document["path"] == "journal/2026-01-05.md"
        assert document["retrieval_type"] == "structured"
        assert document["contract_version"] == 0
        assert document["source_sha256"]

        blocks = conn.execute(
            "SELECT id, profile_id, status, snapshot_sha256 FROM blocks"
        ).fetchall()
        profile_id = conn.execute("SELECT id FROM profiles").fetchone()[0]
        assert all(block["profile_id"] == profile_id for block in blocks)
        assert all(block["status"] == "active" for block in blocks)
        assert all(block["snapshot_sha256"] for block in blocks)
    finally:
        conn.close()


def test_source_ingest_is_idempotent(tmp_path, monkeypatch, capsys):
    root = _init(tmp_path, monkeypatch)
    path = _write(root, "journal/2026-01-05.md", SOURCE)

    assert main(["ingest", "source", str(path)]) == 0
    capsys.readouterr()
    assert main(["ingest", "source", str(path)]) == 0
    out = capsys.readouterr().out
    assert out.strip() == "unchanged"

    conn = _db(root)
    try:
        counts = _counts(conn)
        assert counts["document_versions"] == 1
        assert counts["segmentation_runs"] == 1
        assert counts["blocks"] == 3
    finally:
        conn.close()


def test_source_change_increments_version_and_marks_changed_snapshot_stale(
    tmp_path, monkeypatch, capsys
):
    root = _init(tmp_path, monkeypatch)
    path = _write(root, "journal/2026-01-05.md", SOURCE)

    conn = _db(root)
    try:
        assert main(["ingest", "source", str(path)]) == 0
        capsys.readouterr()
        before = {
            row["id"]: row["status"]
            for row in conn.execute("SELECT id, status FROM blocks")
        }
    finally:
        conn.close()

    changed = SOURCE.replace("Started early.", "Started very early.")
    path.write_text(changed, encoding="utf-8", newline="\n")

    assert main(["ingest", "source", str(path)]) == 0
    out = capsys.readouterr().out
    assert "Version: 2" in out
    assert "updated 1" in out

    conn = _db(root)
    try:
        assert _counts(conn)["document_versions"] == 2
        rows = {
            row["id"]: row["status"]
            for row in conn.execute("SELECT id, status FROM blocks")
        }
        stale = [block_id for block_id, status in rows.items() if status == "stale"]
        assert len(stale) == 1
        assert stale[0] in before
        assert rows[stale[0]] == "stale"
        assert sum(1 for status in rows.values() if status == "active") == 2
    finally:
        conn.close()


def test_source_delete_marks_missing_without_deleting_rows(
    tmp_path, monkeypatch, capsys
):
    root = _init(tmp_path, monkeypatch)
    path = _write(root, "journal/2026-01-05.md", SOURCE)
    assert main(["ingest", "source", str(path)]) == 0
    capsys.readouterr()

    path.write_text(SOURCE.replace("## Evening\n\nWrapped up.\n", ""), encoding="utf-8")
    assert main(["ingest", "source", str(path)]) == 0
    out = capsys.readouterr().out
    assert "missing 1" in out

    conn = _db(root)
    try:
        statuses = [
            row["status"] for row in conn.execute("SELECT status FROM blocks")
        ]
        assert statuses.count("missing") == 1
        assert statuses.count("active") == 2
        assert _counts(conn)["blocks"] == 3
    finally:
        conn.close()


def test_source_rename_creates_lineage_and_old_block_missing(
    tmp_path, monkeypatch, capsys
):
    root = _init(tmp_path, monkeypatch)
    profiles = root / ".stratum" / "profiles" / "custom.toml"
    profiles.write_text(
        '[profiles.body]\nselector = "heading"\ninclude_heading = false\n',
        encoding="utf-8",
    )
    text = "# Keep\n\nBody.\n\n## Old Name\n\nSame body.\n"
    path = _write(root, "journal/rename.md", text)
    assert main(["ingest", "source", str(path), "--profile", "body"]) == 0
    capsys.readouterr()

    conn = _db(root)
    try:
        old_id = conn.execute(
            "SELECT id FROM blocks WHERE anchor = 'Old Name'"
        ).fetchone()[0]
    finally:
        conn.close()

    path.write_text(text.replace("Old Name", "New Name"), encoding="utf-8")
    assert main(["ingest", "source", str(path), "--profile", "body"]) == 0

    conn = _db(root)
    try:
        edge = conn.execute(
            "SELECT * FROM block_lineage WHERE old_block_id = ?", (old_id,)
        ).fetchone()
        assert edge is not None
        assert edge["reason"] == "rename"
        old_status = conn.execute(
            "SELECT status FROM blocks WHERE id = ?", (old_id,)
        ).fetchone()[0]
        new_status = conn.execute(
            "SELECT status FROM blocks WHERE id = ?", (edge["new_block_id"],)
        ).fetchone()[0]
        assert old_status == "missing"
        assert new_status == "active"
    finally:
        conn.close()


def test_source_split_creates_lineage(tmp_path, monkeypatch, capsys):
    root = _init(tmp_path, monkeypatch)
    profiles = root / ".stratum" / "profiles" / "custom.toml"
    profiles.write_text(
        '[profiles.paragraphs]\nselector = "paragraph"\nmin_chars = 0\n',
        encoding="utf-8",
    )
    path = _write(root, "journal/split.md", "Alpha. Beta. Gamma.\n")
    assert main(["ingest", "source", str(path), "--profile", "paragraphs"]) == 0
    capsys.readouterr()

    path.write_text("Alpha.\n\nBeta.\n\nGamma.\n", encoding="utf-8")
    assert main(["ingest", "source", str(path), "--profile", "paragraphs"]) == 0
    out = capsys.readouterr().out
    assert "lineage 3" in out

    conn = _db(root)
    try:
        reasons = [
            row["reason"]
            for row in conn.execute("SELECT reason FROM block_lineage")
        ]
        assert reasons == ["split", "split", "split"]
    finally:
        conn.close()


def test_source_transaction_rolls_back_on_failure(tmp_path, monkeypatch):
    root = _init(tmp_path, monkeypatch)
    path = _write(root, "journal/fail.md", SOURCE)

    import stratum.ingest_source as ingest_source

    def boom(*args, **kwargs):
        raise RuntimeError("injected failure")

    monkeypatch.setattr(ingest_source, "_apply_resegmentation", boom)
    with pytest.raises(RuntimeError):
        ingest_source.ingest_source_file(path)

    conn = _db(root)
    try:
        counts = _counts(conn)
        assert counts["documents"] == 0
        assert counts["document_versions"] == 0
        assert counts["blocks"] == 0
        assert counts["segmentation_runs"] == 0
    finally:
        conn.close()


def test_source_outside_workspace_exits_two(tmp_path, monkeypatch, capsys):
    root = _init(tmp_path, monkeypatch)
    outside = tmp_path / "outside.md"
    outside.write_text("# Outside\n\nText.\n", encoding="utf-8")

    assert main(["ingest", "source", str(outside)]) == 2
    err = capsys.readouterr().err
    assert "outside workspace root" in err
    conn = _db(root)
    try:
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
    finally:
        conn.close()


def test_source_profile_override_changes_retrieval_type(
    tmp_path, monkeypatch, capsys
):
    root = _init(tmp_path, monkeypatch)
    path = _write(root, "journal/override.md", SOURCE)

    assert main(["ingest", "source", str(path), "--profile", "whole-document"]) == 0
    conn = _db(root)
    try:
        row = conn.execute("SELECT * FROM blocks").fetchone()
        assert row["retrieval_type"] == "prose"
        assert conn.execute("SELECT COUNT(*) FROM blocks").fetchone()[0] == 1
    finally:
        conn.close()


def test_crlf_and_bom_are_hash_equivalent(tmp_path, monkeypatch, capsys):
    root = _init(tmp_path, monkeypatch)
    path = _write(root, "journal/encoding.md", "# Heading\n\nBody.\n")
    assert main(["ingest", "source", str(path)]) == 0
    capsys.readouterr()

    path.write_bytes(b"\xef\xbb\xbf# Heading\r\n\r\nBody.\r\n")
    assert main(["ingest", "source", str(path)]) == 0
    assert capsys.readouterr().out.strip() == "unchanged"
