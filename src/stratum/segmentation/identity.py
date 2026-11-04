"""Deterministic block identity.

A block id is derived from the document id, profile, selector type, anchor and
an occurrence counter. Line numbers are deliberately excluded: they are a
location hint, not identity.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from ..markdown import normalize_anchor, normalize_relative_path, normalize_text
from .selectors import Selection

__all__ = [
    "BlockDraft",
    "anchor_from_text",
    "build_block_drafts",
    "compute_block_id",
    "document_id",
    "snapshot_sha256",
]


@dataclass(frozen=True)
class BlockDraft:
    """A block ready to be persisted by the ingest layer."""

    doc_id: str
    block_id: str
    profile_id: str
    selector_type: str
    anchor: str
    occurrence: int
    line_start: int
    line_end: int
    snapshot_text: str
    snapshot_sha256: str
    retrieval_type: str = "structured"
    status: str = "active"
    legacy_block_id: str | None = None
    parent_block_id: str | None = None
    source_sha256: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


def document_id(
    frontmatter_values: Mapping[str, Any],
    relative_path: str | None,
) -> str:
    """Return the document id: frontmatter id first, else normalised path."""

    raw_id = frontmatter_values.get("id")
    if raw_id is not None:
        text = str(raw_id).strip()
        if text:
            return text
    return normalize_relative_path(relative_path or "")


def anchor_from_text(text: str, prefix: str = "text") -> str:
    """Build a start/end text anchor that survives middle edits."""

    normalized = normalize_text(text)
    lines = [
        normalize_anchor(line)
        for line in normalized.split("\n")
        if normalize_anchor(line)
    ]
    if not lines:
        return f"{prefix}:"
    return f"{prefix}:{lines[0][:80]}|{lines[-1][:80]}"


def snapshot_sha256(text: str) -> str:
    """Hash a snapshot after BOM and line-ending normalisation."""

    return hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()


def compute_block_id(
    doc_id: str,
    profile_id: str,
    selector_type: str,
    anchor: str,
    occurrence: int,
) -> str:
    payload = (
        f"{doc_id}{profile_id}{selector_type}"
        f"{normalize_anchor(anchor)}{int(occurrence)}"
    )
    return "BLK-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _legacy_value(
    legacy_block_ids: Mapping[Any, str] | Sequence[str | None] | None,
    *,
    index: int,
    selector_type: str,
    anchor: str,
    occurrence: int,
    sha: str,
) -> str | None:
    if legacy_block_ids is None:
        return None
    if isinstance(legacy_block_ids, Mapping):
        candidates: tuple[Any, ...] = (
            (selector_type, anchor, occurrence),
            (anchor, occurrence),
            (selector_type, anchor),
            anchor,
            sha,
            index,
        )
        for key in candidates:
            try:
                if key in legacy_block_ids:
                    return legacy_block_ids[key]
            except TypeError:
                continue
        return None
    if isinstance(legacy_block_ids, Sequence) and not isinstance(
        legacy_block_ids, (str, bytes)
    ):
        if index < len(legacy_block_ids):
            return legacy_block_ids[index]
    return None


def build_block_drafts(
    doc_id: str,
    profile_id: str,
    selections: Sequence[Selection],
    *,
    retrieval_type: str = "structured",
    legacy_block_ids: Mapping[Any, str] | Sequence[str | None] | None = None,
    source_sha256: str | None = None,
) -> list[BlockDraft]:
    """Assign deterministic identities to selected blocks."""

    occurrences: dict[tuple[str, str], int] = {}
    used_legacy: set[str] = set()
    drafts: list[BlockDraft] = []

    for index, selection in enumerate(selections):
        selector_type = selection.selector_type
        anchor = normalize_anchor(selection.anchor or anchor_from_text(selection.text))
        key = (selector_type, anchor)
        occurrence = occurrences.get(key, 0)
        occurrences[key] = occurrence + 1

        snapshot_text = normalize_text(selection.text)
        sha = snapshot_sha256(snapshot_text)
        legacy = _legacy_value(
            legacy_block_ids,
            index=index,
            selector_type=selector_type,
            anchor=anchor,
            occurrence=occurrence,
            sha=sha,
        )
        if legacy is not None:
            if legacy in used_legacy:
                raise ValueError(
                    f"legacy block id {legacy!r} is reused by more than one block"
                )
            used_legacy.add(legacy)

        metadata = dict(selection.metadata)
        if selection.level is not None:
            metadata.setdefault("level", selection.level)

        drafts.append(
            BlockDraft(
                doc_id=doc_id,
                block_id=compute_block_id(
                    doc_id, profile_id, selector_type, anchor, occurrence
                ),
                profile_id=profile_id,
                selector_type=selector_type,
                anchor=anchor,
                occurrence=occurrence,
                line_start=selection.line_start,
                line_end=selection.line_end,
                snapshot_text=snapshot_text,
                snapshot_sha256=sha,
                retrieval_type=retrieval_type,
                legacy_block_id=legacy,
                source_sha256=source_sha256,
                metadata=metadata,
            )
        )
    return drafts
