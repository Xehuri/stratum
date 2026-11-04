"""Read-only text queries over claims, cards, reports and blocks.

The default claim/card/block view is active content. ``--include-draft``
opens the review backlog for claims and cards (draft, rejected, stale) and
stale/missing blocks for block queries. An explicit ``--status`` always
wins. No FTS table is required: ``--grep`` is a parameterised SQL LIKE.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from typing import Sequence

from .claims import CLAIM_TYPES
from .errors import UsageProblem

__all__ = [
    "QueryRow",
    "format_query_rows",
    "query_items",
]

QUERY_ENTITY_TYPES = frozenset(
    {"claim", "card", "report", "block", "prose", "structured"}
)
QUERY_STATUSES = frozenset(
    {
        "draft",
        "active",
        "superseded",
        "rejected",
        "stale",
        "archived",
        "missing",
    }
)
DEFAULT_LIMIT = 50


@dataclass(frozen=True)
class QueryRow:
    entity_type: str
    entity_id: str
    status: str
    type_label: str
    period_start: str | None
    period_end: str | None
    body: str
    document_id: str | None = None
    line_start: int | None = None
    context: bool = False

    @property
    def period(self) -> str:
        if self.period_start and self.period_end:
            return f"{self.period_start}..{self.period_end}"
        if self.period_start:
            return f"{self.period_start}.."
        return "-"


def _one_line(text: str) -> str:
    return " ".join(str(text).split())


def _parse_day(value: str | None, field: str) -> str | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise UsageProblem(f"{field} must be YYYY-MM-DD, got {value!r}") from exc


def _like(text: str) -> str:
    escaped = (
        text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )
    return f"%{escaped}%"


def _placeholders(values: Sequence[object]) -> str:
    return ", ".join("?" for _ in values)


def _default_statuses(
    entity_type: str, include_draft: bool
) -> tuple[str, ...] | None:
    if entity_type in {"claim", "card"}:
        if include_draft:
            return ("active", "draft", "rejected", "stale")
        return ("active",)
    if entity_type in {"block", "prose", "structured"}:
        if include_draft:
            return ("active", "stale", "missing")
        return ("active",)
    # Reports have no promotion workflow in v1: every report is normally
    # draft, so filtering by active by default would hide everything.
    return None


def query_items(
    conn: sqlite3.Connection,
    *,
    entity_type: str = "claim",
    date_from: str | None = None,
    date_to: str | None = None,
    status: str | None = None,
    claim_type: str | None = None,
    grep: str | None = None,
    include_draft: bool = False,
    limit: int = DEFAULT_LIMIT,
    context: int = 0,
) -> list[QueryRow]:
    if entity_type not in QUERY_ENTITY_TYPES:
        raise UsageProblem(
            "--type must be claim, card, report, block, prose or structured"
        )
    if limit < 1:
        raise UsageProblem("--limit must be at least 1")
    if context < 0:
        raise UsageProblem("--context must be zero or greater")
    if context and entity_type not in {"block", "prose", "structured"}:
        raise UsageProblem("--context is only valid for block queries")
    if status is not None and status not in QUERY_STATUSES:
        raise UsageProblem(f"unknown status {status!r}")
    if claim_type is not None:
        if claim_type not in CLAIM_TYPES:
            raise UsageProblem(f"unknown claim type {claim_type!r}")
        if entity_type not in {"claim", "card"}:
            raise UsageProblem("--claim-type is only valid for claims and cards")

    from_day = _parse_day(date_from, "--from")
    to_day = _parse_day(date_to, "--to")
    if from_day is not None and to_day is not None and from_day > to_day:
        raise UsageProblem("--from must not be later than --to")

    statuses: tuple[str, ...] | None
    if status is not None:
        statuses = (status,)
    else:
        statuses = _default_statuses(entity_type, include_draft)

    params: list[object] = []

    if entity_type in {"claim", "card"}:
        if entity_type == "claim":
            sql = [
                "SELECT id, status, claim_type AS type_label,",
                "period_start, period_end, body AS body FROM claims WHERE 1=1",
            ]
        else:
            sql = [
                "SELECT id, status, COALESCE(card_type, '') AS type_label,",
                "period_start, period_end, body AS body FROM cards WHERE 1=1",
            ]
        if statuses is not None:
            sql.append(f"AND status IN ({_placeholders(statuses)})")
            params.extend(statuses)
        if claim_type is not None:
            sql.append("AND claim_type = ?")
            params.append(claim_type)
        if from_day is not None:
            sql.append("AND period_start IS NOT NULL AND period_end > ?")
            params.append(from_day)
        if to_day is not None:
            sql.append("AND period_start IS NOT NULL AND period_start < ?")
            params.append(to_day)
        if grep:
            if entity_type == "card":
                sql.append("AND (title LIKE ? ESCAPE '\\' OR body LIKE ? ESCAPE '\\')")
                params.extend([_like(grep), _like(grep)])
            else:
                sql.append("AND body LIKE ? ESCAPE '\\'")
                params.append(_like(grep))
        sql.append("ORDER BY period_start, id LIMIT ?")
        params.append(limit)
        rows = conn.execute(" ".join(sql), params).fetchall()
        return [
            QueryRow(
                entity_type=entity_type,
                entity_id=str(row["id"]),
                status=str(row["status"]),
                type_label=str(row["type_label"] or ""),
                period_start=row["period_start"],
                period_end=row["period_end"],
                body=str(row["body"]),
            )
            for row in rows
        ]

    if entity_type == "report":
        sql = [
            "SELECT id, status, report_type AS type_label,",
            "period_start, period_end, title AS body FROM reports WHERE 1=1",
        ]
        if statuses is not None:
            sql.append(f"AND status IN ({_placeholders(statuses)})")
            params.extend(statuses)
        if from_day is not None:
            sql.append("AND period_start IS NOT NULL AND period_end > ?")
            params.append(from_day)
        if to_day is not None:
            sql.append("AND period_start IS NOT NULL AND period_start < ?")
            params.append(to_day)
        if grep:
            sql.append("AND title LIKE ? ESCAPE '\\'")
            params.append(_like(grep))
        sql.append("ORDER BY period_start, id LIMIT ?")
        params.append(limit)
        rows = conn.execute(" ".join(sql), params).fetchall()
        return [
            QueryRow(
                entity_type="report",
                entity_id=str(row["id"]),
                status=str(row["status"]),
                type_label=str(row["type_label"]),
                period_start=row["period_start"],
                period_end=row["period_end"],
                body=str(row["body"]),
            )
            for row in rows
        ]

    retrieval_type: str | None = None
    if entity_type in {"prose", "structured"}:
        retrieval_type = entity_type
    sql = [
        "SELECT id, status, retrieval_type AS type_label,",
        "snapshot_text AS body, document_id, line_start",
        "FROM blocks WHERE 1=1",
    ]
    if statuses is not None:
        sql.append(f"AND status IN ({_placeholders(statuses)})")
        params.extend(statuses)
    if retrieval_type is not None:
        sql.append("AND retrieval_type = ?")
        params.append(retrieval_type)
    if grep:
        sql.append("AND snapshot_text LIKE ? ESCAPE '\\'")
        params.append(_like(grep))
    sql.append("ORDER BY document_id, line_start, id LIMIT ?")
    params.append(limit)
    primary = conn.execute(" ".join(sql), params).fetchall()

    rows = [
        QueryRow(
            entity_type="block",
            entity_id=str(row["id"]),
            status=str(row["status"]),
            type_label=str(row["type_label"]),
            period_start=None,
            period_end=None,
            body=str(row["body"]),
            document_id=str(row["document_id"]),
            line_start=int(row["line_start"]),
        )
        for row in primary
    ]
    if context and rows:
        rows = _with_block_context(conn, rows, context)
    return rows


def _with_block_context(
    conn: sqlite3.Connection, primary: Sequence[QueryRow], context: int
) -> list[QueryRow]:
    primary_ids = {row.entity_id for row in primary}
    documents: dict[str, list[QueryRow]] = {}
    for row in primary:
        documents.setdefault(str(row.document_id), []).append(row)

    collected: dict[str, QueryRow] = {}
    for document_id, primary_rows in documents.items():
        all_rows = conn.execute(
            """
            SELECT id, status, retrieval_type AS type_label,
                   snapshot_text AS body, document_id, line_start
            FROM blocks
            WHERE document_id = ?
            ORDER BY line_start, id
            """,
            (document_id,),
        ).fetchall()
        index_by_id = {
            str(row["id"]): index for index, row in enumerate(all_rows)
        }
        for row in primary_rows:
            center = index_by_id[row.entity_id]
            start = max(0, center - context)
            end = min(len(all_rows), center + context + 1)
            for neighbour in all_rows[start:end]:
                neighbour_id = str(neighbour["id"])
                if neighbour_id in collected:
                    continue
                collected[neighbour_id] = QueryRow(
                    entity_type="block",
                    entity_id=neighbour_id,
                    status=str(neighbour["status"]),
                    type_label=str(neighbour["type_label"]),
                    period_start=None,
                    period_end=None,
                    body=str(neighbour["body"]),
                    document_id=str(neighbour["document_id"]),
                    line_start=int(neighbour["line_start"]),
                    context=neighbour_id not in primary_ids,
                )
    return sorted(
        collected.values(),
        key=lambda item: (item.document_id or "", item.line_start or 0),
    )


def format_query_rows(rows: Sequence[QueryRow]) -> str:
    if not rows:
        return "0 results\n"
    lines: list[str] = []
    for row in rows:
        prefix = "  " if row.context else ""
        lines.append(
            f"{prefix}{row.entity_id}  {row.status}  {row.period}  "
            f"{row.type_label}  {_one_line(row.body)}"
        )
    count = sum(1 for row in rows if not row.context)
    lines.append(f"{count} results")
    return "\n".join(lines) + "\n"
