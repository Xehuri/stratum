"""Cross-platform path, encoding and identity invariants.

These tests run on both Linux and Windows CI. They avoid symlinks, git and
case-only file names so the same assertions are valid on either platform.
"""

from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path

from stratum.db import open_db
from stratum.markdown import normalize_relative_path, normalize_text, scan_document
from stratum.query import query_items
from stratum.segmentation.identity import compute_block_id, document_id
from stratum.workspace import init_workspace

BLOCK_ID_RE = re.compile(r"^BLK-[0-9a-f]{16}$")
DOCUMENT_ID_RE = re.compile(r"^[A-Za-z0-9_./-]+$")


def _ingest(
    tmp_path: Path, monkeypatch, relative: str, payload: bytes
) -> tuple[Path, sqlite3.Connection]:
    home = tmp_path / "home"
    root = tmp_path / "workspace"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("STRATUM_HOME", str(home))
    monkeypatch.chdir(tmp_path)
    result = init_workspace(root, name="cross-platform", home=home)
    source = root / relative
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(payload)

    from stratum.ingest_source import ingest_source_file

    ingest_source_file(source, workspace=result.workspace.workspace_id)
    return root, open_db(result.workspace.db_path)


def test_normalize_relative_path_accepts_windows_and_posix_spellings():
    expected = "notes/entry.md"
    assert normalize_relative_path("notes\\entry.md") == expected
    assert normalize_relative_path("./notes//entry.md") == expected
    assert normalize_relative_path("C:\\notes\\entry.md") == expected
    assert normalize_relative_path("/notes/entry.md") == expected
    assert normalize_relative_path(Path("notes") / "entry.md") == expected
    assert normalize_relative_path(".") == ""


def test_normalize_text_crlf_cr_and_bom_are_equivalent():
    lf = "# Title\n\n## Section\n\nbody\n"
    crlf = lf.replace("\n", "\r\n")
    cr = lf.replace("\n", "\r")
    assert normalize_text(lf) == lf
    assert normalize_text(crlf) == lf
    assert normalize_text(cr) == lf
    assert normalize_text(b"\xef\xbb\xbf" + lf.encode("utf-8")) == lf
    assert normalize_text((lf.encode("utf-8"))) == lf


def test_crlf_bom_and_cjk_ingest_roundtrip(tmp_path, monkeypatch):
    text = "# Title\r\n\r\n## Section\r\n\r\nCJK 潮间带 fixture.\r\n"
    payload = b"\xef\xbb\xbf" + text.encode("utf-8")
    root, conn = _ingest(tmp_path, monkeypatch, "notes/cjk.md", payload)
    try:
        rows = conn.execute(
            "SELECT snapshot_text, retrieval_type, id FROM blocks "
            "ORDER BY line_start"
        ).fetchall()
        assert rows
        assert all("\r" not in row["snapshot_text"] for row in rows)
        assert not any(row["snapshot_text"].startswith("\ufeff") for row in rows)
        assert any("潮间带" in row["snapshot_text"] for row in rows)
        cjk_rows = query_items(conn, entity_type="block", grep="潮间带")
        assert len(cjk_rows) == 1
        assert cjk_rows[0].document_id == "notes/cjk.md"
        assert BLOCK_ID_RE.fullmatch(cjk_rows[0].entity_id)
    finally:
        conn.close()

    documented = sorted((root / "notes").iterdir())
    assert [item.name for item in documented] == ["cjk.md"]
    assert not (root / ".stratum").is_symlink()
    assert not (root / ".stratum" / "stratum.db").is_symlink()


def test_ingest_does_not_call_os_symlink(tmp_path, monkeypatch):
    def fail_symlink(*args, **kwargs):  # pragma: no cover - must not be called
        raise AssertionError("ingest must not create symlinks")

    monkeypatch.setattr(os, "symlink", fail_symlink)
    payload = "# Title\n\n## Section\n\nbody\n".encode("utf-8")
    root, conn = _ingest(tmp_path, monkeypatch, "plain.md", payload)
    try:
        assert conn.execute("SELECT COUNT(*) FROM blocks").fetchone()[0] == 2
    finally:
        conn.close()
    assert (root / ".stratum" / "stratum.db").is_file()


def test_short_stable_ids_and_database_layout(tmp_path):
    document = scan_document("# Heading\n\nBody text.\n")
    assert document.headings
    doc_id = document_id({}, "notes/entry.md")
    assert DOCUMENT_ID_RE.fullmatch(doc_id)
    block_id = compute_block_id(doc_id, "profile", "heading", "Heading", 0)
    assert BLOCK_ID_RE.fullmatch(block_id)
    assert block_id == compute_block_id(doc_id, "profile", "heading", "Heading", 0)
    assert len(block_id) == 20

    home = tmp_path / "home"
    root = tmp_path / "workspace"
    result = init_workspace(root, name="short", home=home)
    db_path = Path(result.workspace.db_path)
    assert db_path.parent == (root / ".stratum").resolve()
    assert db_path.name == "stratum.db"
    assert len(str(db_path)) < len(str(root)) + 40


def test_platform_lock_imports_are_lazy():
    import stratum.workspace as workspace_module

    # Importing the module must work on Windows, where fcntl is unavailable.
    assert "fcntl" not in vars(workspace_module)
    assert "msvcrt" not in vars(workspace_module)


def test_document_ids_are_case_safe_and_separator_stable():
    upper = document_id({}, "Notes/Entry.md")
    lower = document_id({}, "notes/entry.md")
    windows = document_id({}, "Notes\\Entry.md")
    assert upper == "Notes/Entry.md"
    assert lower == "notes/entry.md"
    assert windows == upper
    # Distinct case spellings must not collapse into one identity. The
    # platform filesystem decides whether both files can coexist; the identity
    # layer never lowercases a path implicitly.
    assert upper != lower
