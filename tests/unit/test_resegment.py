"""Re-segmentation: keep, update, lineage, missing and no re-pointing."""

from __future__ import annotations

from stratum.segmentation.resegment import BlockSnapshot, resegment


def _block(
    block_id: str,
    anchor: str,
    text: str,
    *,
    line_start: int | None = None,
    line_end: int | None = None,
) -> BlockSnapshot:
    return BlockSnapshot(
        block_id=block_id,
        anchor=anchor,
        snapshot_text=text,
        line_start=line_start,
        line_end=line_end,
    )


def test_same_anchor_and_snapshot_is_kept() -> None:
    old = [_block("BLK-old", "Heading", "Same text.")]
    new = [_block("BLK-new", "Heading", "Same text.")]

    result = resegment(old, new)

    assert [(item.old_block_id, item.new_block_id) for item in result.kept] == [
        ("BLK-old", "BLK-new")
    ]
    assert result.updated == ()
    assert result.lineage == ()
    assert result.missing == ()
    assert result.created == ()


def test_same_anchor_changed_snapshot_keeps_id_and_marks_stale() -> None:
    old = [_block("BLK-old", "Heading", "Old text.")]
    new = [_block("BLK-new", "Heading", "New text.")]

    result = resegment(old, new)

    assert result.kept == ()
    assert len(result.updated) == 1
    update = result.updated[0]
    assert update.old_block_id == "BLK-old"
    assert update.new_block_id == "BLK-new"
    assert update.status == "stale"
    assert update.old_snapshot_sha256 != update.new_snapshot_sha256


def test_renamed_anchor_with_same_snapshot_creates_lineage() -> None:
    old = [_block("BLK-old", "Old heading", "Same body.")]
    new = [_block("BLK-new", "New heading", "Same body.")]

    result = resegment(old, new)

    assert len(result.lineage) == 1
    edge = result.lineage[0]
    assert (edge.old_block_id, edge.new_block_id) == ("BLK-old", "BLK-new")
    assert edge.reason == "rename"
    assert edge.method == "snapshot"
    assert result.kept == ()
    assert result.missing == ()
    assert result.created == ()


def test_moved_anchor_with_same_snapshot_creates_move_lineage() -> None:
    old = [_block("BLK-old", "Old location", "Same body.", line_start=2, line_end=2)]
    new = [_block("BLK-new", "New location", "Same body.", line_start=90, line_end=90)]

    result = resegment(old, new)

    assert len(result.lineage) == 1
    assert result.lineage[0].reason == "move"


def test_split_lineage_old_to_each_new() -> None:
    old = [_block("BLK-old", "Paragraph", "Alpha. Beta. Gamma.")]
    new = [
        _block("BLK-a", "Paragraph", "Alpha."),
        _block("BLK-b", "Paragraph", "Beta."),
        _block("BLK-c", "Paragraph", "Gamma."),
    ]

    result = resegment(old, new)

    assert [edge.reason for edge in result.lineage] == ["split", "split", "split"]
    assert [edge.old_block_id for edge in result.lineage] == ["BLK-old"] * 3
    assert [edge.new_block_id for edge in result.lineage] == [
        "BLK-a",
        "BLK-b",
        "BLK-c",
    ]
    assert result.kept == ()
    assert result.updated == ()
    assert result.missing == ()
    assert result.created == ()


def test_merge_lineage_each_old_to_new() -> None:
    old = [
        _block("BLK-a", "A", "Alpha."),
        _block("BLK-b", "B", "Beta."),
        _block("BLK-c", "C", "Gamma."),
    ]
    new = [_block("BLK-new", "Merged", "Alpha. Beta. Gamma.")]

    result = resegment(old, new)

    assert [edge.reason for edge in result.lineage] == ["merge", "merge", "merge"]
    assert [edge.old_block_id for edge in result.lineage] == [
        "BLK-a",
        "BLK-b",
        "BLK-c",
    ]
    assert [edge.new_block_id for edge in result.lineage] == ["BLK-new"] * 3
    assert result.missing == ()
    assert result.created == ()


def test_missing_old_block_is_reported_never_deleted() -> None:
    old = [
        _block("BLK-keep", "Keep", "Kept."),
        _block("BLK-gone", "Gone", "Removed."),
    ]
    new = [_block("BLK-keep", "Keep", "Kept.")]

    result = resegment(old, new)

    assert [item.block_id for item in result.missing] == ["BLK-gone"]
    assert len(result.kept) == 1
    assert all(edge.old_block_id != "BLK-gone" for edge in result.lineage)


def test_old_block_is_never_repointed_to_a_different_segment() -> None:
    old = [_block("BLK-old", "Original", "Original body.")]
    new = [_block("BLK-new", "Different", "Different body.")]

    result = resegment(old, new)

    assert result.kept == ()
    assert result.updated == ()
    assert result.lineage == ()
    assert [item.block_id for item in result.missing] == ["BLK-old"]
    assert [item.block_id for item in result.created] == ["BLK-new"]


def test_duplicate_anchors_pair_by_snapshot_before_updating() -> None:
    old = [
        _block("BLK-one", "Status", "First status."),
        _block("BLK-two", "Status", "Second status."),
    ]
    new = [
        _block("BLK-one", "Status", "First status."),
        _block("BLK-two", "Status", "Second status changed."),
    ]

    result = resegment(old, new)

    assert [item.old_block_id for item in result.kept] == ["BLK-one"]
    assert [item.old_block_id for item in result.updated] == ["BLK-two"]
