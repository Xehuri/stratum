"""Report-level rules shared by lint and ingest."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .errors import ValidationError

if TYPE_CHECKING:
    from .ingest_report import ParsedReport

__all__ = [
    "COUNT_RULES",
    "PROFILE_REPORT_TYPE",
    "claim_count_issues",
    "check_profile",
    "enforce_claim_counts",
]

PROFILE_REPORT_TYPE = {
    "loom-daily": "daily",
    "loom-weekly": "weekly",
    "loom-biweekly": "biweekly",
}

COUNT_RULES = {
    "weekly": ("OBS_COUNT_LOW", "OBS_COUNT_HIGH", "observations"),
    "biweekly": ("CLAIM_COUNT_LOW", "CLAIM_COUNT_HIGH", "claims"),
}


def claim_count_issues(parsed: ParsedReport) -> tuple[tuple[str, str], ...]:
    """Return count-rule issues for reports that require 3 to 7 claims."""
    report_type = parsed.frontmatter.report_type
    rule = COUNT_RULES.get(report_type)
    if rule is None:
        return ()
    low_code, high_code, label = rule
    count = len(parsed.claims)
    if count < 3:
        return (
            (
                low_code,
                f"{report_type} requires 3 to 7 {label}, got {count}",
            ),
        )
    if count > 7:
        return (
            (
                high_code,
                f"{report_type} requires 3 to 7 {label}, got {count}",
            ),
        )
    return ()


def enforce_claim_counts(parsed: ParsedReport) -> None:
    """Reject a report before ingest when its claim count is out of range."""
    issues = claim_count_issues(parsed)
    if issues:
        code, message = issues[0]
        raise ValidationError(code, message)


def check_profile(parsed: ParsedReport, profile: str | None) -> None:
    """Reject a report whose type does not match the requested lint profile."""
    if profile is None:
        return
    expected = PROFILE_REPORT_TYPE[profile]
    actual = parsed.frontmatter.report_type
    if actual != expected:
        raise ValidationError(
            "PROFILE_REPORT_TYPE_MISMATCH",
            f"{profile} expects {expected}, got {actual}",
        )
