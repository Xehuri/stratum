"""Re-segmentation matching.

This module only computes the comparison result. It never writes to the
database: the ingest layer applies kept, updated, lineage and missing records
in a later wave.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, replace
from typing import Any, Iterable, Sequence

from ..markdown import normalize_anchor, normalize_text
from .identity import snapshot_sha256

__all__ = [
    "BlockSnapshot",
    "KeptBlock",
    "LineageEdge",
    "ResegmentationResult",
    "UpdatedBlock",
    "resegment",
]


@dataclass(frozen=True)
class BlockSnapshot:
    """Minimal block state needed to compare two segmentations."""

    block_id: str
    anchor: str
    snapshot_text: str
    snapshot_sha256: str | None = None
    selector_type: str = "heading"
    line_start: int | None = None
    line_end: int | None = None
    status: str = "active"


@dataclass(frozen=True)
class KeptBlock:
    old_block_id: str
    new_block_id: str
    anchor: str


@dataclass(frozen=True)
class UpdatedBlock:
    old_block_id: str
    new_block_id: str
    anchor: str
    old_snapshot_sha256: str
    new_snapshot_sha256: str
    status: str = "stale"


@dataclass(frozen=True)
class LineageEdge:
    old_block_id: str
    new_block_id: str
    reason: str
    method: str = "snapshot"
    confidence: float = 1.0


@dataclass(frozen=True)
class ResegmentationResult:
    kept: tuple[KeptBlock, ...] = ()
    updated: tuple[UpdatedBlock, ...] = ()
    lineage: tuple[LineageEdge, ...] = ()
    missing: tuple[BlockSnapshot, ...] = ()
    created: tuple[BlockSnapshot, ...] = ()


def _sha(block: Any) -> str:
    value = getattr(block, "snapshot_sha256", None)
    if value:
        return str(value)
    return snapshot_sha256(getattr(block, "snapshot_text", ""))


def _anchor(block: Any) -> str:
    return normalize_anchor(str(getattr(block, "anchor", "")))


def _block_id(block: Any) -> str:
    return str(getattr(block, "block_id"))


def _canonical(text: str) -> str:
    return re.sub(r"\s+", " ", normalize_text(text)).strip()


def _concat_matches(target: str, pieces: Sequence[str]) -> bool:
    canonical_target = _canonical(target)
    for separator in ("", "\n", " ", "\n\n"):
        if _canonical(separator.join(pieces)) == canonical_target:
            return True
    return False


def _position(block: Any, index: int) -> tuple[int, int]:
    line_start = getattr(block, "line_start", None)
    if isinstance(line_start, int):
        return (line_start, index)
    return (0, index)


def _is_move(old: Any, new: Any) -> bool:
    old_start = getattr(old, "line_start", None)
    new_start = getattr(new, "line_start", None)
    if not isinstance(old_start, int) or not isinstance(new_start, int):
        return False
    return abs(new_start - old_start) > 3


def _ordered_indices(
    blocks: Sequence[Any], indices: Iterable[int]
) -> list[int]:
    return sorted(indices, key=lambda index: _position(blocks[index], index))


def _with_status(block: Any, status: str) -> Any:
    try:
        return replace(block, status=status)
    except TypeError:
        return block


def resegment(
    old_blocks: Sequence[Any],
    new_blocks: Sequence[Any],
) -> ResegmentationResult:
    """Compare old and new segmentations without changing either side."""

    old = list(old_blocks)
    new = list(new_blocks)
    old_shas = [_sha(block) for block in old]
    new_shas = [_sha(block) for block in new]
    used_old: set[int] = set()
    used_new: set[int] = set()
    kept: list[KeptBlock] = []
    updated: list[UpdatedBlock] = []
    lineage: list[LineageEdge] = []

    old_by_anchor: dict[str, list[int]] = defaultdict(list)
    new_by_anchor: dict[str, list[int]] = defaultdict(list)
    for index, block in enumerate(old):
        old_by_anchor[_anchor(block)].append(index)
    for index, block in enumerate(new):
        new_by_anchor[_anchor(block)].append(index)

    # 1. Same anchor and same snapshot: keep the old id.
    for anchor, old_indices in old_by_anchor.items():
        new_indices = [
            index for index in new_by_anchor.get(anchor, []) if index not in used_new
        ]
        new_by_sha: dict[str, list[int]] = defaultdict(list)
        for index in new_indices:
            new_by_sha[new_shas[index]].append(index)
        for old_index in old_indices:
            if old_index in used_old:
                continue
            candidates = [
                index
                for index in new_by_sha.get(old_shas[old_index], [])
                if index not in used_new
            ]
            if not candidates:
                continue
            new_index = candidates[0]
            kept.append(
                KeptBlock(
                    old_block_id=_block_id(old[old_index]),
                    new_block_id=_block_id(new[new_index]),
                    anchor=anchor,
                )
            )
            used_old.add(old_index)
            used_new.add(new_index)

    # 2. Split: one old snapshot equals a contiguous run of new snapshots.
    for old_index in _ordered_indices(
        old, [index for index in range(len(old)) if index not in used_old]
    ):
        available = _ordered_indices(
            new, [index for index in range(len(new)) if index not in used_new]
        )
        matched: list[int] | None = None
        for start in range(len(available)):
            for length in range(2, len(available) - start + 1):
                window = available[start : start + length]
                if _concat_matches(
                    old[old_index].snapshot_text,
                    [new[index].snapshot_text for index in window],
                ):
                    matched = window
                    break
            if matched is not None:
                break
        if matched is None:
            continue
        for new_index in matched:
            lineage.append(
                LineageEdge(
                    old_block_id=_block_id(old[old_index]),
                    new_block_id=_block_id(new[new_index]),
                    reason="split",
                )
            )
            used_new.add(new_index)
        used_old.add(old_index)

    # 3. Merge: one new snapshot equals a contiguous run of old snapshots.
    for new_index in _ordered_indices(
        new, [index for index in range(len(new)) if index not in used_new]
    ):
        available = _ordered_indices(
            old, [index for index in range(len(old)) if index not in used_old]
        )
        matched: list[int] | None = None
        for start in range(len(available)):
            for length in range(2, len(available) - start + 1):
                window = available[start : start + length]
                if _concat_matches(
                    new[new_index].snapshot_text,
                    [old[index].snapshot_text for index in window],
                ):
                    matched = window
                    break
            if matched is not None:
                break
        if matched is None:
            continue
        for old_index in matched:
            lineage.append(
                LineageEdge(
                    old_block_id=_block_id(old[old_index]),
                    new_block_id=_block_id(new[new_index]),
                    reason="merge",
                )
            )
            used_old.add(old_index)
        used_new.add(new_index)

    # 4. Same anchor, changed snapshot: keep the old id, mark the reference
    # stale. Pair remaining blocks in source order.
    for anchor, old_indices in old_by_anchor.items():
        remaining_old = [index for index in old_indices if index not in used_old]
        remaining_new = _ordered_indices(
            new,
            [
                index
                for index in new_by_anchor.get(anchor, [])
                if index not in used_new
            ],
        )
        for old_index, new_index in zip(remaining_old, remaining_new):
            updated.append(
                UpdatedBlock(
                    old_block_id=_block_id(old[old_index]),
                    new_block_id=_block_id(new[new_index]),
                    anchor=anchor,
                    old_snapshot_sha256=old_shas[old_index],
                    new_snapshot_sha256=new_shas[new_index],
                    status="stale",
                )
            )
            used_old.add(old_index)
            used_new.add(new_index)

    # 5. Different anchor, identical snapshot: rename or move lineage.
    new_by_sha_global: dict[str, list[int]] = defaultdict(list)
    for index, sha in enumerate(new_shas):
        if index not in used_new:
            new_by_sha_global[sha].append(index)
    for old_index in _ordered_indices(
        old, [index for index in range(len(old)) if index not in used_old]
    ):
        candidates = [
            index
            for index in new_by_sha_global.get(old_shas[old_index], [])
            if index not in used_new
        ]
        if not candidates:
            continue
        new_index = candidates[0]
        reason = "move" if _is_move(old[old_index], new[new_index]) else "rename"
        lineage.append(
            LineageEdge(
                old_block_id=_block_id(old[old_index]),
                new_block_id=_block_id(new[new_index]),
                reason=reason,
            )
        )
        used_old.add(old_index)
        used_new.add(new_index)

    missing = tuple(
        _with_status(old[index], "missing")
        for index in range(len(old))
        if index not in used_old
    )
    created = tuple(
        new[index] for index in range(len(new)) if index not in used_new
    )
    return ResegmentationResult(
        kept=tuple(kept),
        updated=tuple(updated),
        lineage=tuple(lineage),
        missing=missing,
        created=created,
    )
