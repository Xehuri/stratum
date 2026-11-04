"""Manual blocks: line ranges, anchors, re-resolution and stale state."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from stratum.markdown import scan_document
from stratum.segmentation.manual import (
    ManualBlockError,
    create_manual_block,
    resolve_manual_block,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "markdown"


def _doc(name: str = "manual.md"):
    return scan_document((FIXTURES / name).read_text(encoding="utf-8"))


def test_create_manual_block_from_range() -> None:
    doc = _doc()
    block = create_manual_block(
        doc,
        "notes/manual.md",
        3,
        9,
        start_anchor="## Sensor calibration window",
        end_anchor="All readings in this window are synthetic.",
    )

    assert block.block_id.startswith("BLK-")
    assert re.fullmatch(r"BLK-[0-9a-f]{16}", block.block_id)
    assert (block.line_start, block.line_end) == (3, 9)
    assert block.start_anchor == "## Sensor calibration window"
    assert block.end_anchor == "All readings in this window are synthetic."
    assert block.snapshot_text.startswith("## Sensor calibration window")
    assert block.snapshot_text.endswith("All readings in this window are synthetic.")
    assert block.snapshot_sha256
    assert block.source_sha256
    assert block.status == "active"
    assert block.parent_block_id is None


def test_resolve_manual_block_after_source_shift() -> None:
    doc = _doc()
    block = create_manual_block(doc, "notes/manual.md", 3, 9)

    shifted = scan_document("Inserted one.\n\nInserted two.\n\n" + doc.text)
    resolved = resolve_manual_block(block, shifted)

    assert resolved.status == "active"
    assert resolved.block_id == block.block_id
    assert (resolved.line_start, resolved.line_end) == (7, 13)
    assert resolved.snapshot_text == block.snapshot_text
    assert resolved.snapshot_sha256 == block.snapshot_sha256
    assert resolved.source_sha256 != block.source_sha256
    assert resolved.stale_reason is None


def test_missing_anchor_marks_block_stale_and_preserves_identity() -> None:
    doc = _doc()
    block = create_manual_block(doc, "notes/manual.md", 3, 9)

    changed = scan_document(
        doc.text.replace(
            "## Sensor calibration window",
            "## Sensor calibration renamed",
            1,
        )
    )
    resolved = resolve_manual_block(block, changed)

    assert resolved.status == "stale"
    assert resolved.block_id == block.block_id
    assert (resolved.line_start, resolved.line_end) == (3, 9)
    assert resolved.snapshot_text == block.snapshot_text
    assert resolved.snapshot_sha256 == block.snapshot_sha256
    assert resolved.stale_reason
    assert "not found" in resolved.stale_reason.lower()


def test_ambiguous_anchor_marks_block_stale() -> None:
    doc = _doc()
    block = create_manual_block(doc, "notes/manual.md", 3, 9)

    duplicated = scan_document("## Sensor calibration window\n\ncopy\n\n" + doc.text)
    resolved = resolve_manual_block(block, duplicated)

    assert resolved.status == "stale"
    assert resolved.block_id == block.block_id
    assert "ambiguous" in (resolved.stale_reason or "").lower()


def test_overlapping_blocks_and_parent_links_are_allowed() -> None:
    doc = _doc()
    parent = create_manual_block(doc, "notes/manual.md", 3, 9)
    child = create_manual_block(
        doc,
        "notes/manual.md",
        5,
        7,
        parent_block_id=parent.block_id,
    )

    assert child.parent_block_id == parent.block_id
    assert child.line_start >= parent.line_start
    assert child.line_end <= parent.line_end


def test_out_of_range_manual_ranges_are_rejected() -> None:
    doc = _doc()

    with pytest.raises(ManualBlockError, match="line"):
        create_manual_block(doc, "notes/manual.md", 0, 2)
    with pytest.raises(ManualBlockError, match="line"):
        create_manual_block(doc, "notes/manual.md", 3, 999)
    with pytest.raises(ManualBlockError, match="line"):
        create_manual_block(doc, "notes/manual.md", 9, 3)
    with pytest.raises(ManualBlockError, match="empty"):
        create_manual_block(scan_document(""), "empty.md", 1, 1)


def test_legacy_id_is_preserved_on_manual_blocks() -> None:
    doc = _doc()
    block = create_manual_block(
        doc, "notes/manual.md", 3, 9, legacy_block_id="B-legacy"
    )

    assert block.legacy_block_id == "B-legacy"
