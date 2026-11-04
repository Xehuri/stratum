"""Report frontmatter, section and claim parsing against the v1 contract."""

from __future__ import annotations

from pathlib import Path

import pytest

from stratum.errors import ValidationError
from stratum.ingest_report import parse_report

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "reports"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _code(exc: pytest.ExceptionInfo[ValidationError]) -> str:
    return exc.value.code


def test_parse_daily_fixture() -> None:
    report = parse_report(_fixture("daily.md"))

    assert report.frontmatter.report_contract == "v1"
    assert report.frontmatter.report_type == "daily"
    assert report.frontmatter.report_id == "RPT-D-2026-01-05"
    assert report.frontmatter.base_commit is None
    assert report.frontmatter.granularity == "day"
    assert report.title == "RPT-D-2026-01-05"
    assert [claim.claim_id for claim in report.claims] == [
        "OBS-D-2026-01-05-01",
        "CHG-D-2026-01-05-01",
    ]
    observation = report.claims[0]
    assert observation.claim_type == "observation"
    assert observation.entities == ("示例实体",)
    assert observation.evidence == ("C-EXAMPLE-0001",)
    assert observation.content_sha256
    assert len(observation.content_sha256) == 64
    assert report.sections["叙事"][0] < report.sections["检索条目"][0]


def test_parse_weekly_fixture_lists_and_scale() -> None:
    report = parse_report(_fixture("weekly.md"))

    claim = report.claims[0]
    assert report.frontmatter.report_type == "weekly"
    assert report.frontmatter.base_commit == (
        "0123456789abcdef0123456789abcdef01234567"
    )
    assert claim.granularity == "week"
    assert claim.derived_from == (
        "OBS-D-2026-01-05-01",
        "CHG-D-2026-01-06-01",
    )


def test_parse_report_accepts_stratum_commit_id() -> None:
    text = _fixture("daily.md").replace(
        "base_commit: none", "base_commit: CMT-0123456789abcdef"
    )

    report = parse_report(text)

    assert report.frontmatter.base_commit == "CMT-0123456789abcdef"


@pytest.mark.parametrize(
    "value",
    (
        "CMT-0123456789ABCDEF",
        "CMT-0123456789abcde",
        "CMT-0123456789abcdef0",
        "0123456789abcdef0123456789abcdef0123456",
        "0123456789abcdef0123456789abcdef012345678",
        "0123456789ABCDEF0123456789abcdef01234567",
    ),
)
def test_parse_report_rejects_invalid_base_commit(value: str) -> None:
    text = _fixture("daily.md").replace("base_commit: none", f"base_commit: {value}")

    with pytest.raises(ValidationError) as exc:
        parse_report(text)

    assert _code(exc) == "FRONTMATTER_INVALID"


def test_parse_biweekly_fixture_counts() -> None:
    report = parse_report(_fixture("biweekly.md"))
    claim = report.claims[0]

    assert claim.claim_type == "pattern"
    assert claim.granularity == "range"
    assert claim.support_count == 2
    assert claim.opportunity_count == 3
    assert claim.counterexample_count == 1
    assert claim.counterexample_cards == ("C-EXAMPLE-0003",)


def test_report_contract_one_is_accepted() -> None:
    text = _fixture("daily.md").replace("report_contract: v1", "report_contract: 1")
    report = parse_report(text)
    assert report.frontmatter.report_contract == "v1"


def test_missing_frontmatter() -> None:
    with pytest.raises(ValidationError) as exc:
        parse_report("## 叙事\n\n## 检索条目\n\n## 运维\n")
    assert _code(exc) == "FRONTMATTER_MISSING"


def test_missing_frontmatter_field() -> None:
    text = _fixture("daily.md").replace("timezone: Asia/Shanghai\n", "")
    with pytest.raises(ValidationError) as exc:
        parse_report(text)
    assert _code(exc) == "FRONTMATTER_FIELD_MISSING"


def test_duplicate_frontmatter_field() -> None:
    text = _fixture("daily.md").replace(
        "timezone: Asia/Shanghai",
        "timezone: Asia/Shanghai\ntimezone: Asia/Shanghai",
    )
    with pytest.raises(ValidationError) as exc:
        parse_report(text)
    assert _code(exc) == "FRONTMATTER_INVALID"


def test_unknown_frontmatter_field() -> None:
    text = _fixture("daily.md").replace(
        "timezone: Asia/Shanghai", "timezone: Asia/Shanghai\nindex_profile: x"
    )
    with pytest.raises(ValidationError) as exc:
        parse_report(text)
    assert _code(exc) == "FRONTMATTER_FIELD_UNKNOWN"


def test_unknown_contract_version() -> None:
    with pytest.raises(ValidationError) as exc:
        parse_report(_fixture("bad_contract.md"))
    assert _code(exc) == "CONTRACT_VERSION_UNKNOWN"


def test_missing_section() -> None:
    with pytest.raises(ValidationError) as exc:
        parse_report(_fixture("bad_missing_section.md"))
    assert _code(exc) == "SECTION_MISSING"


def test_unknown_section() -> None:
    text = _fixture("daily.md").replace(
        "## 运维", "## 附录\n\ntext\n\n## 运维"
    )
    with pytest.raises(ValidationError) as exc:
        parse_report(text)
    assert _code(exc) == "SECTION_UNKNOWN"


def test_section_order() -> None:
    text = _fixture("daily.md")
    text = text.replace("## 叙事", "## TEMP")
    text = text.replace("## 检索条目", "## 叙事")
    text = text.replace("## TEMP", "## 检索条目")
    with pytest.raises(ValidationError) as exc:
        parse_report(text)
    assert _code(exc) == "SECTION_ORDER"


def test_fenced_code_in_claim_section_is_not_parsed_as_claims() -> None:
    text = _fixture("daily.md").replace(
        "## 检索条目\n\n",
        "## 检索条目\n\n```text\n### OBS-D-2026-01-05-99 Fake\n"
        "- claim_id: OBS-D-2026-01-05-99\n```\n\n",
        1,
    )
    with pytest.raises(ValidationError) as exc:
        parse_report(text)
    assert _code(exc) == "SECTION_UNKNOWN"


def test_ops_and_narrative_field_lines_are_not_claims() -> None:
    text = _fixture("daily.md").replace(
        "- 结果: 合成数据",
        "- 结果: 合成数据\n- claim_id: OBS-D-2026-01-05-99\n- evidence: C-FAKE",
    )
    text = text.replace(
        "这是一个合成的日复盘叙事段落，仅用于测试。",
        "这是一个合成的日复盘叙事段落，仅用于测试。\n- claim_type: observation",
    )
    report = parse_report(text)
    assert len(report.claims) == 2


def test_narrative_heading_is_rejected() -> None:
    text = _fixture("daily.md").replace(
        "这是一个合成的日复盘叙事段落，仅用于测试。",
        "### OBS-D-2026-01-05-99 叙事标题\n- claim_id: OBS-D-2026-01-05-99",
    )
    with pytest.raises(ValidationError) as exc:
        parse_report(text)
    assert _code(exc) == "NARRATIVE_AS_CLAIM"


def test_ops_heading_is_rejected() -> None:
    text = _fixture("daily.md").replace(
        "- 命令: stratum status",
        "### OBS-D-2026-01-05-99 运维标题",
    )
    with pytest.raises(ValidationError) as exc:
        parse_report(text)
    assert _code(exc) == "OPS_AS_CLAIM"


def test_claim_without_evidence_is_rejected() -> None:
    with pytest.raises(ValidationError) as exc:
        parse_report(_fixture("bad_evidence.md"))
    assert _code(exc) == "EVIDENCE_MISSING"


def test_data_gap_replaces_evidence() -> None:
    text = _fixture("daily.md").replace(
        "- evidence: C-EXAMPLE-0001\n- confidence: medium\n- body: 示例观测内容，仅用于测试格式。",
        "- data_gap: 当天没有原始记录\n- confidence: medium\n- body: 示例观测内容，仅用于测试格式。",
    )
    report = parse_report(text)
    assert report.claims[0].data_gap == "当天没有原始记录"
    assert report.claims[0].evidence == ()


def test_status_is_never_authored() -> None:
    text = _fixture("daily.md").replace(
        "- confidence: medium", "- status: active\n- confidence: medium"
    )
    with pytest.raises(ValidationError) as exc:
        parse_report(text)
    assert _code(exc) == "STATUS_AUTHORED"


def test_unknown_claim_field() -> None:
    text = _fixture("daily.md").replace(
        "- confidence: medium", "- surprise: value\n- confidence: medium"
    )
    with pytest.raises(ValidationError) as exc:
        parse_report(text)
    assert _code(exc) == "CLAIM_FIELD_UNKNOWN"


def test_duplicate_scalar_field() -> None:
    text = _fixture("daily.md").replace(
        "- confidence: medium", "- confidence: medium\n- confidence: low"
    )
    with pytest.raises(ValidationError) as exc:
        parse_report(text)
    assert _code(exc) == "CLAIM_FIELD_DUPLICATE"


def test_claim_sequence_zero_is_rejected() -> None:
    text = _fixture("daily.md").replace(
        "OBS-D-2026-01-05-01", "OBS-D-2026-01-05-00"
    )
    with pytest.raises(ValidationError) as exc:
        parse_report(text)
    assert _code(exc) == "CLAIM_ID_INVALID"


def test_invalid_claim_id() -> None:
    with pytest.raises(ValidationError) as exc:
        parse_report(_fixture("bad_claim_id.md"))
    assert _code(exc) == "CLAIM_ID_INVALID"


def test_period_must_match_report() -> None:
    text = _fixture("daily.md").replace(
        "- period_end: 2026-01-06", "- period_end: 2026-01-07", 1
    )
    with pytest.raises(ValidationError) as exc:
        parse_report(text)
    assert _code(exc) == "CLAIM_PERIOD_MISMATCH"


def test_daily_claim_cannot_derive_from() -> None:
    text = _fixture("daily.md").replace(
        "- evidence: C-EXAMPLE-0001",
        "- derived_from: OBS-D-2026-01-04-01\n- evidence: C-EXAMPLE-0001",
        1,
    )
    with pytest.raises(ValidationError) as exc:
        parse_report(text)
    assert _code(exc) == "DERIVED_FROM_LAYER"


def test_weekly_claim_requires_derived_from() -> None:
    text = _fixture("weekly.md")
    text = text.replace("- derived_from: OBS-D-2026-01-05-01\n", "")
    text = text.replace("- derived_from: CHG-D-2026-01-06-01\n", "")
    with pytest.raises(ValidationError) as exc:
        parse_report(text)
    assert _code(exc) == "DERIVED_FROM_MISSING"


def test_claim_title_length_is_bounded() -> None:
    long_title = "x" * 41
    text = _fixture("daily.md").replace("示例观测", long_title, 1)
    with pytest.raises(ValidationError) as exc:
        parse_report(text)
    assert _code(exc) == "CLAIM_FIELD_INVALID"


def test_blank_line_inside_claim_is_rejected() -> None:
    text = _fixture("daily.md").replace(
        "- entities: 示例实体", "- entities: 示例实体\n\n- evidence: C-EXAMPLE-0001",
        1,
    )
    text = text.replace(
        "- evidence: C-EXAMPLE-0001\n- confidence: medium\n- body: 示例观测内容，仅用于测试格式。\n",
        "- confidence: medium\n- body: 示例观测内容，仅用于测试格式。\n",
        1,
    )
    with pytest.raises(ValidationError) as exc:
        parse_report(text)
    assert _code(exc) == "CLAIM_FIELD_INVALID"


def test_fingerprint_is_field_order_insensitive_but_value_sensitive() -> None:
    text = _fixture("daily.md")
    reordered = text.replace(
        "- claim_id: OBS-D-2026-01-05-01\n- claim_type: observation",
        "- claim_type: observation\n- claim_id: OBS-D-2026-01-05-01",
    )
    assert (
        parse_report(text).claims[0].content_sha256
        == parse_report(reordered).claims[0].content_sha256
    )

    changed = text.replace(
        "示例观测内容，仅用于测试格式。", "示例观测内容，已改动。"
    )
    assert (
        parse_report(text).claims[0].content_sha256
        != parse_report(changed).claims[0].content_sha256
    )
