"""End-to-end coverage for ``stratum demo``.

The runner is exercised through the CLI entry point. Tests inspect the kept
temporary workspace to prove the printed summary is backed by real database
rows, not a hard-coded transcript.
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from stratum import demo as demo_module
from stratum.cli import main
from stratum.markdown import scan_file

ROOT = Path(__file__).resolve().parents[2]
DEMO = ROOT / "demo"


def _run_demo(capsys, monkeypatch, *args: str) -> tuple[int, str, str]:
    code = main(["demo", *args])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _kept_path(output: str) -> Path:
    for line in output.splitlines():
        if line.startswith("Kept temporary demo: "):
            return Path(line.removeprefix("Kept temporary demo: "))
    raise AssertionError(f"no kept path in output:\n{output}")


def _db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def test_demo_runs_twice_deterministically(monkeypatch, capsys):
    monkeypatch.chdir(ROOT)
    first = _run_demo(capsys, monkeypatch)
    second = _run_demo(capsys, monkeypatch)

    assert first[0] == 0
    assert second[0] == 0
    assert first[1] == second[1]
    assert first[2] == ""
    output = first[1]
    for label in (
        "Stratum demo",
        "Workspaces: 3",
        "Manual block: BLK-",
        "Card: C-",
        "Claim: OBS-D-2026-02-02-01",
        "Prose hit: BLK-",
        "Legacy hit: BLK-",
        "Fsck: clean (0 findings)",
        "Commit:",
        "Log:",
        "Query: 1 active claim(s)",
    ):
        assert label in output
    assert not output.lstrip().startswith("{")
    assert '"' not in output
    assert not any((DEMO).rglob(".stratum"))
    assert not any((DEMO).rglob("*.db"))


def test_demo_cleans_temp_workspace_by_default(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(ROOT)
    created: list[Path] = []

    def fake_mkdtemp(*, prefix: str = "") -> str:
        path = tmp_path / f"{prefix}run"
        path.mkdir()
        created.append(path)
        return str(path)

    monkeypatch.setattr(demo_module.tempfile, "mkdtemp", fake_mkdtemp)
    code, out, err = _run_demo(capsys, monkeypatch)

    assert code == 0
    assert err == ""
    assert "Kept temporary demo" not in out
    assert len(created) == 1
    assert not created[0].exists()


def test_demo_keep_inspects_workspaces_and_database(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(ROOT)
    created: list[Path] = []

    def fake_mkdtemp(*, prefix: str = "") -> str:
        path = tmp_path / f"{prefix}kept"
        path.mkdir()
        created.append(path)
        return str(path)

    monkeypatch.setattr(demo_module.tempfile, "mkdtemp", fake_mkdtemp)
    code, out, err = _run_demo(capsys, monkeypatch, "--keep")

    assert code == 0
    assert err == ""
    kept = _kept_path(out)
    assert kept == created[0]
    assert kept.is_dir()

    workspaces = kept / "workspaces"
    assert sorted(item.name for item in workspaces.iterdir()) == [
        "lantern-library",
        "prose-notes",
        "tidepool-lab",
    ]

    tidepool = _db(workspaces / "tidepool-lab" / ".stratum" / "stratum.db")
    try:
        manual = tidepool.execute(
            "SELECT id, line_start, line_end FROM blocks "
            "WHERE selector_type = 'manual_range'"
        ).fetchone()
        assert manual is not None
        assert (manual["line_start"], manual["line_end"]) == (12, 24)
        card = tidepool.execute(
            "SELECT id, status FROM cards"
        ).fetchone()
        assert card is not None
        assert card["status"] == "active"
        assert tidepool.execute("SELECT COUNT(*) FROM commits").fetchone()[0] == 1
        assert tidepool.execute("SELECT COUNT(*) FROM fsck_findings").fetchone()[0] == 0
    finally:
        tidepool.close()

    lantern = _db(workspaces / "lantern-library" / ".stratum" / "stratum.db")
    try:
        claim = lantern.execute(
            "SELECT id, status FROM claims"
        ).fetchone()
        assert claim is not None
        assert claim["id"] == "OBS-D-2026-02-02-01"
        assert claim["status"] == "active"
        assert lantern.execute("SELECT COUNT(*) FROM commits").fetchone()[0] == 1
        assert lantern.execute("SELECT COUNT(*) FROM fsck_findings").fetchone()[0] == 0
    finally:
        lantern.close()

    prose = _db(workspaces / "prose-notes" / ".stratum" / "stratum.db")
    try:
        prose_blocks = prose.execute(
            "SELECT id, retrieval_type, snapshot_text FROM blocks "
            "WHERE retrieval_type = 'prose'"
        ).fetchall()
        assert prose_blocks
        assert any("潮间带" in row["snapshot_text"] for row in prose_blocks)
        assert prose.execute("SELECT COUNT(*) FROM commits").fetchone()[0] == 1
    finally:
        prose.close()

    shutil.rmtree(kept)


def test_demo_root_option_accepts_a_copy(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    copied = tmp_path / "synthetic-copy"
    shutil.copytree(DEMO, copied)

    code, out, err = _run_demo(capsys, monkeypatch, "--root", str(copied))

    assert code == 0
    assert err == ""
    assert "Workspaces: 3" in out
    assert not any(copied.rglob(".stratum"))
    assert not any(copied.rglob("*.db"))


def test_demo_missing_root_is_usage_error(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    missing = tmp_path / "missing-demo"

    code, out, err = _run_demo(capsys, monkeypatch, "--root", str(missing))

    assert code == 2
    assert out == ""
    assert "demo root not found" in err


def test_demo_unknown_workspace_is_usage_error(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    copied = tmp_path / "partial-demo"
    shutil.copytree(DEMO, copied)
    shutil.rmtree(copied / "workspaces" / "tidepool-lab")

    code, out, err = _run_demo(capsys, monkeypatch, "--root", str(copied))

    assert code == 2
    assert out == ""
    assert "tidepool-lab" in err


def test_demo_restores_stratum_home(monkeypatch, capsys):
    monkeypatch.chdir(ROOT)
    monkeypatch.setenv("STRATUM_HOME", "/tmp/synthetic-preexisting-home")

    code, _, _ = _run_demo(capsys, monkeypatch)

    assert code == 0
    assert (
        __import__("os").environ["STRATUM_HOME"]
        == "/tmp/synthetic-preexisting-home"
    )


def test_demo_corpus_has_required_markdown_structures():
    reading_log = scan_file(DEMO / "workspaces" / "lantern-library" / "reading-log.md")
    assert len(reading_log.tables) == 1
    assert any(span.heading_text.startswith("OBS-") for span in reading_log.headings)

    fence_document = scan_file(
        DEMO / "workspaces" / "lantern-library" / "notes" / "the-dispossessed.md"
    )
    assert len(fence_document.fenced_code) == 1
    anchors = [span.heading_text for span in fence_document.headings]
    assert "Narrative" in anchors
    assert "This heading is only sample code" not in anchors

    journal = scan_file(
        DEMO / "workspaces" / "tidepool-lab" / "journal" / "2026-01-19.md"
    )
    assert len(journal.fenced_code) == 1
    assert all(
        "This is inside a code fence" not in (span.heading_text or "")
        for span in journal.headings
    )

    prose = scan_file(DEMO / "workspaces" / "prose-notes" / "field-notes.md")
    assert any("潮间带" in span.text for span in prose.paragraphs)
