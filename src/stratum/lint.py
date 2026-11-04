"""Plain-text contract lint for Loom v1 reports."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .errors import ValidationError
from .ingest_report import ParsedReport, parse_report
from .markdown import normalize_text
from .report_rules import check_profile, claim_count_issues

__all__ = ["LintIssue", "LintResult", "format_lint", "lint_file"]


@dataclass(frozen=True)
class LintIssue:
    code: str
    message: str


@dataclass(frozen=True)
class LintResult:
    path: str
    profile: str | None
    claims: int
    issues: tuple[LintIssue, ...]

    @property
    def ready(self) -> bool:
        return not self.issues


def _lint_parsed(parsed: ParsedReport, profile: str | None) -> LintResult:
    issues: list[LintIssue] = []
    try:
        check_profile(parsed, profile)
    except ValidationError as exc:
        issues.append(LintIssue(exc.code, exc.message))
    for code, message in claim_count_issues(parsed):
        issues.append(LintIssue(code, message))
    return LintResult(
        path="",
        profile=profile,
        claims=len(parsed.claims),
        issues=tuple(issues),
    )


def lint_file(
    path: str | Path, *, profile: str | None = None
) -> LintResult:
    """Parse and lint one report without touching a workspace."""
    file_path = Path(path)
    text = normalize_text(file_path.read_bytes())
    try:
        parsed = parse_report(text, source_name=str(file_path))
    except ValidationError as exc:
        return LintResult(
            path=str(file_path),
            profile=profile,
            claims=0,
            issues=(LintIssue(exc.code, exc.message),),
        )
    result = _lint_parsed(parsed, profile)
    return LintResult(
        path=str(file_path),
        profile=result.profile,
        claims=result.claims,
        issues=result.issues,
    )


def format_lint(result: LintResult) -> str:
    lines = [
        "Stratum lint",
        f"Path: {result.path}",
        "Contract: v1",
        f"Profile: {result.profile or 'auto'}",
        f"Claims: {result.claims}",
        f"Ready: {'true' if result.ready else 'false'}",
        "Issues: none" if result.ready else "Issues:",
    ]
    if not result.ready:
        for issue in result.issues:
            lines.append(f"  [ERROR] {issue.code}: {issue.message}")
    return "\n".join(lines) + "\n"
