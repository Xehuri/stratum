"""Claim contract: parsing, validation, fingerprints, evidence and relations.

This module implements the frozen ``contracts/claim-schema.md`` rules that
W3 needs: field syntax, identifier and scale checks, evidence resolution,
layer validation and acyclic relation checks. It performs no I/O outside the
passed SQLite connection and never writes Markdown.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from typing import Mapping, Sequence

from .errors import ValidationError
from .migrations import utcnow

__all__ = [
    "CLAIM_TYPES",
    "ClaimDraft",
    "ClaimId",
    "EvidenceTarget",
    "REPORT_GRANULARITY",
    "REPORT_SCALE",
    "claim_relations_for",
    "delete_claim_relations",
    "delete_evidence",
    "detect_cycles",
    "draft_from_fields",
    "fingerprint_fields",
    "insert_claim_row",
    "insert_evidence_link",
    "insert_relation",
    "parse_claim_id",
    "parse_field_lines",
    "resolve_evidence",
    "update_claim_row",
    "validate_claim_relation",
    "validate_relations",
]

CLAIM_TYPES = (
    "observation",
    "change",
    "finding",
    "pattern",
    "hypothesis",
)
CONFIDENCE_VALUES = ("high", "medium", "low")
GRANULARITY_VALUES = ("instant", "day", "week", "month", "range")
RELATION_TYPES = ("derived_from", "supersedes", "contradicts")

REPORT_SCALE = {"daily": "D", "weekly": "W", "biweekly": "B"}
REPORT_GRANULARITY = {"daily": "day", "weekly": "week", "biweekly": "range"}
TYPE_SCALE = {"D": "day", "W": "week", "B": "range"}
TYPE_PREFIX = {
    "observation": "OBS",
    "change": "CHG",
    "finding": "FND",
    "pattern": "PAT",
    "hypothesis": "HYP",
}
PREFIX_TYPE = {value: key for key, value in TYPE_PREFIX.items()}
TYPE_ALLOWED_SCALES = {
    "observation": frozenset({"D", "W"}),
    "change": frozenset({"D"}),
    "finding": frozenset({"D"}),
    "pattern": frozenset({"B"}),
    "hypothesis": frozenset({"B"}),
}
REPORT_ALLOWED_TYPES = {
    "daily": frozenset({"observation", "change", "finding"}),
    "weekly": frozenset({"observation"}),
    "biweekly": frozenset({"pattern", "hypothesis"}),
}
LAYER_BY_SCALE = {"D": 0, "W": 1, "B": 2}

_LIST_FIELDS = frozenset(
    {"entities", "evidence", "derived_from", "supersedes", "contradicts",
     "counterexample_cards"}
)
_COMMON_FIELDS = frozenset(
    {
        "claim_id",
        "claim_type",
        "period_start",
        "period_end",
        "granularity",
        "entities",
        "evidence",
        "confidence",
        "body",
        "data_gap",
        "derived_from",
        "supersedes",
        "contradicts",
        "metric",
        "baseline",
        "delta",
        "sample_size",
    }
)
_TYPE_FIELDS = {
    "observation": _COMMON_FIELDS,
    "change": _COMMON_FIELDS | {"outcome"},
    "finding": _COMMON_FIELDS,
    "pattern": _COMMON_FIELDS
    | {
        "conditions",
        "outcome",
        "support_count",
        "opportunity_count",
        "counterexample_count",
        "counterexample_cards",
        "counterexample_note",
        "lag",
    },
    "hypothesis": _COMMON_FIELDS
    | {
        "conditions",
        "outcome",
        "support_count",
        "opportunity_count",
        "counterexample_count",
        "counterexample_cards",
        "counterexample_note",
        "lag",
    },
}

_CLAIM_ID_RE = re.compile(
    r"^(OBS|CHG|FND|PAT|HYP)-([DWB])-(\d{4}-\d{2}-\d{2})-(\d{2})$"
)
_FIELD_KEY_RE = re.compile(r"^[a-z0-9_]+$")
_BLOCK_REF_RE = re.compile(
    r"^(BLK-[A-Za-z0-9_-]+)(?:#sha256:([0-9a-f]{64}))?$"
)
_SNAPSHOT_RE = re.compile(r"^snapshot:sha256:([0-9a-f]{64})$")
_CARD_REF_RE = re.compile(r"^C-[A-Za-z0-9_-]+$")


# ---------------------------------------------------------------------------
# Identifiers and fingerprints
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClaimId:
    prefix: str
    scale: str
    period: str
    number: str

    @property
    def claim_type(self) -> str:
        return PREFIX_TYPE[self.prefix]

    @property
    def layer(self) -> int:
        return LAYER_BY_SCALE[self.scale]


def parse_claim_id(value: str) -> ClaimId:
    match = _CLAIM_ID_RE.fullmatch(value)
    if match is None:
        raise ValidationError(
            "CLAIM_ID_INVALID",
            f"claim id {value!r} must look like PREFIX-SCALE-YYYY-MM-DD-NN",
        )
    if match.group(4) == "00":
        raise ValidationError(
            "CLAIM_ID_INVALID", f"claim id {value!r} must use sequence 01..99"
        )
    return ClaimId(
        prefix=match.group(1),
        scale=match.group(2),
        period=match.group(3),
        number=match.group(4),
    )


def _short_hash(*parts: object) -> str:
    joined = "\x00".join(str(part) for part in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


def fingerprint_fields(fields: Mapping[str, Sequence[str]]) -> str:
    """Hash parsed fields: sorted keys, document order within repeated keys."""

    pairs: list[list[str]] = []
    for key in sorted(fields):
        for value in fields[key]:
            pairs.append([key, value])
    payload = json.dumps(pairs, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Field parsing
# ---------------------------------------------------------------------------


def parse_field_lines(lines: Sequence[str], *, where: str) -> dict[str, tuple[str, ...]]:
    """Parse ``- key: value`` lines without interpreting any field."""

    fields: dict[str, list[str]] = {}
    for raw in lines:
        if not raw.startswith("- "):
            raise ValidationError(
                "CLAIM_FIELD_INVALID",
                f"{where}: field lines must start with '- '",
            )
        payload = raw[2:]
        if ": " not in payload:
            raise ValidationError(
                "CLAIM_FIELD_INVALID",
                f"{where}: field line must use ': ' between key and value",
            )
        key, value = payload.split(": ", 1)
        if not _FIELD_KEY_RE.fullmatch(key):
            raise ValidationError(
                "CLAIM_FIELD_INVALID",
                f"{where}: invalid field name {key!r}",
            )
        if not value:
            raise ValidationError(
                "CLAIM_FIELD_INVALID",
                f"{where}: field {key!r} has an empty value",
            )
        if value != value.strip():
            raise ValidationError(
                "CLAIM_FIELD_INVALID",
                f"{where}: field {key!r} value has leading or trailing whitespace",
            )
        fields.setdefault(key, []).append(value)
    return {key: tuple(values) for key, values in fields.items()}


def _single(fields: Mapping[str, Sequence[str]], key: str, where: str) -> str | None:
    values = fields.get(key)
    if values is None:
        return None
    if len(values) > 1:
        if key in _LIST_FIELDS:
            return None
        raise ValidationError(
            "CLAIM_FIELD_DUPLICATE",
            f"{where}: field {key!r} must appear once",
        )
    return values[0]


def _require(
    fields: Mapping[str, Sequence[str]], key: str, where: str
) -> str:
    value = _single(fields, key, where)
    if value is None or value == "":
        raise ValidationError(
            "CLAIM_FIELD_MISSING", f"{where}: missing required field {key!r}"
        )
    return value


def _as_int(value: str, key: str, where: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValidationError(
            "CLAIM_FIELD_INVALID", f"{where}: {key} must be an integer"
        ) from exc
    if parsed < 0:
        raise ValidationError(
            "CLAIM_FIELD_INVALID", f"{where}: {key} must be >= 0"
        )
    return parsed


def _date(value: str, key: str, where: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError(
            "CLAIM_FIELD_INVALID", f"{where}: {key} must be YYYY-MM-DD"
        ) from exc


# ---------------------------------------------------------------------------
# Claim drafts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClaimDraft:
    claim_id: str
    claim_type: str
    period_start: str
    period_end: str
    granularity: str
    entities: tuple[str, ...]
    confidence: str
    body: str
    evidence: tuple[str, ...] = ()
    data_gap: str | None = None
    derived_from: tuple[str, ...] = ()
    supersedes: tuple[str, ...] = ()
    contradicts: tuple[str, ...] = ()
    metric: str | None = None
    baseline: str | None = None
    delta: str | None = None
    sample_size: int | None = None
    conditions: str | None = None
    outcome: str | None = None
    support_count: int | None = None
    opportunity_count: int | None = None
    counterexample_count: int | None = None
    counterexample_note: str | None = None
    counterexample_cards: tuple[str, ...] = ()
    lag: str | None = None
    fields: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    report_id: str | None = None
    author: str | None = None
    content_sha256: str = ""

    @property
    def identifier(self) -> ClaimId:
        return parse_claim_id(self.claim_id)


def _check_claim_scale(
    claim_id: ClaimId,
    claim_type: str,
    granularity: str,
    *,
    report_type: str | None,
    where: str,
) -> None:
    expected_prefix = TYPE_PREFIX.get(claim_type)
    if expected_prefix is None:
        raise ValidationError(
            "CLAIM_TYPE_INVALID", f"{where}: unknown claim_type {claim_type!r}"
        )
    if claim_id.prefix != expected_prefix:
        raise ValidationError(
            "CLAIM_SCALE_INVALID",
            f"{where}: claim id prefix {claim_id.prefix!r} does not match "
            f"claim_type {claim_type!r}",
        )
    if claim_id.scale not in TYPE_ALLOWED_SCALES[claim_type]:
        raise ValidationError(
            "CLAIM_SCALE_INVALID",
            f"{where}: {claim_type} cannot use scale {claim_id.scale}",
        )
    if report_type is not None:
        if claim_id.scale != REPORT_SCALE[report_type]:
            raise ValidationError(
                "CLAIM_SCALE_INVALID",
                f"{where}: {report_type} claims must use scale "
                f"{REPORT_SCALE[report_type]}",
            )
    expected_granularity = TYPE_SCALE[claim_id.scale]
    if granularity != expected_granularity:
        raise ValidationError(
            "CLAIM_SCALE_INVALID",
            f"{where}: scale {claim_id.scale} requires granularity "
            f"{expected_granularity!r}",
        )


def draft_from_fields(
    fields: Mapping[str, Sequence[str]],
    *,
    heading_id: str | None = None,
    report_type: str | None = None,
    report_period: tuple[str, str] | None = None,
    report_id: str | None = None,
    author: str | None = None,
    require_type_fields: bool = True,
    where: str = "claim",
) -> ClaimDraft:
    """Validate parsed field values and return a typed draft."""

    for key, values in fields.items():
        for value in values:
            if value and (
                value != value.strip()
                or "\n" in value
                or "\r" in value
            ):
                raise ValidationError(
                    "CLAIM_FIELD_INVALID",
                    f"{where}: field {key!r} must be a single trimmed line",
                )

    if "status" in fields:
        raise ValidationError(
            "STATUS_AUTHORED", f"{where}: status is managed by Stratum"
        )

    claim_type_raw = _single(fields, "claim_type", where) or ""
    if claim_type_raw not in CLAIM_TYPES:
        if claim_type_raw:
            raise ValidationError(
                "CLAIM_TYPE_INVALID",
                f"{where}: unknown claim_type {claim_type_raw!r}",
            )
        raise ValidationError(
            "CLAIM_FIELD_MISSING", f"{where}: missing required field 'claim_type'"
        )

    allowed = _TYPE_FIELDS[claim_type_raw]
    for key in fields:
        if key == "status":
            continue
        if key not in allowed:
            raise ValidationError(
                "CLAIM_FIELD_UNKNOWN",
                f"{where}: field {key!r} is not allowed for {claim_type_raw}",
            )

    required_common = (
        "claim_id",
        "period_start",
        "period_end",
        "granularity",
        "confidence",
        "body",
    )
    for key in required_common:
        _require(fields, key, where)

    claim_id_text = _require(fields, "claim_id", where)
    if heading_id is not None and heading_id != claim_id_text:
        raise ValidationError(
            "CLAIM_ID_INVALID",
            f"{where}: heading id {heading_id!r} does not match field "
            f"claim_id {claim_id_text!r}",
        )
    claim_id = parse_claim_id(claim_id_text)
    if claim_id.claim_type != claim_type_raw:
        raise ValidationError(
            "CLAIM_SCALE_INVALID",
            f"{where}: claim id prefix {claim_id.prefix!r} maps to "
            f"{claim_id.claim_type!r}, not {claim_type_raw!r}",
        )

    period_start = _require(fields, "period_start", where)
    period_end = _require(fields, "period_end", where)
    start_date = _date(period_start, "period_start", where)
    end_date = _date(period_end, "period_end", where)
    if end_date <= start_date:
        raise ValidationError(
            "CLAIM_FIELD_INVALID",
            f"{where}: period_end must be later than period_start",
        )
    if claim_id.period != period_start:
        raise ValidationError(
            "CLAIM_ID_INVALID",
            f"{where}: claim id date must equal period_start",
        )
    if report_period is not None and (
        period_start != report_period[0] or period_end != report_period[1]
    ):
        raise ValidationError(
            "CLAIM_PERIOD_MISMATCH",
            f"{where}: claim period must match the report period "
            f"{report_period[0]}..{report_period[1]}",
        )

    granularity = _require(fields, "granularity", where)
    if granularity not in GRANULARITY_VALUES:
        raise ValidationError(
            "CLAIM_FIELD_INVALID",
            f"{where}: invalid granularity {granularity!r}",
        )
    _check_claim_scale(
        claim_id,
        claim_type_raw,
        granularity,
        report_type=report_type,
        where=where,
    )
    if report_type is not None and granularity != REPORT_GRANULARITY[report_type]:
        raise ValidationError(
            "CLAIM_SCALE_INVALID",
            f"{where}: {report_type} requires granularity "
            f"{REPORT_GRANULARITY[report_type]!r}",
        )
    if report_type is not None and claim_type_raw not in REPORT_ALLOWED_TYPES[report_type]:
        raise ValidationError(
            "CLAIM_TYPE_INVALID",
            f"{where}: {report_type} cannot contain {claim_type_raw}",
        )

    entities = fields.get("entities", ())
    if not entities:
        raise ValidationError(
            "CLAIM_FIELD_MISSING", f"{where}: entities requires at least one value"
        )
    for entity in entities:
        if not 1 <= len(entity) <= 80:
            raise ValidationError(
                "CLAIM_FIELD_INVALID",
                f"{where}: entity must be 1..80 characters",
            )

    confidence = _require(fields, "confidence", where)
    if confidence not in CONFIDENCE_VALUES:
        raise ValidationError(
            "CLAIM_FIELD_INVALID",
            f"{where}: confidence must be high, medium or low",
        )
    if claim_type_raw == "hypothesis" and confidence == "high":
        raise ValidationError(
            "CLAIM_CONFIDENCE_INVALID",
            f"{where}: hypothesis confidence cannot be high",
        )

    body = _require(fields, "body", where)
    data_gap = _single(fields, "data_gap", where)
    evidence = fields.get("evidence", ())
    if not evidence and data_gap is None:
        raise ValidationError(
            "EVIDENCE_MISSING", f"{where}: evidence or data_gap is required"
        )

    derived_from = fields.get("derived_from", ())
    supersedes = fields.get("supersedes", ())
    contradicts = fields.get("contradicts", ())
    if report_type == "daily" and derived_from:
        raise ValidationError(
            "DERIVED_FROM_LAYER", f"{where}: daily claims cannot use derived_from"
        )
    if report_type in {"weekly", "biweekly"} and require_type_fields and not derived_from:
        raise ValidationError(
            "DERIVED_FROM_MISSING",
            f"{where}: {report_type} claims require derived_from",
        )

    sample_size_raw = _single(fields, "sample_size", where)
    sample_size = (
        _as_int(sample_size_raw, "sample_size", where)
        if sample_size_raw is not None
        else None
    )

    baseline = _single(fields, "baseline", where)
    delta = _single(fields, "delta", where)
    outcome = _single(fields, "outcome", where)
    if require_type_fields and claim_type_raw == "change":
        if baseline is None:
            raise ValidationError(
                "CLAIM_FIELD_MISSING",
                f"{where}: change claims require baseline",
            )
        if delta is None and outcome is None:
            raise ValidationError(
                "CLAIM_FIELD_MISSING",
                f"{where}: change claims require delta or outcome",
            )

    conditions = _single(fields, "conditions", where)
    support_count = opportunity_count = counterexample_count = None
    counterexample_note = _single(fields, "counterexample_note", where)
    lag = _single(fields, "lag", where)
    counterexample_cards = fields.get("counterexample_cards", ())
    if claim_type_raw in {"pattern", "hypothesis"}:
        if require_type_fields:
            for key in (
                "conditions",
                "outcome",
                "support_count",
                "opportunity_count",
                "counterexample_count",
            ):
                _require(fields, key, where)
        if "support_count" in fields:
            support_count = _as_int(
                _require(fields, "support_count", where), "support_count", where
            )
        if "opportunity_count" in fields:
            opportunity_count = _as_int(
                _require(fields, "opportunity_count", where),
                "opportunity_count",
                where,
            )
        if "counterexample_count" in fields:
            counterexample_count = _as_int(
                _require(fields, "counterexample_count", where),
                "counterexample_count",
                where,
            )
        if support_count is not None and support_count < 1:
            raise ValidationError(
                "CLAIM_FIELD_INVALID", f"{where}: support_count must be at least 1"
            )
        if (
            support_count is not None
            and opportunity_count is not None
            and support_count > opportunity_count
        ):
            raise ValidationError(
                "CLAIM_FIELD_INVALID",
                f"{where}: support_count cannot exceed opportunity_count",
            )
        if (
            opportunity_count is not None
            and counterexample_count is not None
            and support_count is not None
            and opportunity_count < support_count + counterexample_count
        ):
            raise ValidationError(
                "CLAIM_FIELD_INVALID",
                f"{where}: opportunity_count must be >= support_count + "
                "counterexample_count",
            )
        if counterexample_count is not None:
            if counterexample_count > 0 and not counterexample_cards:
                raise ValidationError(
                    "COUNTEREXAMPLE_MISSING",
                    f"{where}: counterexample_count > 0 requires "
                    "counterexample_cards",
                )
            if counterexample_count == 0 and counterexample_note is None:
                raise ValidationError(
                    "COUNTEREXAMPLE_MISSING",
                    f"{where}: counterexample_count 0 requires "
                    "counterexample_note",
                )
        if (
            claim_type_raw == "pattern"
            and confidence == "high"
            and support_count is not None
            and support_count < 3
        ):
            raise ValidationError(
                "CLAIM_CONFIDENCE_INVALID",
                f"{where}: high-confidence pattern requires support_count >= 3",
            )

    normalized_fields: dict[str, tuple[str, ...]] = {
        key: tuple(values) for key, values in fields.items()
    }
    return ClaimDraft(
        claim_id=claim_id_text,
        claim_type=claim_type_raw,
        period_start=period_start,
        period_end=period_end,
        granularity=granularity,
        entities=tuple(entities),
        confidence=confidence,
        body=body,
        evidence=tuple(evidence),
        data_gap=data_gap,
        derived_from=tuple(derived_from),
        supersedes=tuple(supersedes),
        contradicts=tuple(contradicts),
        metric=_single(fields, "metric", where),
        baseline=baseline,
        delta=delta,
        sample_size=sample_size,
        conditions=conditions,
        outcome=outcome,
        support_count=support_count,
        opportunity_count=opportunity_count,
        counterexample_count=counterexample_count,
        counterexample_note=counterexample_note,
        counterexample_cards=tuple(counterexample_cards),
        lag=lag,
        fields=normalized_fields,
        report_id=report_id,
        author=author,
        content_sha256=fingerprint_fields(normalized_fields),
    )


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvidenceTarget:
    ref: str
    target_type: str
    target_id: str
    quote_sha256: str | None = None
    role: str = "evidence"


def _block_row(conn: sqlite3.Connection, block_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM blocks WHERE id = ?", (block_id,)).fetchone()


def _intersects(
    start: int, end: int, span: tuple[int, int] | None
) -> bool:
    if span is None:
        return False
    return not (end < span[0] or start > span[1])


def _check_block_origin(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    report_document_id: str | None,
    sections: Mapping[str, tuple[int, int]] | None,
    where: str,
) -> None:
    report_row = conn.execute(
        "SELECT id FROM reports WHERE document_id = ?", (row["document_id"],)
    ).fetchone()
    if report_row is None:
        return
    span = (int(row["line_start"]), int(row["line_end"]))
    if row["document_id"] == report_document_id and sections is not None:
        if _intersects(*span, sections.get("叙事")):
            raise ValidationError(
                "NARRATIVE_AS_CLAIM",
                f"{where}: narrative blocks cannot be evidence",
            )
        if _intersects(*span, sections.get("运维")):
            raise ValidationError(
                "OPS_AS_CLAIM", f"{where}: ops blocks cannot be evidence"
            )
    raise ValidationError(
        "EVIDENCE_UNRESOLVED",
        f"{where}: blocks from report documents cannot be evidence",
    )


def _check_block(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    quote_sha256: str | None,
    report_document_id: str | None,
    sections: Mapping[str, tuple[int, int]] | None,
    where: str,
) -> EvidenceTarget:
    if row["retrieval_type"] == "prose":
        raise ValidationError(
            "EVIDENCE_PROSE", f"{where}: prose blocks cannot be evidence"
        )
    if row["status"] == "missing":
        raise ValidationError(
            "EVIDENCE_UNRESOLVED", f"{where}: block {row['id']} is missing"
        )
    if quote_sha256 is not None and quote_sha256 != row["snapshot_sha256"]:
        raise ValidationError(
            "EVIDENCE_SNAPSHOT_MISMATCH",
            f"{where}: block snapshot does not match {quote_sha256}",
        )
    _check_block_origin(
        conn,
        row,
        report_document_id=report_document_id,
        sections=sections,
        where=where,
    )
    return EvidenceTarget(
        ref=str(row["id"]),
        target_type="block",
        target_id=str(row["id"]),
        quote_sha256=quote_sha256,
    )


def resolve_evidence(
    conn: sqlite3.Connection,
    ref: str,
    *,
    report_document_id: str | None = None,
    sections: Mapping[str, tuple[int, int]] | None = None,
    where: str = "claim",
) -> EvidenceTarget:
    """Resolve one evidence reference against the workspace database."""

    if _CARD_REF_RE.fullmatch(ref):
        row = conn.execute("SELECT id FROM cards WHERE id = ?", (ref,)).fetchone()
        if row is None:
            raise ValidationError(
                "EVIDENCE_UNRESOLVED", f"{where}: card {ref} does not exist"
            )
        return EvidenceTarget(ref=ref, target_type="card", target_id=ref)

    match = _BLOCK_REF_RE.fullmatch(ref)
    if match is not None:
        block_id, quote = match.group(1), match.group(2)
        row = _block_row(conn, block_id)
        if row is None:
            raise ValidationError(
                "EVIDENCE_UNRESOLVED", f"{where}: block {block_id} does not exist"
            )
        return _check_block(
            conn,
            row,
            quote_sha256=quote,
            report_document_id=report_document_id,
            sections=sections,
            where=where,
        )

    match = _SNAPSHOT_RE.fullmatch(ref)
    if match is not None:
        digest = match.group(1)
        rows = conn.execute(
            "SELECT * FROM blocks WHERE snapshot_sha256 = ?", (digest,)
        ).fetchall()
        if len(rows) != 1:
            raise ValidationError(
                "EVIDENCE_UNRESOLVED",
                f"{where}: snapshot {digest} does not resolve to one block",
            )
        return _check_block(
            conn,
            rows[0],
            quote_sha256=None,
            report_document_id=report_document_id,
            sections=sections,
            where=where,
        )

    raise ValidationError(
        "EVIDENCE_UNRESOLVED", f"{where}: unsupported evidence reference {ref!r}"
    )


def resolve_counterexample_card(conn: sqlite3.Connection, ref: str, *, where: str) -> str:
    if not _CARD_REF_RE.fullmatch(ref):
        raise ValidationError(
            "EVIDENCE_UNRESOLVED",
            f"{where}: counterexample_cards must reference cards",
        )
    row = conn.execute("SELECT id FROM cards WHERE id = ?", (ref,)).fetchone()
    if row is None:
        raise ValidationError(
            "EVIDENCE_UNRESOLVED", f"{where}: card {ref} does not exist"
        )
    return ref


def insert_evidence_link(
    conn: sqlite3.Connection, owner_id: str, target: EvidenceTarget
) -> bool:
    link_id = "EVL-" + _short_hash(
        owner_id,
        target.target_type,
        target.target_id,
        target.role,
        target.quote_sha256,
    )
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO evidence_links (
            id, owner_type, owner_id, target_type, target_id,
            quote_sha256, role, confidence, created_at
        ) VALUES (?, 'claim', ?, ?, ?, ?, ?, NULL, ?)
        """,
        (
            link_id,
            owner_id,
            target.target_type,
            target.target_id,
            target.quote_sha256,
            target.role,
            utcnow(),
        ),
    )
    return cursor.rowcount > 0


def delete_evidence(conn: sqlite3.Connection, claim_id: str) -> None:
    conn.execute(
        "DELETE FROM evidence_links WHERE owner_type = 'claim' AND owner_id = ?",
        (claim_id,),
    )


# ---------------------------------------------------------------------------
# Relations
# ---------------------------------------------------------------------------


def _claim_row(conn: sqlite3.Connection, claim_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM claims WHERE id = ?", (claim_id,)).fetchone()


def _layer_of(claim_id_text: str, *, where: str) -> int:
    try:
        return parse_claim_id(claim_id_text).layer
    except ValidationError as exc:
        raise ValidationError(
            "CLAIM_ID_INVALID", f"{where}: target claim id is invalid: {exc.message}"
        ) from exc


def validate_relations(
    conn: sqlite3.Connection,
    draft: ClaimDraft,
    *,
    report_type: str | None,
    where: str = "claim",
) -> None:
    """Validate derived_from, supersedes and contradicts before writing."""

    layer = draft.identifier.layer

    if len(draft.supersedes) > 1:
        raise ValidationError(
            "SUPERSEDES_INVALID",
            f"{where}: a claim may supersede at most one claim",
        )

    if report_type == "daily" and draft.derived_from:
        raise ValidationError(
            "DERIVED_FROM_LAYER", f"{where}: daily claims cannot use derived_from"
        )
    if report_type in {"weekly", "biweekly"} and not draft.derived_from:
        raise ValidationError(
            "DERIVED_FROM_MISSING",
            f"{where}: {report_type} claims require derived_from",
        )

    expected_lower = {"weekly": "D", "biweekly": "W"}
    for target_id in draft.derived_from:
        if target_id == draft.claim_id:
            raise ValidationError(
                "CYCLE_DETECTED", f"{where}: derived_from cannot reference itself"
            )
        target = _claim_row(conn, target_id)
        if target is None:
            raise ValidationError(
                "DERIVED_FROM_MISSING",
                f"{where}: derived_from target {target_id} does not exist",
            )
        target_scale = parse_claim_id(target_id).scale
        if report_type in expected_lower and target_scale != expected_lower[report_type]:
            raise ValidationError(
                "DERIVED_FROM_LAYER",
                f"{where}: {report_type} may only derive from "
                f"{expected_lower[report_type]} claims",
            )
        if (
            str(target["period_start"]) < draft.period_start
            or str(target["period_end"]) > draft.period_end
        ):
            raise ValidationError(
                "DERIVED_FROM_LAYER",
                f"{where}: derived_from target {target_id} is outside the "
                "report period",
            )
        if target["status"] in {"rejected", "superseded"}:
            raise ValidationError(
                "DERIVED_FROM_INVALID",
                f"{where}: derived_from target {target_id} is "
                f"{target['status']}",
            )

    for target_id in draft.supersedes:
        if target_id == draft.claim_id:
            raise ValidationError(
                "CYCLE_DETECTED", f"{where}: supersedes cannot reference itself"
            )
        target = _claim_row(conn, target_id)
        if target is None:
            raise ValidationError(
                "SUPERSEDES_INVALID",
                f"{where}: supersedes target {target_id} does not exist",
            )
        target_id_parsed = parse_claim_id(target_id)
        if target_id_parsed.layer != layer:
            raise ValidationError(
                "SUPERSEDES_INVALID",
                f"{where}: supersedes target must be on the same layer",
            )
        if str(target["claim_type"]) != draft.claim_type:
            raise ValidationError(
                "SUPERSEDES_INVALID",
                f"{where}: supersedes target must have the same claim type",
            )
        if (
            str(target["period_start"]) > draft.period_start
            or str(target["period_end"]) > draft.period_end
        ):
            raise ValidationError(
                "SUPERSEDES_INVALID",
                f"{where}: supersedes target period is later than the new claim",
            )

    for target_id in draft.contradicts:
        if target_id == draft.claim_id:
            raise ValidationError(
                "CONTRADICTS_SELF", f"{where}: contradicts cannot reference itself"
            )
        target = _claim_row(conn, target_id)
        if target is None:
            raise ValidationError(
                "CONTRADICTS_INVALID",
                f"{where}: contradicts target {target_id} does not exist",
            )
        if _layer_of(target_id, where=where) > layer:
            raise ValidationError(
                "CONTRADICTS_INVALID",
                f"{where}: contradicts may only point to the same or a lower layer",
            )


def validate_claim_relation(
    conn: sqlite3.Connection,
    from_claim_id: str,
    to_claim_id: str,
    relation_type: str,
) -> None:
    """Validate one manually authored claim-to-claim relation.

    Report ingest validates relations against a parsed draft. The explicit
    CLI entry writes relations after both claims already exist, so it needs
    the same layer, period and status checks without a draft object.
    """

    if relation_type not in RELATION_TYPES:
        raise ValidationError(
            "RELATION_INVALID", f"unknown relation type {relation_type!r}"
        )
    if from_claim_id == to_claim_id:
        raise ValidationError(
            "RELATION_INVALID", "a claim cannot relate to itself"
        )

    from_row = _claim_row(conn, from_claim_id)
    if from_row is None:
        raise ValidationError(
            "CLAIM_NOT_FOUND", f"claim {from_claim_id} does not exist"
        )
    to_row = _claim_row(conn, to_claim_id)
    if to_row is None:
        raise ValidationError(
            "CLAIM_NOT_FOUND", f"claim {to_claim_id} does not exist"
        )

    if str(from_row["status"]) in {"rejected", "superseded"}:
        raise ValidationError(
            "RELATION_INVALID",
            f"claim {from_claim_id} is {from_row['status']} and cannot own relations",
        )
    if str(to_row["status"]) in {"rejected", "superseded"}:
        raise ValidationError(
            "RELATION_INVALID",
            f"claim {to_claim_id} is {to_row['status']} and cannot be a target",
        )

    from_id = parse_claim_id(from_claim_id)
    to_id = parse_claim_id(to_claim_id)

    if relation_type == "supersedes" and str(from_row["status"]) == "active":
        raise ValidationError(
            "RELATION_INVALID",
            "supersedes must be written before the source claim is promoted",
        )

    if relation_type == "derived_from":
        expected_scale = {"W": "D", "B": "W"}.get(from_id.scale)
        if expected_scale is None:
            raise ValidationError(
                "DERIVED_FROM_LAYER",
                "daily claims cannot use derived_from",
            )
        if to_id.scale != expected_scale:
            raise ValidationError(
                "DERIVED_FROM_LAYER",
                f"{from_id.scale} claims may only derive from "
                f"{expected_scale} claims",
            )
        if from_id.scale == "W" and str(from_row["claim_type"]) != "observation":
            raise ValidationError(
                "DERIVED_FROM_LAYER",
                "weekly derived_from sources must be observations",
            )
        if from_id.scale == "B" and str(from_row["claim_type"]) not in {
            "pattern",
            "hypothesis",
        }:
            raise ValidationError(
                "DERIVED_FROM_LAYER",
                "biweekly derived_from sources must be patterns or hypotheses",
            )
        if from_id.scale == "B" and str(to_row["claim_type"]) != "observation":
            raise ValidationError(
                "DERIVED_FROM_LAYER",
                "biweekly derived_from targets must be weekly observations",
            )
        if (
            str(from_row["status"]) == "active"
            and str(to_row["status"]) != "active"
        ):
            raise ValidationError(
                "RELATION_INVALID",
                "an active source may only derive from active claims",
            )
        if (
            str(from_row["period_start"]) > str(to_row["period_start"])
            or str(to_row["period_end"]) > str(from_row["period_end"])
        ):
            raise ValidationError(
                "DERIVED_FROM_LAYER",
                f"derived_from target {to_claim_id} is outside the source period",
            )

    elif relation_type == "supersedes":
        if str(to_row["status"]) not in {"active", "stale"}:
            raise ValidationError(
                "SUPERSEDES_INVALID",
                "supersedes target must be active or stale",
            )
        if from_id.layer != to_id.layer:
            raise ValidationError(
                "SUPERSEDES_INVALID",
                "supersedes target must be on the same layer",
            )
        if str(from_row["claim_type"]) != str(to_row["claim_type"]):
            raise ValidationError(
                "SUPERSEDES_INVALID",
                "supersedes target must have the same claim type",
            )
        if (
            str(to_row["period_start"]) > str(from_row["period_start"])
            or str(to_row["period_end"]) > str(from_row["period_end"])
        ):
            raise ValidationError(
                "SUPERSEDES_INVALID",
                "supersedes target period is later than the source claim",
            )
        existing = conn.execute(
            """
            SELECT 1 FROM claim_relations
            WHERE from_claim_id = ? AND relation_type = 'supersedes'
            """,
            (from_claim_id,),
        ).fetchone()
        if existing is not None:
            raise ValidationError(
                "SUPERSEDES_INVALID",
                f"claim {from_claim_id} already supersedes another claim",
            )

    elif relation_type == "contradicts":
        if to_id.layer > from_id.layer:
            raise ValidationError(
                "CONTRADICTS_INVALID",
                "contradicts may only point to the same or a lower layer",
            )


def insert_relation(
    conn: sqlite3.Connection,
    from_claim_id: str,
    to_claim_id: str,
    relation_type: str,
) -> bool:
    if relation_type not in RELATION_TYPES:
        raise ValidationError(
            "RELATION_INVALID", f"unknown relation type {relation_type!r}"
        )
    relation_id = "CLR-" + _short_hash(
        from_claim_id, to_claim_id, relation_type
    )
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO claim_relations (
            id, from_claim_id, to_claim_id, relation_type, created_at
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (relation_id, from_claim_id, to_claim_id, relation_type, utcnow()),
    )
    return cursor.rowcount > 0


def insert_draft_relations(conn: sqlite3.Connection, draft: ClaimDraft) -> int:
    count = 0
    for target_id in draft.derived_from:
        count += int(insert_relation(conn, draft.claim_id, target_id, "derived_from"))
    for target_id in draft.supersedes:
        count += int(insert_relation(conn, draft.claim_id, target_id, "supersedes"))
    for target_id in draft.contradicts:
        count += int(insert_relation(conn, draft.claim_id, target_id, "contradicts"))
    return count


def delete_claim_relations(conn: sqlite3.Connection, claim_id: str) -> None:
    conn.execute("DELETE FROM claim_relations WHERE from_claim_id = ?", (claim_id,))


def claim_relations_for(conn: sqlite3.Connection, claim_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM claim_relations WHERE from_claim_id = ? "
        "ORDER BY relation_type, to_claim_id",
        (claim_id,),
    ).fetchall()


def detect_cycles(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT from_claim_id, to_claim_id FROM claim_relations "
        "WHERE relation_type IN ('derived_from', 'supersedes')"
    ).fetchall()
    adjacency: dict[str, list[str]] = {}
    for row in rows:
        adjacency.setdefault(str(row["from_claim_id"]), []).append(
            str(row["to_claim_id"])
        )

    colour: dict[str, int] = {}

    def visit(node: str) -> None:
        colour[node] = 1
        for nxt in adjacency.get(node, ()):
            state = colour.get(nxt, 0)
            if state == 1:
                raise ValidationError(
                    "CYCLE_DETECTED",
                    f"relation cycle detected through {nxt}",
                )
            if state == 0:
                visit(nxt)
        colour[node] = 2

    for node in list(adjacency):
        if colour.get(node, 0) == 0:
            visit(node)


# ---------------------------------------------------------------------------
# Persistence helpers shared by report ingest and ``stratum add claim``
# ---------------------------------------------------------------------------


def _claim_values(draft: ClaimDraft, status: str) -> tuple:
    return (
        draft.claim_type,
        draft.period_start,
        draft.period_end,
        draft.granularity,
        json.dumps(list(draft.entities), ensure_ascii=False, separators=(",", ":")),
        draft.body,
        draft.confidence,
        status,
        draft.author,
        draft.content_sha256,
        draft.data_gap,
        draft.metric,
        draft.baseline,
        draft.delta,
        draft.sample_size,
        draft.conditions,
        draft.outcome,
        draft.support_count,
        draft.opportunity_count,
        draft.counterexample_count,
        draft.counterexample_note,
        draft.lag,
    )


def insert_claim_row(
    conn: sqlite3.Connection, draft: ClaimDraft, *, status: str = "draft"
) -> None:
    now = utcnow()
    conn.execute(
        """
        INSERT INTO claims (
            id, report_id, claim_type, period_start, period_end, granularity,
            entities_json, body, confidence, status, author,
            contract_version, content_sha256, data_gap, metric, baseline,
            delta, sample_size, conditions, outcome, support_count,
            opportunity_count, counterexample_count, counterexample_note,
            lag, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                  ?, ?, ?, ?, ?, ?)
        """,
        (draft.claim_id, draft.report_id) + _claim_values(draft, status) + (now, now),
    )


def update_claim_row(conn: sqlite3.Connection, draft: ClaimDraft) -> None:
    now = utcnow()
    conn.execute(
        """
        UPDATE claims SET
            claim_type = ?,
            period_start = ?,
            period_end = ?,
            granularity = ?,
            entities_json = ?,
            body = ?,
            confidence = ?,
            status = ?,
            author = ?,
            content_sha256 = ?,
            data_gap = ?,
            metric = ?,
            baseline = ?,
            delta = ?,
            sample_size = ?,
            conditions = ?,
            outcome = ?,
            support_count = ?,
            opportunity_count = ?,
            counterexample_count = ?,
            counterexample_note = ?,
            lag = ?,
            updated_at = ?
        WHERE id = ?
        """,
        _claim_values(draft, "draft")
        + (now, draft.claim_id),
    )
