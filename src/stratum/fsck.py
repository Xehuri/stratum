"""Deep integrity checks for the Stratum index.

``fsck`` records every invocation in ``fsck_runs`` and every finding in
``fsck_findings``. Findings are typed objects for Python callers; the CLI
renders plain text only. ``--fix`` is intentionally narrow: it marks active
claims and cards with broken evidence ``stale`` inside the same transaction
as the run. It never deletes rows, never rewrites Markdown and never
promotes anything.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Iterable

from .claims import parse_claim_id
from .errors import ValidationError
from .ingest_source import transaction
from .migrations import utcnow

__all__ = [
    "Finding",
    "FixAction",
    "FsckResult",
    "format_fsck",
    "run_fsck",
]

SEVERITIES = ("error", "warning", "info")

FIXABLE_CLAIM_CODES = frozenset(
    {
        "CLAIM_EVIDENCE_MISSING",
        "CLAIM_CARD_MISSING",
        "CLAIM_CARD_NOT_ACTIVE",
        "CLAIM_BLOCK_MISSING",
        "CLAIM_BLOCK_PROSE",
        "CLAIM_SNAPSHOT_MISMATCH",
        "CLAIM_EVIDENCE_UNSUPPORTED",
    }
)
FIXABLE_CARD_CODES = frozenset(
    {
        "CARD_EVIDENCE_MISSING",
        "CARD_BLOCK_MISSING",
        "CARD_BLOCK_PROSE",
        "CARD_BLOCK_NOT_ACTIVE",
        "CARD_SNAPSHOT_MISMATCH",
    }
)


@dataclass(frozen=True)
class Finding:
    code: str
    severity: str
    entity_type: str | None
    entity_id: str | None
    message: str


@dataclass(frozen=True)
class FixAction:
    entity_type: str
    entity_id: str
    from_status: str
    to_status: str
    codes: tuple[str, ...]


@dataclass(frozen=True)
class FsckResult:
    run_id: str
    status: str
    findings: tuple[Finding, ...]
    fixed: tuple[FixAction, ...]

    @property
    def error_count(self) -> int:
        return sum(1 for finding in self.findings if finding.severity == "error")

    @property
    def warning_count(self) -> int:
        return sum(
            1 for finding in self.findings if finding.severity == "warning"
        )

    @property
    def info_count(self) -> int:
        return sum(1 for finding in self.findings if finding.severity == "info")

    @property
    def has_errors(self) -> bool:
        return self.error_count > 0


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------


def _sort_findings(findings: Iterable[Finding]) -> tuple[Finding, ...]:
    order = {severity: index for index, severity in enumerate(SEVERITIES)}
    return tuple(
        sorted(
            findings,
            key=lambda item: (
                order.get(item.severity, len(SEVERITIES)),
                item.entity_type or "",
                item.entity_id or "",
                item.code,
                item.message,
            ),
        )
    )


def _check_foreign_keys(conn: sqlite3.Connection, findings: list[Finding]) -> None:
    for row in conn.execute("PRAGMA foreign_key_check").fetchall():
        table = str(row[0])
        rowid = row[1]
        parent = str(row[2])
        findings.append(
            Finding(
                code="FOREIGN_KEY_VIOLATION",
                severity="error",
                entity_type=table,
                entity_id=str(rowid),
                message=(
                    f"{table} row {rowid} violates a foreign key to {parent}"
                ),
            )
        )


def _check_claim_evidence_presence(
    conn: sqlite3.Connection, findings: list[Finding]
) -> None:
    rows = conn.execute(
        """
        SELECT c.id
        FROM claims c
        WHERE c.data_gap IS NULL
          AND c.status NOT IN ('rejected', 'stale')
          AND NOT EXISTS (
              SELECT 1 FROM evidence_links e
              WHERE e.owner_type = 'claim' AND e.owner_id = c.id
          )
        ORDER BY c.id
        """
    ).fetchall()
    for row in rows:
        claim_id = str(row[0])
        findings.append(
            Finding(
                code="CLAIM_EVIDENCE_MISSING",
                severity="error",
                entity_type="claim",
                entity_id=claim_id,
                message="claim has no evidence and no data_gap",
            )
        )

    rows = conn.execute(
        """
        SELECT id, data_gap FROM claims
        WHERE status = 'active' AND data_gap IS NOT NULL
        ORDER BY id
        """
    ).fetchall()
    for row in rows:
        claim_id = str(row[0])
        findings.append(
            Finding(
                code="CLAIM_DATA_GAP_ACTIVE",
                severity="error",
                entity_type="claim",
                entity_id=claim_id,
                message=f"active claim has data_gap: {row[1]}",
            )
        )


def _check_active_claim_evidence(
    conn: sqlite3.Connection, findings: list[Finding]
) -> None:
    rows = conn.execute(
        """
        SELECT e.owner_id AS claim_id, e.target_type, e.target_id, e.role,
               e.quote_sha256,
               b.id AS block_found, b.retrieval_type, b.status AS block_status,
               b.snapshot_sha256,
               c.id AS card_found, c.status AS card_status
        FROM evidence_links e
        JOIN claims cl ON cl.id = e.owner_id AND cl.status = 'active'
        LEFT JOIN blocks b ON e.target_type = 'block' AND b.id = e.target_id
        LEFT JOIN cards c ON e.target_type = 'card' AND c.id = e.target_id
        WHERE e.owner_type = 'claim'
        ORDER BY e.owner_id, e.target_type, e.target_id, e.role
        """
    ).fetchall()
    for row in rows:
        claim_id = str(row["claim_id"])
        target_id = str(row["target_id"])
        target_type = str(row["target_type"])
        if target_type == "card":
            if row["card_found"] is None:
                findings.append(
                    Finding(
                        code="CLAIM_CARD_MISSING",
                        severity="error",
                        entity_type="claim",
                        entity_id=claim_id,
                        message=f"claim references missing card {target_id}",
                    )
                )
            elif str(row["card_status"]) != "active":
                findings.append(
                    Finding(
                        code="CLAIM_CARD_NOT_ACTIVE",
                        severity="error",
                        entity_type="claim",
                        entity_id=claim_id,
                        message=(
                            f"claim references card {target_id} "
                            f"which is {row['card_status']}"
                        ),
                    )
                )
            continue
        if target_type == "block":
            if row["block_found"] is None:
                findings.append(
                    Finding(
                        code="CLAIM_BLOCK_MISSING",
                        severity="error",
                        entity_type="claim",
                        entity_id=claim_id,
                        message=f"claim references missing block {target_id}",
                    )
                )
                continue
            if str(row["retrieval_type"]) == "prose":
                findings.append(
                    Finding(
                        code="CLAIM_BLOCK_PROSE",
                        severity="error",
                        entity_type="claim",
                        entity_id=claim_id,
                        message=f"claim references prose block {target_id}",
                    )
                )
                continue
            if str(row["block_status"]) == "missing":
                findings.append(
                    Finding(
                        code="CLAIM_BLOCK_MISSING",
                        severity="error",
                        entity_type="claim",
                        entity_id=claim_id,
                        message=f"claim references block {target_id} marked missing",
                    )
                )
                continue
            quote = row["quote_sha256"]
            if quote is not None and str(quote) != str(row["snapshot_sha256"]):
                findings.append(
                    Finding(
                        code="CLAIM_SNAPSHOT_MISMATCH",
                        severity="error",
                        entity_type="claim",
                        entity_id=claim_id,
                        message=(
                            f"claim snapshot for block {target_id} "
                            "no longer matches"
                        ),
                    )
                )
            continue
        findings.append(
            Finding(
                code="CLAIM_EVIDENCE_UNSUPPORTED",
                severity="error",
                entity_type="claim",
                entity_id=claim_id,
                message=f"claim has unsupported evidence target {target_type}",
            )
        )


def _check_active_claim_derivations(
    conn: sqlite3.Connection, findings: list[Finding]
) -> None:
    claim_rows = conn.execute(
        """
        SELECT id, claim_type, period_start, period_end, status
        FROM claims ORDER BY id
        """
    ).fetchall()
    claims = {str(row[0]): row for row in claim_rows}

    edges = conn.execute(
        """
        SELECT from_claim_id, to_claim_id FROM claim_relations
        WHERE relation_type = 'derived_from'
        ORDER BY from_claim_id, to_claim_id
        """
    ).fetchall()
    derived: dict[str, list[str]] = {}
    for row in edges:
        derived.setdefault(str(row[0]), []).append(str(row[1]))

    for claim_id, claim in claims.items():
        if str(claim["status"]) != "active":
            continue
        targets = derived.get(claim_id, [])
        try:
            parsed = parse_claim_id(claim_id)
        except ValidationError:
            findings.append(
                Finding(
                    code="CLAIM_DERIVED_FROM_INVALID",
                    severity="error",
                    entity_type="claim",
                    entity_id=claim_id,
                    message="active claim id cannot be parsed for layer checks",
                )
            )
            continue

        if parsed.scale == "D":
            if targets:
                findings.append(
                    Finding(
                        code="CLAIM_DERIVED_FROM_LAYER",
                        severity="error",
                        entity_type="claim",
                        entity_id=claim_id,
                        message="daily claim must not use derived_from",
                    )
                )
            continue
        if not targets:
            findings.append(
                Finding(
                    code="CLAIM_DERIVED_FROM_MISSING",
                    severity="error",
                    entity_type="claim",
                    entity_id=claim_id,
                    message="active aggregate claim has no derived_from target",
                )
            )
            continue

        expected_scale = "D" if parsed.scale == "W" else "W"
        for target_id in targets:
            target = claims.get(target_id)
            if target is None:
                findings.append(
                    Finding(
                        code="CLAIM_DERIVED_FROM_MISSING",
                        severity="error",
                        entity_type="claim",
                        entity_id=claim_id,
                        message=f"derived_from target {target_id} does not exist",
                    )
                )
                continue
            try:
                target_parsed = parse_claim_id(target_id)
            except ValidationError:
                findings.append(
                    Finding(
                        code="CLAIM_DERIVED_FROM_INVALID",
                        severity="error",
                        entity_type="claim",
                        entity_id=claim_id,
                        message=f"derived_from target id {target_id} is invalid",
                    )
                )
                continue
            if target_parsed.scale != expected_scale:
                findings.append(
                    Finding(
                        code="CLAIM_DERIVED_FROM_LAYER",
                        severity="error",
                        entity_type="claim",
                        entity_id=claim_id,
                        message=(
                            f"derived_from target {target_id} is on the "
                            f"wrong layer; expected {expected_scale}"
                        ),
                    )
                )
            if (
                parsed.scale == "B"
                and str(target["claim_type"]) != "observation"
            ):
                findings.append(
                    Finding(
                        code="CLAIM_DERIVED_FROM_LAYER",
                        severity="error",
                        entity_type="claim",
                        entity_id=claim_id,
                        message=(
                            f"derived_from target {target_id} must be a "
                            "weekly observation"
                        ),
                    )
                )
            if (
                str(target["period_start"]) < str(claim["period_start"])
                or str(target["period_end"]) > str(claim["period_end"])
            ):
                findings.append(
                    Finding(
                        code="CLAIM_DERIVED_FROM_PERIOD",
                        severity="error",
                        entity_type="claim",
                        entity_id=claim_id,
                        message=(
                            f"derived_from target {target_id} is outside "
                            "the claim period"
                        ),
                    )
                )
            if str(target["status"]) != "active":
                findings.append(
                    Finding(
                        code="CLAIM_DERIVED_FROM_NOT_ACTIVE",
                        severity="error",
                        entity_type="claim",
                        entity_id=claim_id,
                        message=(
                            f"derived_from target {target_id} is "
                            f"{target['status']}"
                        ),
                    )
                )


def _check_relation_cycles(
    conn: sqlite3.Connection, findings: list[Finding]
) -> None:
    rows = conn.execute(
        """
        SELECT from_claim_id, to_claim_id FROM claim_relations
        WHERE relation_type IN ('derived_from', 'supersedes')
        ORDER BY from_claim_id, to_claim_id
        """
    ).fetchall()
    adjacency: dict[str, list[str]] = {}
    for row in rows:
        adjacency.setdefault(str(row[0]), []).append(str(row[1]))

    colour: dict[str, int] = {}
    stack: list[str] = []
    seen: set[frozenset[str]] = set()

    def visit(node: str) -> None:
        colour[node] = 1
        stack.append(node)
        for target in adjacency.get(node, ()):
            state = colour.get(target, 0)
            if state == 1:
                start = stack.index(target)
                cycle = tuple(stack[start:] + [target])
                key = frozenset(cycle)
                if key not in seen:
                    seen.add(key)
                    findings.append(
                        Finding(
                            code="RELATION_CYCLE",
                            severity="error",
                            entity_type="claim",
                            entity_id=target,
                            message="relation cycle: " + " -> ".join(cycle),
                        )
                    )
            elif state == 0:
                visit(target)
        stack.pop()
        colour[node] = 2

    for node in sorted(adjacency):
        if colour.get(node, 0) == 0:
            visit(node)


def _check_active_cards(
    conn: sqlite3.Connection, findings: list[Finding]
) -> None:
    rows = conn.execute(
        """
        SELECT cb.card_id, cb.block_id, cb.quote_sha256,
               b.id AS block_found, b.retrieval_type, b.status AS block_status,
               b.snapshot_sha256
        FROM card_blocks cb
        JOIN cards c ON c.id = cb.card_id AND c.status = 'active'
        LEFT JOIN blocks b ON b.id = cb.block_id
        ORDER BY cb.card_id, cb.block_id
        """
    ).fetchall()
    for row in rows:
        card_id = str(row["card_id"])
        block_id = str(row["block_id"])
        if row["block_found"] is None:
            findings.append(
                Finding(
                    code="CARD_BLOCK_MISSING",
                    severity="error",
                    entity_type="card",
                    entity_id=card_id,
                    message=f"card references missing block {block_id}",
                )
            )
            continue
        if str(row["retrieval_type"]) == "prose":
            findings.append(
                Finding(
                    code="CARD_BLOCK_PROSE",
                    severity="error",
                    entity_type="card",
                    entity_id=card_id,
                    message=f"card references prose block {block_id}",
                )
            )
            continue
        if str(row["block_status"]) == "missing":
            findings.append(
                Finding(
                    code="CARD_BLOCK_MISSING",
                    severity="error",
                    entity_type="card",
                    entity_id=card_id,
                    message=f"card references block {block_id} marked missing",
                )
            )
            continue
        if str(row["block_status"]) != "active":
            findings.append(
                Finding(
                    code="CARD_BLOCK_NOT_ACTIVE",
                    severity="error",
                    entity_type="card",
                    entity_id=card_id,
                    message=(
                        f"card references block {block_id} "
                        f"which is {row['block_status']}"
                    ),
                )
            )
            continue
        quote = row["quote_sha256"]
        if quote is None or str(quote) != str(row["snapshot_sha256"]):
            findings.append(
                Finding(
                    code="CARD_SNAPSHOT_MISMATCH",
                    severity="error",
                    entity_type="card",
                    entity_id=card_id,
                    message=(
                        f"card snapshot for block {block_id} no longer matches"
                    ),
                )
            )

    empty_cards = conn.execute(
        """
        SELECT c.id FROM cards c
        WHERE c.status = 'active'
          AND NOT EXISTS (
              SELECT 1 FROM card_blocks cb WHERE cb.card_id = c.id
          )
        ORDER BY c.id
        """
    ).fetchall()
    for row in empty_cards:
        card_id = str(row[0])
        findings.append(
            Finding(
                code="CARD_EVIDENCE_MISSING",
                severity="error",
                entity_type="card",
                entity_id=card_id,
                message="active card has no card_blocks",
            )
        )


def _check_reports(conn: sqlite3.Connection, findings: list[Finding]) -> None:
    rows = conn.execute(
        """
        SELECT r.id, r.document_id
        FROM reports r
        LEFT JOIN documents d ON d.id = r.document_id
        WHERE r.document_id IS NULL OR d.id IS NULL
        ORDER BY r.id
        """
    ).fetchall()
    for row in rows:
        report_id = str(row[0])
        document_id = row[1]
        if document_id is None:
            message = "report has no document"
        else:
            message = f"report references missing document {document_id}"
        findings.append(
            Finding(
                code="REPORT_DOCUMENT_MISSING",
                severity="error",
                entity_type="report",
                entity_id=report_id,
                message=message,
            )
        )


def _check_evidence_links(
    conn: sqlite3.Connection, findings: list[Finding]
) -> None:
    owners = conn.execute(
        """
        SELECT e.id, e.owner_type, e.owner_id
        FROM evidence_links e
        WHERE NOT (
            (e.owner_type = 'claim' AND EXISTS (
                SELECT 1 FROM claims c WHERE c.id = e.owner_id))
            OR (e.owner_type = 'card' AND EXISTS (
                SELECT 1 FROM cards c WHERE c.id = e.owner_id))
            OR (e.owner_type = 'report' AND EXISTS (
                SELECT 1 FROM reports r WHERE r.id = e.owner_id))
        )
        ORDER BY e.id
        """
    ).fetchall()
    for row in owners:
        findings.append(
            Finding(
                code="EVIDENCE_OWNER_MISSING",
                severity="error",
                entity_type=str(row["owner_type"]),
                entity_id=str(row["owner_id"]),
                message=f"evidence link {row['id']} has no owner",
            )
        )

    targets = conn.execute(
        """
        SELECT e.id, e.target_type, e.target_id
        FROM evidence_links e
        WHERE (e.target_type = 'card' AND NOT EXISTS (
                   SELECT 1 FROM cards c WHERE c.id = e.target_id))
           OR (e.target_type = 'block' AND NOT EXISTS (
                   SELECT 1 FROM blocks b WHERE b.id = e.target_id))
           OR (e.target_type = 'report' AND NOT EXISTS (
                   SELECT 1 FROM reports r WHERE r.id = e.target_id))
        ORDER BY e.id
        """
    ).fetchall()
    for row in targets:
        findings.append(
            Finding(
                code="EVIDENCE_TARGET_MISSING",
                severity="warning",
                entity_type=str(row["target_type"]),
                entity_id=str(row["target_id"]),
                message=f"evidence link {row['id']} points at a missing target",
            )
        )


def _check_review_events(
    conn: sqlite3.Connection, findings: list[Finding]
) -> None:
    rows = conn.execute(
        """
        SELECT re.id, re.entity_type, re.entity_id
        FROM review_events re
        WHERE (re.entity_type = 'claim' AND NOT EXISTS (
                   SELECT 1 FROM claims c WHERE c.id = re.entity_id))
           OR (re.entity_type = 'card' AND NOT EXISTS (
                   SELECT 1 FROM cards c WHERE c.id = re.entity_id))
        ORDER BY re.id
        """
    ).fetchall()
    for row in rows:
        findings.append(
            Finding(
                code="REVIEW_EVENT_ENTITY_MISSING",
                severity="warning",
                entity_type=str(row["entity_type"]),
                entity_id=str(row["entity_id"]),
                message=(
                    f"review event {row['id']} references an entity "
                    "that no longer exists"
                ),
            )
        )


def _collect_findings(conn: sqlite3.Connection) -> tuple[Finding, ...]:
    findings: list[Finding] = []
    _check_foreign_keys(conn, findings)
    _check_claim_evidence_presence(conn, findings)
    _check_active_claim_evidence(conn, findings)
    _check_active_claim_derivations(conn, findings)
    _check_relation_cycles(conn, findings)
    _check_active_cards(conn, findings)
    _check_reports(conn, findings)
    _check_evidence_links(conn, findings)
    _check_review_events(conn, findings)
    return _sort_findings(findings)


# ---------------------------------------------------------------------------
# Safe fixes
# ---------------------------------------------------------------------------


def _apply_safe_fixes(
    conn: sqlite3.Connection, findings: Iterable[Finding], *, now: str
) -> tuple[FixAction, ...]:
    claim_codes: dict[str, set[str]] = {}
    card_codes: dict[str, set[str]] = {}
    for finding in findings:
        if finding.entity_id is None:
            continue
        if finding.entity_type == "claim" and finding.code in FIXABLE_CLAIM_CODES:
            claim_codes.setdefault(finding.entity_id, set()).add(finding.code)
        elif finding.entity_type == "card" and finding.code in FIXABLE_CARD_CODES:
            card_codes.setdefault(finding.entity_id, set()).add(finding.code)

    actions: list[FixAction] = []
    for entity_type, table, targets in (
        ("claim", "claims", claim_codes),
        ("card", "cards", card_codes),
    ):
        for entity_id in sorted(targets):
            cursor = conn.execute(
                f"""
                UPDATE {table}
                SET status = 'stale', updated_at = ?
                WHERE id = ? AND status = 'active'
                """,
                (now, entity_id),
            )
            if cursor.rowcount:
                actions.append(
                    FixAction(
                        entity_type=entity_type,
                        entity_id=entity_id,
                        from_status="active",
                        to_status="stale",
                        codes=tuple(sorted(targets[entity_id])),
                    )
                )
    return tuple(actions)


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


def _finding_id(run_id: str, index: int, finding: Finding) -> str:
    seed = "\x00".join(
        (
            run_id,
            str(index),
            finding.code,
            finding.entity_type or "",
            finding.entity_id or "",
        )
    )
    return "FSF-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def run_fsck(conn: sqlite3.Connection, *, fix: bool = False) -> FsckResult:
    run_id = "FSCK-" + uuid.uuid4().hex[:16]
    now = utcnow()
    with transaction(conn):
        conn.execute(
            """
            INSERT INTO fsck_runs (
                id, status, options_json, finding_count, started_at, created_at
            ) VALUES (?, 'running', ?, 0, ?, ?)
            """,
            (
                run_id,
                json.dumps({"fix": fix}, sort_keys=True, separators=(",", ":")),
                now,
                now,
            ),
        )

        findings = _collect_findings(conn)
        fixed: tuple[FixAction, ...] = ()
        if fix:
            actions: list[FixAction] = []
            # A card marked stale can make an active claim's evidence
            # broken in the next pass. Repeat the same safe repair until
            # no further active claim or card matches.
            for _ in range(10):
                applied = _apply_safe_fixes(conn, findings, now=utcnow())
                if not applied:
                    break
                actions.extend(applied)
                findings = _collect_findings(conn)
            fixed = tuple(actions)

        for index, finding in enumerate(findings):
            conn.execute(
                """
                INSERT INTO fsck_findings (
                    id, run_id, code, severity, entity_type, entity_id,
                    message, details_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?)
                """,
                (
                    _finding_id(run_id, index, finding),
                    run_id,
                    finding.code,
                    finding.severity,
                    finding.entity_type,
                    finding.entity_id,
                    finding.message,
                    now,
                ),
            )

        status = (
            "failed"
            if any(finding.severity == "error" for finding in findings)
            else "clean"
        )
        conn.execute(
            """
            UPDATE fsck_runs
            SET status = ?, finding_count = ?, finished_at = ?
            WHERE id = ?
            """,
            (status, len(findings), utcnow(), run_id),
        )

    return FsckResult(
        run_id=run_id,
        status=status,
        findings=findings,
        fixed=fixed,
    )


def format_fsck(result: FsckResult) -> str:
    lines = [
        "Stratum fsck",
        f"Run: {result.run_id}",
        f"Status: {result.status}",
        (
            f"Findings: {len(result.findings)} "
            f"(error {result.error_count}, warning {result.warning_count}, "
            f"info {result.info_count})"
        ),
    ]
    for action in result.fixed:
        codes = ", ".join(action.codes)
        lines.append(
            f"Fixed: {action.entity_type} {action.entity_id} "
            f"{action.from_status} -> {action.to_status} ({codes})"
        )
    for finding in result.findings:
        if finding.entity_type is None:
            entity = "-"
        elif finding.entity_id is None:
            entity = finding.entity_type
        else:
            entity = f"{finding.entity_type} {finding.entity_id}"
        lines.append(
            f"  [{finding.severity.upper()}] {finding.code}  "
            f"{entity}: {finding.message}"
        )
    return "\n".join(lines) + "\n"
