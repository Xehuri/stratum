"""Block identity: deterministic doc ids, anchors, ids and snapshots."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

from stratum.markdown import scan_document, scan_file
from stratum.segmentation.identity import (
    BlockDraft,
    anchor_from_text,
    build_block_drafts,
    compute_block_id,
    document_id,
    normalize_anchor,
    snapshot_sha256,
)
from stratum.segmentation.selectors import select

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "markdown"


def _doc(name: str):
    return scan_document((FIXTURES / name).read_text(encoding="utf-8"))


def test_document_id_prefers_frontmatter_id() -> None:
    assert document_id({"id": "DOC-1"}, "notes/a.md") == "DOC-1"
    assert document_id({}, "notes\\a.md") == "notes/a.md"
    assert document_id({}, "./notes//a.md") == "notes/a.md"


def test_block_id_is_deterministic_and_well_formed() -> None:
    first = compute_block_id("notes/a.md", "heading", "heading", "Section", 0)
    second = compute_block_id("notes/a.md", "heading", "heading", "Section", 0)
    expected = "BLK-" + hashlib.sha256(
        b"notes/a.mdheadingheadingSection0"
    ).hexdigest()[:16]

    assert first == second == expected
    assert re.fullmatch(r"BLK-[0-9a-f]{16}", first)
    assert first != compute_block_id("notes/a.md", "heading", "heading", "Section", 1)
    assert first != compute_block_id("notes/a.md", "other", "heading", "Section", 0)


def test_anchor_normalization_and_text_anchor_stability() -> None:
    assert normalize_anchor("  Section \t One\n") == "Section One"

    first = anchor_from_text("First line\nmiddle one\nLast line")
    changed_middle = anchor_from_text("First line\nmiddle two\nLast line")
    changed_edges = anchor_from_text("Different first\nmiddle two\nLast line")

    assert first == changed_middle
    assert first != changed_edges
    assert first.startswith("text:")


def test_snapshot_sha256_ignores_line_endings_and_bom() -> None:
    lf = snapshot_sha256("# A\n\nBody\n")
    crlf = snapshot_sha256("\ufeff# A\r\n\r\nBody\r\n")

    assert lf == crlf
    assert lf != snapshot_sha256("# A\n\nBody changed\n")


def test_build_block_drafts_populates_w3_fields() -> None:
    doc = _doc("atx.md")
    selections = select(doc, {"type": "heading", "level": 2})
    drafts = build_block_drafts(
        "notes/atx.md", "heading", selections, retrieval_type="structured"
    )

    assert len(drafts) == 3
    for draft in drafts:
        assert isinstance(draft, BlockDraft)
        assert draft.doc_id == "notes/atx.md"
        assert draft.profile_id == "heading"
        assert draft.selector_type == "heading"
        assert draft.status == "active"
        assert draft.retrieval_type == "structured"
        assert draft.line_start >= 1
        assert draft.line_end >= draft.line_start
        assert draft.snapshot_text
        assert draft.snapshot_sha256 == snapshot_sha256(draft.snapshot_text)
        assert re.fullmatch(r"BLK-[0-9a-f]{16}", draft.block_id)


def test_duplicate_anchor_occurrence_changes_block_id() -> None:
    doc = _doc("duplicate-heading.md")
    selections = select(doc, {"type": "heading", "level": 2})
    drafts = build_block_drafts("doc", "heading", selections)

    assert [draft.anchor for draft in drafts] == ["Status", "Status", "Other"]
    assert [draft.occurrence for draft in drafts] == [0, 1, 0]
    assert drafts[0].block_id != drafts[1].block_id


def test_build_is_deterministic_across_runs() -> None:
    doc = _doc("atx.md")
    selections = select(doc, {"type": "heading"})

    first = build_block_drafts("doc", "p", selections)
    second = build_block_drafts("doc", "p", selections)

    assert [draft.block_id for draft in first] == [draft.block_id for draft in second]
    assert [draft.snapshot_sha256 for draft in first] == [
        draft.snapshot_sha256 for draft in second
    ]


def test_legacy_block_id_is_preserved() -> None:
    doc = _doc("atx.md")
    selections = select(doc, {"type": "heading", "level": 2})
    drafts = build_block_drafts(
        "doc",
        "heading",
        selections,
        legacy_block_ids={
            "Section One": "L-legacy-one",
            "Section Two": "L-legacy-two",
        },
    )

    assert [draft.legacy_block_id for draft in drafts] == [
        "L-legacy-one",
        "L-legacy-two",
        None,
    ]

    with pytest.raises(ValueError, match="legacy"):
        build_block_drafts(
            "doc",
            "heading",
            selections,
            legacy_block_ids={
                "Section One": "L-same",
                "Section Two": "L-same",
            },
        )


def test_crlf_and_bom_produce_identical_snapshot_hashes() -> None:
    crlf = scan_file(FIXTURES / "crlf.md")
    bom = scan_file(FIXTURES / "bom.md")

    assert crlf.text == bom.text
    assert [span.text for span in crlf.headings] == [
        span.text for span in bom.headings
    ]
    assert snapshot_sha256(crlf.text) == snapshot_sha256(bom.text)
