"""Manual blocks: line hints plus anchors and stale detection."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Sequence

from ..markdown import ScannedDocument, normalize_anchor, normalize_text
from .identity import compute_block_id, snapshot_sha256

__all__ = [
    "ManualBlock",
    "ManualBlockError",
    "create_manual_block",
    "resolve_manual_block",
    "resolve_manual_blocks",
]


class ManualBlockError(ValueError):
    """Raised when a manual block cannot be created or resolved."""


@dataclass(frozen=True)
class ManualBlock:
    doc_id: str
    profile_id: str
    block_id: str
    line_start: int
    line_end: int
    start_anchor: str
    end_anchor: str
    snapshot_text: str
    snapshot_sha256: str
    source_sha256: str
    status: str = "active"
    parent_block_id: str | None = None
    legacy_block_id: str | None = None
    stale_reason: str | None = None

    def __post_init__(self) -> None:
        if self.status not in {"active", "stale", "missing"}:
            raise ManualBlockError(f"invalid manual block status: {self.status!r}")


def _validate_range(document: ScannedDocument, start_line: int, end_line: int) -> None:
    if not document.lines:
        raise ManualBlockError(
            f"manual block line {start_line}-{end_line}: document is empty"
        )
    if (
        not isinstance(start_line, int)
        or isinstance(start_line, bool)
        or not isinstance(end_line, int)
        or isinstance(end_line, bool)
    ):
        raise ManualBlockError("manual block lines: expected integers")
    if start_line < 1 or end_line < 1:
        raise ManualBlockError(
            f"manual block line {start_line}-{end_line}: lines are 1-based"
        )
    if start_line > end_line:
        raise ManualBlockError(
            f"manual block line {start_line}-{end_line}: "
            "start_line must be <= end_line"
        )
    if end_line > len(document.lines):
        raise ManualBlockError(
            f"manual block line {end_line}: document has only "
            f"{len(document.lines)} lines"
        )


def _default_anchor(lines: Sequence[str], start_line: int, end_line: int, *, start: bool) -> str:
    indices = range(start_line - 1, end_line) if start else range(end_line - 1, start_line - 2, -1)
    for index in indices:
        text = normalize_anchor(lines[index])
        if text:
            return text
    return ""


def create_manual_block(
    document: ScannedDocument,
    doc_id: str,
    start_line: int,
    end_line: int,
    *,
    start_anchor: str | None = None,
    end_anchor: str | None = None,
    profile_id: str = "manual",
    parent_block_id: str | None = None,
    legacy_block_id: str | None = None,
) -> ManualBlock:
    """Create a manual block from a 1-based inclusive line range."""

    _validate_range(document, start_line, end_line)
    if start_anchor is None:
        start_anchor = _default_anchor(
            document.lines, start_line, end_line, start=True
        )
    if end_anchor is None:
        end_anchor = _default_anchor(
            document.lines, start_line, end_line, start=False
        )
    start_anchor = normalize_anchor(start_anchor)
    end_anchor = normalize_anchor(end_anchor)
    if not start_anchor or not end_anchor:
        raise ManualBlockError("manual block anchors: start and end must not be empty")

    snapshot_text = "\n".join(document.lines[start_line - 1 : end_line])
    anchor = f"manual:{start_anchor}|{end_anchor}"
    return ManualBlock(
        doc_id=doc_id,
        profile_id=profile_id,
        block_id=compute_block_id(
            doc_id, profile_id, "manual_range", anchor, 0
        ),
        line_start=start_line,
        line_end=end_line,
        start_anchor=start_anchor,
        end_anchor=end_anchor,
        snapshot_text=snapshot_text,
        snapshot_sha256=snapshot_sha256(snapshot_text),
        source_sha256=snapshot_sha256(document.text),
        parent_block_id=parent_block_id,
        legacy_block_id=legacy_block_id,
    )


def _find_unique_anchor(
    lines: Sequence[str], anchor: str, *, start_index: int = 0
) -> tuple[int | None, str | None]:
    normalized = normalize_anchor(anchor)
    if not normalized:
        return None, "anchor is empty"
    matches = [
        index
        for index in range(start_index, len(lines))
        if normalized in normalize_anchor(lines[index])
    ]
    if not matches:
        return None, f"anchor not found: {anchor!r}"
    if len(matches) > 1:
        return None, f"anchor is ambiguous: {anchor!r}"
    return matches[0], None


def resolve_manual_block(
    block: ManualBlock,
    document: ScannedDocument,
) -> ManualBlock:
    """Re-resolve anchors and refresh the snapshot, or mark the block stale.

    Old line numbers are never applied to new content. When either anchor is
    missing or ambiguous the stored snapshot and line range are preserved and
    the block is marked ``stale``.
    """

    if not document.lines:
        return replace(
            block,
            status="stale",
            stale_reason="document is empty",
        )

    start_index, start_error = _find_unique_anchor(document.lines, block.start_anchor)
    if start_error is not None:
        return replace(block, status="stale", stale_reason=start_error)

    end_index, end_error = _find_unique_anchor(
        document.lines,
        block.end_anchor,
        start_index=start_index if start_index is not None else 0,
    )
    if end_error is not None:
        return replace(block, status="stale", stale_reason=end_error)
    if end_index is None or start_index is None or end_index < start_index:
        return replace(
            block,
            status="stale",
            stale_reason="end anchor appears before start anchor",
        )

    line_start = start_index + 1
    line_end = end_index + 1
    snapshot_text = "\n".join(document.lines[start_index : end_index + 1])
    return replace(
        block,
        line_start=line_start,
        line_end=line_end,
        snapshot_text=snapshot_text,
        snapshot_sha256=snapshot_sha256(snapshot_text),
        source_sha256=snapshot_sha256(normalize_text(document.text)),
        status="active",
        stale_reason=None,
    )


def resolve_manual_blocks(
    document: ScannedDocument,
    blocks: Sequence[ManualBlock],
) -> list[ManualBlock]:
    return [resolve_manual_block(block, document) for block in blocks]
