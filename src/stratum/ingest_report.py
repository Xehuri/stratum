"""Report ingest: frontmatter, sections, claims, evidence and relations.

Reports are Markdown files with a frozen v1 contract. The whole operation is
one SQLite transaction: a document version, claims, evidence links and claim
relations either all land or none do. Reports never author a claim status or
infer claims from narrative/ops sections.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Mapping

from .claims import (
    REPORT_GRANULARITY,
    REPORT_SCALE,
    ClaimDraft,
    EvidenceTarget,
    delete_claim_relations,
    delete_evidence,
    detect_cycles,
    draft_from_fields,
    insert_claim_row,
    insert_draft_relations,
    insert_evidence_link,
    parse_claim_id,
    parse_field_lines,
    resolve_counterexample_card,
    resolve_evidence,
    update_claim_row,
    validate_relations,
)
from .db import open_db
from .errors import ValidationError
from .ingest_source import (
    SourceIngestResult,
    ingest_document,
    resolve_workspace_file,
    transaction,
)
from .markdown import ScannedDocument, normalize_text, scan_document
from .migrations import utcnow
from .report_rules import enforce_claim_counts
from .workspace import Workspace, resolve_workspace

__all__ = [
    "ParsedReport",
    "ReportFrontMatter",
    "ReportIngestResult",
    "get_report",
    "ingest_report_file",
    "parse_report",
]

REQUIRED_FRONTMATTER = (
    "report_contract",
    "report_type",
    "report_id",
    "period_start",
    "period_end",
    "granularity",
    "author",
    "base_commit",
    "created_at",
    "timezone",
)
ALLOWED_FRONTMATTER = frozenset(REQUIRED_FRONTMATTER)
REQUIRED_SECTIONS = ("叙事", "检索条目", "运维")
SECTION_PERIOD_DAYS = {"daily": 1, "weekly": 7, "biweekly": 14}
_REPORT_ID_RE = re.compile(r"^RPT-([DWB])-(\d{4}-\d{2}-\d{2})(?:-r([2-9]\d*))?$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_STRATUM_COMMIT_RE = re.compile(r"^CMT-[0-9a-f]{16}$")
_CLAIM_HEADING_RE = re.compile(r"^###[ \t]+(\S+)(?:[ \t]+(.*?))?[ \t]*$")


@dataclass(frozen=True)
class ReportFrontMatter:
    report_contract: str
    report_type: str
    report_id: str
    period_start: str
    period_end: str
    granularity: str
    author: str
    base_commit: str | None
    created_at: str
    timezone: str


@dataclass(frozen=True)
class ParsedReport:
    frontmatter: ReportFrontMatter
    claims: tuple[ClaimDraft, ...]
    sections: Mapping[str, tuple[int, int]]
    title: str


@dataclass(frozen=True)
class ReportIngestResult:
    status: str
    report_id: str
    report_type: str
    claims_created: int = 0
    claims_updated: int = 0
    claims_rejected: int = 0
    evidence_count: int = 0
    relations: int = 0
    document: SourceIngestResult | None = None


def _fail(code: str, message: str) -> None:
    raise ValidationError(code, message)


def _parse_date(value: str, field: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        _fail("FRONTMATTER_INVALID", f"{field} must be YYYY-MM-DD")


def _parse_frontmatter(scanned: ScannedDocument) -> ReportFrontMatter:
    if scanned.frontmatter is None:
        _fail("FRONTMATTER_MISSING", "report has no YAML frontmatter")

    seen_keys: set[str] = set()
    for line in scanned.frontmatter.raw.split("\n"):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if ":" not in stripped:
            _fail("FRONTMATTER_INVALID", f"invalid frontmatter line {stripped!r}")
        key = stripped.partition(":")[0].strip()
        if key in seen_keys:
            _fail("FRONTMATTER_INVALID", f"duplicate frontmatter field {key!r}")
        seen_keys.add(key)

    values = dict(scanned.frontmatter.values)
    for key in values:
        if key not in ALLOWED_FRONTMATTER:
            _fail("FRONTMATTER_FIELD_UNKNOWN", f"unknown frontmatter field {key!r}")
    for key in REQUIRED_FRONTMATTER:
        if key not in values:
            _fail("FRONTMATTER_FIELD_MISSING", f"missing frontmatter field {key!r}")
        if not isinstance(values[key], str):
            _fail("FRONTMATTER_INVALID", f"frontmatter field {key!r} must be scalar")
        if not values[key]:
            _fail("FRONTMATTER_INVALID", f"frontmatter field {key!r} is empty")

    contract = values["report_contract"]
    if contract not in {"v1", "1"}:
        _fail("CONTRACT_VERSION_UNKNOWN", f"unsupported report_contract {contract!r}")

    report_type = values["report_type"]
    if report_type not in REPORT_SCALE:
        _fail("REPORT_TYPE_INVALID", f"unsupported report_type {report_type!r}")

    report_id = values["report_id"]
    match = _REPORT_ID_RE.fullmatch(report_id)
    if match is None:
        _fail("REPORT_ID_INVALID", f"invalid report_id {report_id!r}")
    if match.group(1) != REPORT_SCALE[report_type]:
        _fail(
            "REPORT_ID_INVALID",
            f"report_id scale does not match report_type {report_type!r}",
        )

    period_start = values["period_start"]
    period_end = values["period_end"]
    start = _parse_date(period_start, "period_start")
    end = _parse_date(period_end, "period_end")
    if end <= start:
        _fail("FRONTMATTER_INVALID", "period_end must be later than period_start")
    if match.group(2) != period_start:
        _fail("REPORT_ID_INVALID", "report_id date must equal period_start")
    if end != start + timedelta(days=SECTION_PERIOD_DAYS[report_type]):
        _fail(
            "FRONTMATTER_INVALID",
            f"{report_type} period must span "
            f"{SECTION_PERIOD_DAYS[report_type]} days",
        )
    if report_type in {"weekly", "biweekly"} and start.weekday() != 0:
        _fail("FRONTMATTER_INVALID", "period_start must be a Monday")

    granularity = values["granularity"]
    if granularity != REPORT_GRANULARITY[report_type]:
        _fail(
            "FRONTMATTER_INVALID",
            f"{report_type} requires granularity "
            f"{REPORT_GRANULARITY[report_type]!r}",
        )

    base_commit_text = values["base_commit"]
    if base_commit_text == "none":
        base_commit = None
    elif _COMMIT_RE.fullmatch(base_commit_text):
        base_commit = base_commit_text
    elif _STRATUM_COMMIT_RE.fullmatch(base_commit_text):
        base_commit = base_commit_text
    else:
        _fail(
            "FRONTMATTER_INVALID",
            "base_commit must be 'none', CMT-<16 lowercase hex>, "
            "or 40 lowercase hex characters",
        )

    created_at = values["created_at"]
    try:
        stamp = datetime.fromisoformat(created_at)
    except ValueError:
        _fail("FRONTMATTER_INVALID", "created_at must be an RFC 3339 timestamp")
    if stamp.tzinfo is None:
        _fail("FRONTMATTER_INVALID", "created_at must include a timezone offset")

    return ReportFrontMatter(
        report_contract="v1",
        report_type=report_type,
        report_id=report_id,
        period_start=period_start,
        period_end=period_end,
        granularity=granularity,
        author=values["author"],
        base_commit=base_commit,
        created_at=created_at,
        timezone=values["timezone"],
    )


def _section_ranges(scanned: ScannedDocument) -> dict[str, tuple[int, int]]:
    level_two = [span for span in scanned.headings if span.level == 2]
    names = [span.heading_text for span in level_two]
    for required in REQUIRED_SECTIONS:
        if required not in names:
            _fail("SECTION_MISSING", f"missing section '## {required}'")
    for name in names:
        if name not in REQUIRED_SECTIONS:
            _fail("SECTION_UNKNOWN", f"unknown section '## {name}'")
    if names != list(REQUIRED_SECTIONS):
        _fail(
            "SECTION_ORDER",
            "sections must appear as 叙事, 检索条目, 运维",
        )

    ranges: dict[str, tuple[int, int]] = {}
    for index, span in enumerate(level_two):
        end = (
            level_two[index + 1].line_start - 1
            if index + 1 < len(level_two)
            else len(scanned.lines)
        )
        ranges[str(span.heading_text)] = (span.line_start, end)
    return ranges


def _check_nested_headings(
    scanned: ScannedDocument, sections: Mapping[str, tuple[int, int]]
) -> None:
    claim_start, claim_end = sections["检索条目"]
    for span in scanned.headings:
        if span.level is None or span.level < 3:
            continue
        if span.level == 3 and claim_start <= span.line_start <= claim_end:
            continue
        if sections["叙事"][0] <= span.line_start <= sections["叙事"][1]:
            _fail(
                "NARRATIVE_AS_CLAIM",
                "level-three headings are not allowed in 叙事",
            )
        if sections["运维"][0] <= span.line_start <= sections["运维"][1]:
            _fail("OPS_AS_CLAIM", "level-three headings are not allowed in 运维")
        _fail("SECTION_UNKNOWN", "only level-three claim headings are allowed")


def _parse_claims(
    scanned: ScannedDocument,
    frontmatter: ReportFrontMatter,
    sections: Mapping[str, tuple[int, int]],
) -> tuple[ClaimDraft, ...]:
    start, end = sections["检索条目"]
    lines = list(scanned.lines)
    fenced_lines: set[int] = set()
    for span in scanned.fenced_code:
        fenced_lines.update(range(span.line_start, span.line_end + 1))
    # Section ranges are 1-based inclusive; skip the "## 检索条目" line itself.
    claim_lines = lines[start:end]
    claims: list[ClaimDraft] = []
    seen: set[str] = set()
    index = 0
    while index < len(claim_lines):
        raw = claim_lines[index]
        line_number = start + index + 1
        if line_number in fenced_lines:
            _fail(
                "SECTION_UNKNOWN",
                "fenced code is not allowed in 检索条目",
            )
        if not raw.strip():
            index += 1
            continue
        if raw.startswith("####"):
            _fail("SECTION_UNKNOWN", "only level-three headings may define claims")
        match = _CLAIM_HEADING_RE.match(raw) if raw.startswith("###") else None
        if match is None:
            _fail(
                "SECTION_UNKNOWN",
                "only claim headings and field lines may appear in 检索条目",
            )

        claim_id = match.group(1)
        title = (match.group(2) or "").strip()
        if not title:
            _fail("CLAIM_FIELD_INVALID", f"claim {claim_id} has no title")
        if len(title) > 40:
            _fail("CLAIM_FIELD_INVALID", f"claim {claim_id} title exceeds 40 characters")
        parse_claim_id(claim_id)
        if claim_id in seen:
            _fail("CLAIM_ID_CONFLICT", f"duplicate claim id {claim_id}")
        seen.add(claim_id)

        index += 1
        block: list[str] = []
        while index < len(claim_lines) and not claim_lines[index].startswith("###"):
            block.append(claim_lines[index])
            index += 1
        while block and not block[0].strip():
            block.pop(0)
        while block and not block[-1].strip():
            block.pop()
        if any(not line.strip() for line in block):
            _fail(
                "CLAIM_FIELD_INVALID",
                f"claim {claim_id} field lines must not contain blank lines",
            )
        fields = parse_field_lines(block, where=f"claim {claim_id}")
        draft = draft_from_fields(
            fields,
            heading_id=claim_id,
            report_type=frontmatter.report_type,
            report_period=(frontmatter.period_start, frontmatter.period_end),
            report_id=frontmatter.report_id,
            author=frontmatter.author,
            where=f"claim {claim_id}",
        )
        claims.append(draft)

    return tuple(claims)


def parse_report(text: str, *, source_name: str = "report") -> ParsedReport:
    """Parse a Loom v1 report without touching the database."""

    scanned = scan_document(normalize_text(text))
    frontmatter = _parse_frontmatter(scanned)
    sections = _section_ranges(scanned)
    _check_nested_headings(scanned, sections)
    claims = _parse_claims(scanned, frontmatter, sections)
    title = frontmatter.report_id
    for span in scanned.headings:
        if span.level == 1 and span.heading_text:
            title = span.heading_text
            break
    return ParsedReport(
        frontmatter=frontmatter,
        claims=claims,
        sections=sections,
        title=title,
    )


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def get_report(conn: sqlite3.Connection, report_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()


def _report_claim_rows(
    conn: sqlite3.Connection, report_id: str
) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM claims WHERE report_id = ? ORDER BY id", (report_id,)
    ).fetchall()


def _report_metadata_matches(
    row: sqlite3.Row,
    frontmatter: ReportFrontMatter,
    document_id: str,
    title: str,
) -> bool:
    stored_base = row["base_commit"]
    return (
        str(row["report_type"]) == frontmatter.report_type
        and str(row["period_start"]) == frontmatter.period_start
        and str(row["period_end"]) == frontmatter.period_end
        and str(row["author"]) == frontmatter.author
        and int(row["contract_version"]) == 1
        and (stored_base if stored_base else None) == frontmatter.base_commit
        and row["document_id"] == document_id
        and str(row["title"]) == title
    )


def _claim_fingerprints(rows: list[sqlite3.Row]) -> dict[str, str]:
    return {
        str(row["id"]): str(row["content_sha256"])
        for row in rows
        if row["status"] != "rejected"
    }


def _check_claim_identity_conflicts(
    conn: sqlite3.Connection,
    parsed: ParsedReport,
    *,
    existing_report: sqlite3.Row | None,
) -> None:
    report_id = parsed.frontmatter.report_id
    for draft in parsed.claims:
        row = conn.execute(
            "SELECT report_id, claim_type FROM claims WHERE id = ?",
            (draft.claim_id,),
        ).fetchone()
        if row is None:
            continue
        if existing_report is None:
            _fail(
                "CLAIM_ID_CONFLICT",
                f"claim id {draft.claim_id} already exists",
            )
        if row["report_id"] != report_id:
            _fail(
                "CLAIM_ID_CONFLICT",
                f"claim id {draft.claim_id} belongs to another report",
            )
        if str(row["claim_type"]) != draft.claim_type:
            _fail(
                "CLAIM_ID_CONFLICT",
                f"claim id {draft.claim_id} changed claim_type",
            )


def _mark_removed_drafts(
    conn: sqlite3.Connection, report_id: str, new_ids: set[str]
) -> int:
    rows = conn.execute(
        "SELECT id FROM claims WHERE report_id = ? AND status = 'draft'",
        (report_id,),
    ).fetchall()
    removed = [str(row["id"]) for row in rows if str(row["id"]) not in new_ids]
    now = utcnow()
    for claim_id in removed:
        conn.execute(
            "UPDATE claims SET status = 'rejected', updated_at = ? WHERE id = ?",
            (now, claim_id),
        )
        delete_claim_relations(conn, claim_id)
        delete_evidence(conn, claim_id)
    return len(removed)


def _write_claims(
    conn: sqlite3.Connection,
    parsed: ParsedReport,
    *,
    existing_report: sqlite3.Row | None,
    report_id: str,
) -> tuple[int, int]:
    existing_ids: set[str] = set()
    if existing_report is not None:
        existing_ids = {
            str(row["id"]) for row in _report_claim_rows(conn, report_id)
        }

    created = 0
    updated = 0
    for draft in parsed.claims:
        if draft.claim_id in existing_ids:
            update_claim_row(conn, draft)
            updated += 1
        else:
            insert_claim_row(conn, draft)
            created += 1
    return created, updated


def _write_evidence(
    conn: sqlite3.Connection,
    parsed: ParsedReport,
    document_id: str,
) -> int:
    count = 0
    for draft in parsed.claims:
        delete_evidence(conn, draft.claim_id)
        for ref in draft.evidence:
            target = resolve_evidence(
                conn,
                ref,
                report_document_id=document_id,
                sections=parsed.sections,
                where=f"claim {draft.claim_id}",
            )
            insert_evidence_link(conn, draft.claim_id, target)
            count += 1
        for ref in draft.counterexample_cards:
            card_id = resolve_counterexample_card(
                conn, ref, where=f"claim {draft.claim_id}"
            )
            insert_evidence_link(
                conn,
                draft.claim_id,
                EvidenceTarget(
                    ref=card_id,
                    target_type="card",
                    target_id=card_id,
                    role="counterexample",
                ),
            )
    return count


def _write_relations(
    conn: sqlite3.Connection, parsed: ParsedReport, report_type: str
) -> int:
    report_id = parsed.frontmatter.report_id
    conn.execute(
        "DELETE FROM claim_relations WHERE from_claim_id IN "
        "(SELECT id FROM claims WHERE report_id = ?)",
        (report_id,),
    )
    count = 0
    for draft in parsed.claims:
        validate_relations(
            conn, draft, report_type=report_type, where=f"claim {draft.claim_id}"
        )
        count += insert_draft_relations(conn, draft)
    detect_cycles(conn)
    return count


def _insert_report_row(
    conn: sqlite3.Connection,
    parsed: ParsedReport,
    document_id: str,
) -> None:
    frontmatter = parsed.frontmatter
    now = utcnow()
    conn.execute(
        """
        INSERT INTO reports (
            id, report_type, title, period_start, period_end, author,
            contract_version, base_commit, document_id, status,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, 'draft', ?, ?)
        """,
        (
            frontmatter.report_id,
            frontmatter.report_type,
            parsed.title,
            frontmatter.period_start,
            frontmatter.period_end,
            frontmatter.author,
            frontmatter.base_commit,
            document_id,
            now,
            now,
        ),
    )


def _update_report_row(
    conn: sqlite3.Connection,
    parsed: ParsedReport,
    document_id: str,
) -> None:
    frontmatter = parsed.frontmatter
    conn.execute(
        """
        UPDATE reports SET
            report_type = ?,
            title = ?,
            period_start = ?,
            period_end = ?,
            author = ?,
            contract_version = 1,
            base_commit = ?,
            document_id = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (
            frontmatter.report_type,
            parsed.title,
            frontmatter.period_start,
            frontmatter.period_end,
            frontmatter.author,
            frontmatter.base_commit,
            document_id,
            utcnow(),
            frontmatter.report_id,
        ),
    )


def _ingest_report(
    conn: sqlite3.Connection, workspace: Workspace, path: str | Path, parsed: ParsedReport
) -> ReportIngestResult:
    document = ingest_document(conn, workspace, path)
    frontmatter = parsed.frontmatter
    report_id = frontmatter.report_id

    existing = get_report(conn, report_id)
    existing_claims = (
        _report_claim_rows(conn, report_id) if existing is not None else []
    )
    new_fingerprints = {
        draft.claim_id: draft.content_sha256 for draft in parsed.claims
    }

    if (
        existing is not None
        and document.status == "unchanged"
        and _report_metadata_matches(
            existing, frontmatter, document.document_id, parsed.title
        )
        and _claim_fingerprints(existing_claims) == new_fingerprints
    ):
        return ReportIngestResult(
            status="unchanged",
            report_id=report_id,
            report_type=frontmatter.report_type,
            document=document,
        )

    if existing is not None:
        blocking = [
            row for row in existing_claims if row["status"] not in {"draft", "rejected"}
        ]
        if blocking:
            _fail(
                "REPORT_IMMUTABLE",
                f"report {report_id} has non-draft claims and cannot be revised",
            )

    _check_claim_identity_conflicts(
        conn, parsed, existing_report=existing
    )

    if existing is None:
        _insert_report_row(conn, parsed, document.document_id)
    else:
        _update_report_row(conn, parsed, document.document_id)

    rejected = 0
    if existing is not None:
        rejected = _mark_removed_drafts(
            conn, report_id, set(new_fingerprints)
        )

    created, updated = _write_claims(
        conn,
        parsed,
        existing_report=existing,
        report_id=report_id,
    )
    evidence_count = _write_evidence(conn, parsed, document.document_id)
    relations = _write_relations(conn, parsed, frontmatter.report_type)

    return ReportIngestResult(
        status="created" if existing is None else "revised",
        report_id=report_id,
        report_type=frontmatter.report_type,
        claims_created=created,
        claims_updated=updated,
        claims_rejected=rejected,
        evidence_count=evidence_count,
        relations=relations,
        document=document,
    )


def ingest_report_file(
    path: str | Path, *, workspace: str | None = None
) -> ReportIngestResult:
    selected = resolve_workspace(explicit=workspace)
    file_path, relative_path = resolve_workspace_file(selected, path)
    text = normalize_text(file_path.read_bytes())
    parsed = parse_report(text, source_name=relative_path)
    enforce_claim_counts(parsed)

    conn = open_db(selected.db_path)
    try:
        with transaction(conn):
            return _ingest_report(conn, selected, file_path, parsed)
    finally:
        conn.close()


# Convenience alias for callers that prefer a shorter verb.
ingest_report = ingest_report_file
