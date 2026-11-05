"""Read-only text queries over claims, cards, reports and blocks.

The default claim/card/block view is active content. ``--include-draft``
opens the review backlog for claims and cards (draft, rejected, stale) and
stale/missing blocks for block queries. An explicit ``--status`` always
wins. ``--follow`` adds one hop of claim/card/report relations; ``--sort
relevance`` puts primary hits first, then causal relations, then time. No FTS
table is required: ``--grep`` is a parameterised SQL LIKE.
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
SORT_MODES = ("time", "relevance")
RELATION_PRIORITY = {
    "triggered_by": 0,
    "derived_from": 1,
    "supported_by": 2,
    "contradicts": 3,
    "supersedes": 4,
    "precedes": 5,
    "same_day": 6,
    "state_of": 7,
}


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
    relation_type: str | None = None
    related_to: str | None = None

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
    follow: bool = False,
    sort: str = "time",
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
    if follow and entity_type in {"block", "prose", "structured"}:
        raise UsageProblem("--follow is only valid for claim, card or report queries")
    if sort not in SORT_MODES:
        raise UsageProblem("--sort must be time or relevance")
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
        result = [
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
        if follow and result:
            if entity_type == "claim":
                result = _follow_claim_rows(conn, result)
            else:
                result = _follow_card_rows(conn, result)
        if sort == "relevance":
            result = _sort_relevance(result)
        return result

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
        result = [
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
        if follow and result:
            result = _follow_report_rows(conn, result)
        if sort == "relevance":
            result = _sort_relevance(result)
        return result

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


def _sort_relevance(rows: Sequence[QueryRow]) -> list[QueryRow]:
    def key(row: QueryRow) -> tuple[int, int, str, str]:
        if not row.context:
            return (0, 0, row.period_start or "", row.entity_id)
        if row.relation_type and row.relation_type.startswith("report_card:"):
            return (2, 0, row.period_start or "", row.entity_id)
        return (
            1,
            RELATION_PRIORITY.get(row.relation_type or "", 99),
            row.period_start or "",
            row.entity_id,
        )

    return sorted(rows, key=key)


def _follow_targets(
    conn: sqlite3.Connection,
    table: str,
    from_column: str,
    to_column: str,
    relation_column: str,
    primary_ids: set[str],
) -> dict[str, tuple[str, str]]:
    placeholders = _placeholders(sorted(primary_ids))
    rows = conn.execute(
        f"""
        SELECT {from_column} AS source, {to_column} AS target,
               {relation_column} AS relation
        FROM {table}
        WHERE {from_column} IN ({placeholders})
           OR {to_column} IN ({placeholders})
        """,
        (*sorted(primary_ids), *sorted(primary_ids)),
    ).fetchall()
    targets: dict[str, tuple[str, str]] = {}
    for row in rows:
        source = str(row["source"])
        target = str(row["target"])
        if source in primary_ids and target not in primary_ids:
            targets.setdefault(target, (str(row["relation"]), source))
        elif target in primary_ids and source not in primary_ids:
            targets.setdefault(source, (str(row["relation"]), target))
    return targets


def _follow_card_rows(
    conn: sqlite3.Connection, primary: Sequence[QueryRow]
) -> list[QueryRow]:
    primary_ids = {row.entity_id for row in primary}
    targets = _follow_targets(
        conn,
        "card_relations",
        "from_card_id",
        "to_card_id",
        "relation_type",
        primary_ids,
    )
    if not targets:
        return list(primary)
    placeholders = _placeholders(sorted(targets))
    rows = conn.execute(
        f"""
        SELECT id, status, COALESCE(card_type, '') AS type_label,
               period_start, period_end, body
        FROM cards WHERE id IN ({placeholders})
        ORDER BY period_start, id
        """,
        tuple(sorted(targets)),
    ).fetchall()
    followed = [
        QueryRow(
            entity_type="card",
            entity_id=str(row["id"]),
            status=str(row["status"]),
            type_label=str(row["type_label"] or ""),
            period_start=row["period_start"],
            period_end=row["period_end"],
            body=str(row["body"]),
            context=True,
            relation_type=targets[str(row["id"])][0],
            related_to=targets[str(row["id"])][1],
        )
        for row in rows
    ]
    return [*primary, *followed]


def _follow_claim_rows(
    conn: sqlite3.Connection, primary: Sequence[QueryRow]
) -> list[QueryRow]:
    primary_ids = {row.entity_id for row in primary}
    targets = _follow_targets(
        conn,
        "claim_relations",
        "from_claim_id",
        "to_claim_id",
        "relation_type",
        primary_ids,
    )
    if not targets:
        return list(primary)
    placeholders = _placeholders(sorted(targets))
    rows = conn.execute(
        f"""
        SELECT id, status, claim_type AS type_label,
               period_start, period_end, body
        FROM claims WHERE id IN ({placeholders})
        ORDER BY period_start, id
        """,
        tuple(sorted(targets)),
    ).fetchall()
    followed = [
        QueryRow(
            entity_type="claim",
            entity_id=str(row["id"]),
            status=str(row["status"]),
            type_label=str(row["type_label"]),
            period_start=row["period_start"],
            period_end=row["period_end"],
            body=str(row["body"]),
            context=True,
            relation_type=targets[str(row["id"])][0],
            related_to=targets[str(row["id"])][1],
        )
        for row in rows
    ]
    return [*primary, *followed]


def _follow_report_rows(
    conn: sqlite3.Connection, primary: Sequence[QueryRow]
) -> list[QueryRow]:
    primary_ids = {row.entity_id for row in primary}
    card_targets: dict[str, tuple[str, str]] = {}
    report_targets: dict[str, tuple[str, str]] = {}
    placeholders = _placeholders(sorted(primary_ids))
    card_rows = conn.execute(
        f"""
        SELECT report_id, card_id, role FROM report_cards
        WHERE report_id IN ({placeholders})
        ORDER BY card_id
        """,
        tuple(sorted(primary_ids)),
    ).fetchall()
    for row in card_rows:
        card_targets.setdefault(
            str(row["card_id"]), (f"report_card:{row['role']}", str(row["report_id"]))
        )
    report_rows = conn.execute(
        f"""
        SELECT from_report_id, to_report_id, relation_type
        FROM report_relations
        WHERE from_report_id IN ({placeholders})
           OR to_report_id IN ({placeholders})
        """,
        (*sorted(primary_ids), *sorted(primary_ids)),
    ).fetchall()
    for row in report_rows:
        source = str(row["from_report_id"])
        target = str(row["to_report_id"])
        if source in primary_ids and target not in primary_ids:
            report_targets.setdefault(
                target, (str(row["relation_type"]), source)
            )
        elif target in primary_ids and source not in primary_ids:
            report_targets.setdefault(
                source, (str(row["relation_type"]), target)
            )

    followed: list[QueryRow] = []
    if card_targets:
        ids = _placeholders(sorted(card_targets))
        rows = conn.execute(
            f"""
            SELECT id, status, COALESCE(card_type, '') AS type_label,
                   period_start, period_end, body
            FROM cards WHERE id IN ({ids})
            ORDER BY period_start, id
            """,
            tuple(sorted(card_targets)),
        ).fetchall()
        for row in rows:
            card_id = str(row["id"])
            relation, source = card_targets[card_id]
            followed.append(
                QueryRow(
                    entity_type="card",
                    entity_id=card_id,
                    status=str(row["status"]),
                    type_label=str(row["type_label"] or ""),
                    period_start=row["period_start"],
                    period_end=row["period_end"],
                    body=str(row["body"]),
                    context=True,
                    relation_type=relation,
                    related_to=source,
                )
            )
    if report_targets:
        ids = _placeholders(sorted(report_targets))
        rows = conn.execute(
            f"""
            SELECT id, status, report_type AS type_label,
                   period_start, period_end, title AS body
            FROM reports WHERE id IN ({ids})
            ORDER BY period_start, id
            """,
            tuple(sorted(report_targets)),
        ).fetchall()
        for row in rows:
            report_id = str(row["id"])
            relation, source = report_targets[report_id]
            followed.append(
                QueryRow(
                    entity_type="report",
                    entity_id=report_id,
                    status=str(row["status"]),
                    type_label=str(row["type_label"]),
                    period_start=row["period_start"],
                    period_end=row["period_end"],
                    body=str(row["body"]),
                    context=True,
                    relation_type=relation,
                    related_to=source,
                )
            )
    return [*primary, *followed]


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
        suffix = ""
        if row.context and row.relation_type:
            suffix = f"  via {row.relation_type} from {row.related_to}"
        lines.append(
            f"{prefix}{row.entity_id}  {row.status}  {row.period}  "
            f"{row.type_label}  {_one_line(row.body)}{suffix}"
        )
    count = sum(1 for row in rows if not row.context)
    lines.append(f"{count} results")
    return "\n".join(lines) + "\n"
