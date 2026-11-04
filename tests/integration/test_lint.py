"""Plain-text report lint and weekly/biweekly count gates."""

from __future__ import annotations

from pathlib import Path

from stratum.cli import main
from stratum.lint import lint_file


def _weekly(claims: int) -> str:
    body = "\n\n".join(
        f"""### OBS-W-2026-01-05-0{index} Synthetic weekly observation {index}
- claim_id: OBS-W-2026-01-05-0{index}
- claim_type: observation
- period_start: 2026-01-05
- period_end: 2026-01-12
- granularity: week
- entities: self
- derived_from: OBS-D-2026-01-05-0{index}
- evidence: C-EXAMPLE-{index}
- confidence: medium
- body: Synthetic weekly observation {index}."""
        for index in range(1, claims + 1)
    )
    return f"""---
report_contract: v1
report_type: weekly
report_id: RPT-W-2026-01-05
period_start: 2026-01-05
period_end: 2026-01-12
granularity: week
author: 04-weekly-review
base_commit: CMT-0123456789abcdef
created_at: 2026-01-12T01:00:00+08:00
timezone: Asia/Shanghai
---

## 叙事

Synthetic weekly narrative.

## 检索条目

{body}

## 运维

- 命令: stratum status
"""


def _daily() -> str:
    return """---
report_contract: v1
report_type: daily
report_id: RPT-D-2026-01-05
period_start: 2026-01-05
period_end: 2026-01-06
granularity: day
author: 02-daily-review
base_commit: none
created_at: 2026-01-06T00:30:00+08:00
timezone: Asia/Shanghai
---

## 叙事

Synthetic daily narrative.

## 检索条目

### OBS-D-2026-01-05-01 Synthetic daily observation
- claim_id: OBS-D-2026-01-05-01
- claim_type: observation
- period_start: 2026-01-05
- period_end: 2026-01-06
- granularity: day
- entities: self
- evidence: C-EXAMPLE-0001
- confidence: medium
- body: Synthetic daily observation.

## 运维

- 命令: stratum status
"""


def test_lint_valid_daily_report_exits_zero(tmp_path, capsys):
    path = tmp_path / "daily.md"
    path.write_text(_daily(), encoding="utf-8")

    assert main(["lint", str(path)]) == 0
    out = capsys.readouterr().out
    assert "Ready: true" in out
    assert "Issues: none" in out
    assert "{" not in out


def test_lint_valid_weekly_report_exits_zero(tmp_path, capsys):
    path = tmp_path / "weekly.md"
    path.write_text(_weekly(3), encoding="utf-8")

    assert main(["lint", "--profile", "loom-weekly", str(path)]) == 0
    out = capsys.readouterr().out
    assert "Profile: loom-weekly" in out
    assert "Claims: 3" in out
    assert "Ready: true" in out


def test_lint_weekly_below_minimum(tmp_path, capsys):
    path = tmp_path / "weekly.md"
    path.write_text(_weekly(2), encoding="utf-8")

    assert main(["lint", str(path)]) == 1
    out = capsys.readouterr().out
    assert "Ready: false" in out
    assert "OBS_COUNT_LOW" in out


def test_lint_weekly_above_maximum(tmp_path, capsys):
    path = tmp_path / "weekly.md"
    path.write_text(_weekly(8), encoding="utf-8")

    assert main(["lint", str(path)]) == 1
    assert "OBS_COUNT_HIGH" in capsys.readouterr().out


def test_lint_profile_mismatch(tmp_path, capsys):
    path = tmp_path / "daily.md"
    path.write_text(_daily(), encoding="utf-8")

    assert main(["lint", "--profile", "loom-weekly", str(path)]) == 1
    assert "PROFILE_REPORT_TYPE_MISMATCH" in capsys.readouterr().out


def test_lint_reports_parse_error(tmp_path, capsys):
    path = tmp_path / "broken.md"
    path.write_text("## 叙事\n", encoding="utf-8")

    assert main(["lint", str(path)]) == 1
    out = capsys.readouterr().out
    assert "Ready: false" in out
    assert "FRONTMATTER_MISSING" in out


def test_lint_missing_file_is_usage_error(tmp_path, capsys):
    assert main(["lint", str(tmp_path / "missing.md")]) == 2
    assert "missing.md" in capsys.readouterr().err


def test_lint_file_uses_cmt_base_commit(tmp_path):
    path = tmp_path / "daily.md"
    path.write_text(_daily().replace("base_commit: none", "base_commit: CMT-0123456789abcdef"), encoding="utf-8")

    assert lint_file(path).ready is True
