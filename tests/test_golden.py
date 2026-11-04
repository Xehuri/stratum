"""Golden CLI transcripts for the demo and the three report layers.

Every transcript is produced by running ``python -m stratum`` as a real
subprocess. Temporary absolute paths, timestamps and the random fsck run id are
normalised so the same fixtures pass on Linux and Windows.

Set ``UPDATE_GOLDEN=1`` to regenerate the expected files after an intentional
output change.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from stratum.add import add_card, add_claim
from stratum.db import open_db
from stratum.ingest_report import ingest_report_file
from stratum.ingest_source import ingest_source_file
from stratum.review import promote
from stratum.workspace import init_workspace

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = Path(__file__).resolve().parent / "golden"
FSCK_RUN_RE = re.compile(r"Run: FSCK-[0-9a-f]{16}")


def _update_expected(name: str, text: str) -> None:
    if os.environ.get("UPDATE_GOLDEN") == "1":
        (GOLDEN / name).write_text(text, encoding="utf-8", newline="\n")


def _normalise(text: str) -> str:
    normalised = text.replace("\r\n", "\n").replace("\r", "\n")
    normalised = FSCK_RUN_RE.sub("Run: FSCK-<RUN>", normalised)
    return normalised


def _cli_env(home: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["STRATUM_HOME"] = str(home)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    prefix = str(ROOT / "src")
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = prefix if not existing else prefix + os.pathsep + existing
    return env


def _run_cli(cwd: Path, home: Path, *args: str) -> str:
    completed = subprocess.run(
        [sys.executable, "-m", "stratum", *args],
        cwd=str(cwd),
        env=_cli_env(home),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert completed.returncode == 0, (
        f"command failed: stratum {' '.join(args)}\n"
        f"stdout:\n{completed.stdout}\n"
        f"stderr:\n{completed.stderr}"
    )
    return completed.stdout


def _capture(chunks: list[str], home: Path, cwd: Path, *args: str) -> str:
    output = _run_cli(cwd, home, *args)
    chunks.append(f"$ stratum {' '.join(args)}")
    chunks.append(output.rstrip("\n"))
    chunks.append("")
    return output


def _transcript(chunks: list[str]) -> str:
    return "\n".join(chunks).rstrip("\n") + "\n"


def _init(tmp_path: Path, monkeypatch, name: str):
    home = tmp_path / "home"
    root = tmp_path / "notes"
    root.mkdir(parents=True)
    monkeypatch.setenv("STRATUM_HOME", str(home))
    monkeypatch.chdir(tmp_path)
    result = init_workspace(root, name=name, home=home)
    return root, result.workspace, home


def _write(root: Path, relative: str, text: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def _first_block_id(workspace) -> str:
    conn = open_db(workspace.db_path)
    try:
        row = conn.execute(
            "SELECT id FROM blocks ORDER BY line_start LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    return str(row["id"])


def _card_and_daily_claims(workspace, *, week_start: str = "2026-03-02"):
    block_id = _first_block_id(workspace)
    card = add_card(
        blocks=[block_id],
        card_type="observation",
        body="Synthetic golden evidence card.",
        title="Synthetic golden card",
        author="04-weekly-review",
        date=week_start,
        workspace=workspace.workspace_id,
    )
    conn = open_db(workspace.db_path)
    try:
        promote(conn, card.card_id, author="04-weekly-review", reason="golden")
    finally:
        conn.close()

    daily_ids = (
        "OBS-D-2026-03-02-01",
        "OBS-D-2026-03-03-01",
        "OBS-D-2026-03-04-01",
    )
    for index, claim_id in enumerate(daily_ids):
        add_claim(
            claim_id=claim_id,
            claim_type="observation",
            period_start=f"2026-03-0{2 + index}",
            period_end=f"2026-03-0{3 + index}",
            granularity="day",
            entities=["synthetic-reader"],
            evidence=[card.card_id],
            confidence="medium",
            body=f"Synthetic daily claim {index + 1}.",
            author="02-daily-review",
            workspace=workspace.workspace_id,
        )
        conn = open_db(workspace.db_path)
        try:
            promote(conn, claim_id, author="02-daily-review", reason="golden")
        finally:
            conn.close()
    return card.card_id, daily_ids


def _daily_report(card_id: str, block_id: str) -> str:
    return f"""---
report_contract: v1
report_type: daily
report_id: RPT-D-2026-03-02
period_start: 2026-03-02
period_end: 2026-03-03
granularity: day
author: 02-daily-review
base_commit: none
created_at: 2026-03-03T00:30:00+08:00
timezone: Asia/Shanghai
---

## 叙事

Synthetic golden daily narrative.

## 检索条目

### OBS-D-2026-03-02-01 Synthetic golden morning observation
- claim_id: OBS-D-2026-03-02-01
- claim_type: observation
- period_start: 2026-03-02
- period_end: 2026-03-03
- granularity: day
- entities: synthetic-reader
- evidence: {card_id}
- confidence: medium
- body: The synthetic morning batch was indexed.

### CHG-D-2026-03-02-01 Synthetic golden evening change
- claim_id: CHG-D-2026-03-02-01
- claim_type: change
- period_start: 2026-03-02
- period_end: 2026-03-03
- granularity: day
- entities: synthetic-reader
- evidence: {block_id}
- confidence: low
- baseline: one synthetic batch
- delta: two synthetic batches
- body: A second synthetic batch was added.

## 运维

- 命令: stratum status
"""


def test_demo_golden(tmp_path, monkeypatch):
    monkeypatch.chdir(ROOT)
    home = tmp_path / "home"
    output = _normalise(_run_cli(ROOT, home, "demo"))
    _update_expected("demo_expected.txt", output)
    expected = (GOLDEN / "demo_expected.txt").read_text(encoding="utf-8")
    assert output == expected


def test_daily_pipeline_golden(tmp_path, monkeypatch):
    root, workspace, home = _init(tmp_path, monkeypatch, "golden-daily")
    _write(
        root,
        "sources/morning.md",
        "# Morning batch\n\n## OBS-2026-03-02-01 Morning sample\n\n"
        "The synthetic morning sample was recorded.\n",
    )
    _write(
        root,
        "sources/evening.md",
        "# Evening batch\n\n## OBS-2026-03-02-02 Evening sample\n\n"
        "The synthetic evening sample was recorded.\n",
    )

    chunks: list[str] = []
    _capture(chunks, home, root, "ingest", "source", "sources/morning.md")
    _capture(chunks, home, root, "ingest", "source", "sources/evening.md")
    morning = _capture(
        chunks,
        home,
        root,
        "query",
        "--type",
        "block",
        "--grep",
        "Morning sample",
    )
    evening = _capture(
        chunks,
        home,
        root,
        "query",
        "--type",
        "block",
        "--grep",
        "Evening sample",
    )
    morning_block = morning.split()[0]
    evening_block = evening.split()[0]

    card_output = _capture(
        chunks,
        home,
        root,
        "add",
        "card",
        "--block",
        morning_block,
        "--type",
        "observation",
        "--title",
        "Synthetic golden card",
        "--body",
        "Synthetic golden card body.",
        "--author",
        "02-daily-review",
        "--date",
        "2026-03-02",
    )
    card_id = next(
        line.removeprefix("Added card: ")
        for line in card_output.splitlines()
        if line.startswith("Added card: ")
    )
    report = _write(
        root,
        "reports/daily/2026-03-02.md",
        _daily_report(card_id, evening_block),
    )
    _capture(chunks, home, root, "ingest", "report", report.relative_to(root).as_posix())
    _capture(
        chunks,
        home,
        root,
        "review",
        "promote",
        card_id,
        "--author",
        "02-daily-review",
        "--reason",
        "golden",
    )
    for claim_id in ("OBS-D-2026-03-02-01", "CHG-D-2026-03-02-01"):
        _capture(
            chunks,
            home,
            root,
            "review",
            "promote",
            claim_id,
            "--author",
            "02-daily-review",
            "--reason",
            "golden",
        )
    _capture(
        chunks, home, root, "query", "--type", "claim", "--status", "active"
    )
    _capture(
        chunks,
        home,
        root,
        "commit",
        "--author",
        "02-daily-review",
        "--message",
        "golden daily pipeline",
    )
    _capture(chunks, home, root, "fsck")

    actual = _normalise(_transcript(chunks))
    _update_expected("daily_pipeline_expected.txt", actual)
    expected = (GOLDEN / "daily_pipeline_expected.txt").read_text(encoding="utf-8")
    assert actual == expected


def _weekly_report(card_id: str, daily_ids: tuple[str, ...]) -> str:
    body = "\n\n".join(
        (
            f"### OBS-W-2026-03-02-0{index + 1} Synthetic golden weekly observation {index + 1}\n"
            f"- claim_id: OBS-W-2026-03-02-0{index + 1}\n"
            "- claim_type: observation\n"
            "- period_start: 2026-03-02\n"
            "- period_end: 2026-03-09\n"
            "- granularity: week\n"
            "- entities: synthetic-reader\n"
            f"- derived_from: {daily_ids[index]}\n"
            f"- evidence: {card_id}\n"
            "- confidence: medium\n"
            f"- body: Synthetic weekly aggregation {index + 1}.\n"
        )
        for index in range(len(daily_ids))
    )
    return f"""---
report_contract: v1
report_type: weekly
report_id: RPT-W-2026-03-02
period_start: 2026-03-02
period_end: 2026-03-09
granularity: week
author: 04-weekly-review
base_commit: none
created_at: 2026-03-09T01:00:00+08:00
timezone: Asia/Shanghai
---

## 叙事

Synthetic golden weekly narrative.

## 检索条目

{body}

## 运维

- 命令: stratum status
"""


def test_weekly_pipeline_golden(tmp_path, monkeypatch):
    root, workspace, home = _init(tmp_path, monkeypatch, "golden-weekly")
    _write(
        root,
        "sources/week.md",
        "# Weekly source\n\n## OBS-2026-03-02-01 Synthetic source\n\n"
        "Synthetic weekly source paragraph.\n",
    )
    ingest_source_file(
        root / "sources" / "week.md", workspace=workspace.workspace_id
    )
    card_id, daily_ids = _card_and_daily_claims(workspace)
    report = _write(
        root, "reports/weekly/2026-03-02.md", _weekly_report(card_id, daily_ids)
    )

    chunks: list[str] = []
    _capture(chunks, home, root, "ingest", "report", report.relative_to(root).as_posix())
    for index in range(len(daily_ids)):
        _capture(
            chunks,
            home,
            root,
            "review",
            "promote",
            f"OBS-W-2026-03-02-0{index + 1}",
            "--author",
            "04-weekly-review",
            "--reason",
            "golden",
        )
    _capture(
        chunks,
        home,
        root,
        "query",
        "--type",
        "claim",
        "--status",
        "active",
        "--grep",
        "weekly aggregation",
    )
    _capture(
        chunks,
        home,
        root,
        "commit",
        "--author",
        "04-weekly-review",
        "--message",
        "golden weekly pipeline",
    )
    _capture(chunks, home, root, "fsck")

    actual = _normalise(_transcript(chunks))
    _update_expected("weekly_pipeline_expected.txt", actual)
    expected = (GOLDEN / "weekly_pipeline_expected.txt").read_text(encoding="utf-8")
    assert actual == expected


def _biweekly_dependencies(root: Path, workspace, monkeypatch):
    _write(
        root,
        "sources/biweekly.md",
        "# Biweekly source\n\n## OBS-2026-03-02-01 Synthetic first block\n\n"
        "First synthetic evidence paragraph.\n\n"
        "## OBS-2026-03-02-02 Synthetic second block\n\n"
        "Second synthetic evidence paragraph.\n",
    )
    ingest_source_file(
        root / "sources" / "biweekly.md", workspace=workspace.workspace_id
    )
    conn = open_db(workspace.db_path)
    try:
        blocks = [
            str(row["id"])
            for row in conn.execute(
                "SELECT id FROM blocks ORDER BY line_start"
            ).fetchall()
        ]
    finally:
        conn.close()
    evidence = add_card(
        blocks=[blocks[1]],
        card_type="observation",
        body="Synthetic golden evidence card.",
        title="Synthetic golden evidence",
        author="04-weekly-review",
        date="2026-03-02",
        workspace=workspace.workspace_id,
    )
    counterexample = add_card(
        blocks=[blocks[2]],
        card_type="observation",
        body="Synthetic golden counterexample card.",
        title="Synthetic golden counterexample",
        author="04-weekly-review",
        date="2026-03-02",
        workspace=workspace.workspace_id,
    )
    conn = open_db(workspace.db_path)
    try:
        for card_id in (evidence.card_id, counterexample.card_id):
            promote(conn, card_id, author="04-weekly-review", reason="golden")
    finally:
        conn.close()

    daily_ids = (
        "OBS-D-2026-03-02-01",
        "OBS-D-2026-03-03-01",
        "OBS-D-2026-03-09-01",
        "OBS-D-2026-03-10-01",
    )
    days = (
        ("2026-03-02", "2026-03-03"),
        ("2026-03-03", "2026-03-04"),
        ("2026-03-09", "2026-03-10"),
        ("2026-03-10", "2026-03-11"),
    )
    for claim_id, (start, end) in zip(daily_ids, days):
        add_claim(
            claim_id=claim_id,
            claim_type="observation",
            period_start=start,
            period_end=end,
            granularity="day",
            entities=["synthetic-reader"],
            evidence=[evidence.card_id],
            confidence="medium",
            body=f"Synthetic golden daily claim {start}.",
            author="02-daily-review",
            workspace=workspace.workspace_id,
        )
        conn = open_db(workspace.db_path)
        try:
            promote(conn, claim_id, author="02-daily-review", reason="golden")
        finally:
            conn.close()

    week_one = _write(
        root,
        "reports/weekly/2026-03-02.md",
        f"""---
report_contract: v1
report_type: weekly
report_id: RPT-W-2026-03-02
period_start: 2026-03-02
period_end: 2026-03-09
granularity: week
author: 04-weekly-review
base_commit: none
created_at: 2026-03-09T01:00:00+08:00
timezone: Asia/Shanghai
---

## 叙事

Synthetic golden week one narrative.

## 检索条目

### OBS-W-2026-03-02-01 Synthetic week one A
- claim_id: OBS-W-2026-03-02-01
- claim_type: observation
- period_start: 2026-03-02
- period_end: 2026-03-09
- granularity: week
- entities: synthetic-reader
- derived_from: {daily_ids[0]}
- evidence: {evidence.card_id}
- confidence: medium
- body: Synthetic week one observation A.

### OBS-W-2026-03-02-02 Synthetic week one B
- claim_id: OBS-W-2026-03-02-02
- claim_type: observation
- period_start: 2026-03-02
- period_end: 2026-03-09
- granularity: week
- entities: synthetic-reader
- derived_from: {daily_ids[1]}
- evidence: {evidence.card_id}
- confidence: low
- body: Synthetic week one observation B.

## 运维

- 命令: stratum status
""",
    )
    week_two = _write(
        root,
        "reports/weekly/2026-03-09.md",
        f"""---
report_contract: v1
report_type: weekly
report_id: RPT-W-2026-03-09
period_start: 2026-03-09
period_end: 2026-03-16
granularity: week
author: 04-weekly-review
base_commit: none
created_at: 2026-03-16T01:00:00+08:00
timezone: Asia/Shanghai
---

## 叙事

Synthetic golden week two narrative.

## 检索条目

### OBS-W-2026-03-09-01 Synthetic week two
- claim_id: OBS-W-2026-03-09-01
- claim_type: observation
- period_start: 2026-03-09
- period_end: 2026-03-16
- granularity: week
- entities: synthetic-reader
- derived_from: {daily_ids[2]}
- evidence: {evidence.card_id}
- confidence: medium
- body: Synthetic week two observation.

## 运维

- 命令: stratum status
""",
    )
    ingest_report_file(week_one, workspace=workspace.workspace_id)
    ingest_report_file(week_two, workspace=workspace.workspace_id)
    conn = open_db(workspace.db_path)
    try:
        for claim_id in (
            "OBS-W-2026-03-02-01",
            "OBS-W-2026-03-02-02",
            "OBS-W-2026-03-09-01",
        ):
            promote(conn, claim_id, author="04-weekly-review", reason="golden")
    finally:
        conn.close()

    return evidence.card_id, counterexample.card_id


def _biweekly_report(evidence_card: str, counterexample_card: str) -> str:
    return f"""---
report_contract: v1
report_type: biweekly
report_id: RPT-B-2026-03-02
period_start: 2026-03-02
period_end: 2026-03-16
granularity: range
author: 03-biweekly-review
base_commit: none
created_at: 2026-03-16T01:00:00+08:00
timezone: Asia/Shanghai
---

## 叙事

Synthetic golden biweekly narrative.

## 检索条目

### PAT-B-2026-03-02-01 Synthetic golden pattern one
- claim_id: PAT-B-2026-03-02-01
- claim_type: pattern
- period_start: 2026-03-02
- period_end: 2026-03-16
- granularity: range
- entities: synthetic-reader
- derived_from: OBS-W-2026-03-02-01
- derived_from: OBS-W-2026-03-02-02
- evidence: {evidence_card}
- conditions: two synthetic weeks agree
- outcome: the synthetic pattern appears
- support_count: 2
- opportunity_count: 3
- counterexample_count: 1
- counterexample_cards: {counterexample_card}
- confidence: medium
- body: Synthetic golden biweekly pattern one.

### PAT-B-2026-03-02-02 Synthetic golden pattern two
- claim_id: PAT-B-2026-03-02-02
- claim_type: pattern
- period_start: 2026-03-02
- period_end: 2026-03-16
- granularity: range
- entities: synthetic-reader
- derived_from: OBS-W-2026-03-02-02
- derived_from: OBS-W-2026-03-09-01
- evidence: {evidence_card}
- conditions: synthetic condition holds
- outcome: synthetic outcome appears
- support_count: 2
- opportunity_count: 3
- counterexample_count: 1
- counterexample_cards: {counterexample_card}
- confidence: low
- body: Synthetic golden biweekly pattern two.

### HYP-B-2026-03-02-01 Synthetic golden hypothesis
- claim_id: HYP-B-2026-03-02-01
- claim_type: hypothesis
- period_start: 2026-03-02
- period_end: 2026-03-16
- granularity: range
- entities: synthetic-reader
- derived_from: OBS-W-2026-03-02-01
- derived_from: OBS-W-2026-03-09-01
- evidence: {evidence_card}
- conditions: synthetic condition may hold
- outcome: synthetic outcome may appear
- support_count: 1
- opportunity_count: 2
- counterexample_count: 0
- counterexample_note: No counterexample in the synthetic sample.
- confidence: low
- body: Synthetic golden biweekly hypothesis.

## 运维

- 命令: stratum status
"""


def test_biweekly_pipeline_golden(tmp_path, monkeypatch):
    root, workspace, home = _init(tmp_path, monkeypatch, "golden-biweekly")
    evidence_card, counterexample_card = _biweekly_dependencies(
        root, workspace, monkeypatch
    )
    report = _write(
        root,
        "reports/biweekly/2026-03-02.md",
        _biweekly_report(evidence_card, counterexample_card),
    )

    chunks: list[str] = []
    _capture(chunks, home, root, "ingest", "report", report.relative_to(root).as_posix())
    for claim_id in (
        "PAT-B-2026-03-02-01",
        "PAT-B-2026-03-02-02",
        "HYP-B-2026-03-02-01",
    ):
        _capture(
            chunks,
            home,
            root,
            "review",
            "promote",
            claim_id,
            "--author",
            "human",
            "--reason",
            "golden review",
        )
    _capture(
        chunks,
        home,
        root,
        "query",
        "--type",
        "claim",
        "--status",
        "active",
        "--grep",
        "biweekly",
    )
    _capture(
        chunks,
        home,
        root,
        "commit",
        "--author",
        "03-biweekly-review",
        "--message",
        "golden biweekly pipeline",
    )
    _capture(chunks, home, root, "fsck")

    actual = _normalise(_transcript(chunks))
    _update_expected("biweekly_pipeline_expected.txt", actual)
    expected = (GOLDEN / "biweekly_pipeline_expected.txt").read_text(encoding="utf-8")
    assert actual == expected
