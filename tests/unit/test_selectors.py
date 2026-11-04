"""Selector engine: every selector type and prose merge/split rules."""

from __future__ import annotations

from pathlib import Path

import pytest

from stratum.markdown import scan_document
from stratum.segmentation.selectors import SelectorError, select

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "markdown"


def _doc(name: str):
    return scan_document((FIXTURES / name).read_text(encoding="utf-8"))


def test_heading_selector_level_and_include_heading() -> None:
    doc = _doc("atx.md")
    blocks = select(doc, {"type": "heading", "level": 2, "include_heading": True})

    assert [block.line_start for block in blocks] == [5, 13, 22]
    assert [block.line_end for block in blocks] == [11, 15, 25]
    assert blocks[0].anchor == "Section One"
    assert "### Subsection" in blocks[0].text

    body_only = select(doc, {"type": "heading", "level": 2, "include_heading": False})
    assert body_only[0].line_start == 7
    assert body_only[0].text.startswith("Alpha content")
    assert "## Section One" not in body_only[0].text


def test_heading_selector_without_level_does_not_overlap() -> None:
    doc = _doc("atx.md")
    blocks = select(doc, {"type": "heading"})

    assert [block.anchor for block in blocks] == [
        "Top Title",
        "Section One",
        "Subsection",
        "Section Two",
        "Setext One",
        "Setext Two",
    ]
    for previous, current in zip(blocks, blocks[1:]):
        assert previous.line_end < current.line_start


def test_heading_prefix_filter() -> None:
    text = (
        "# Daily\n\n"
        "## OBS-1 First\n\nbody one\n\n"
        "## Narrative\n\nnot selected\n\n"
        "## OBS-2 Second\n\nbody two\n"
    )
    doc = scan_document(text)
    blocks = select(
        doc,
        {"type": "heading", "level": 2, "heading_prefixes": ["OBS-"]},
    )

    assert [block.anchor for block in blocks] == ["OBS-1 First", "OBS-2 Second"]


def test_time_heading_selector() -> None:
    text = (
        "# Day\n\n"
        "## 09:30 Morning\n\ntext one\n\n"
        "## 12:00 Noon\n\ntext two\n\n"
        "## 9:05 Early\n\ntext three\n\n"
        "## Not a time\n\ntext four\n"
    )
    doc = scan_document(text)
    blocks = select(doc, {"type": "time_heading"})

    assert [block.anchor for block in blocks] == [
        "09:30 Morning",
        "12:00 Noon",
        "9:05 Early",
    ]
    assert blocks[0].selector_type == "time_heading"


def test_paragraph_selector_splits_on_blank_lines() -> None:
    doc = _doc("prose.md")
    blocks = select(doc, {"type": "paragraph"})

    assert len(blocks) == 4
    assert blocks[0].line_start == 1
    assert blocks[1].line_start == 3
    assert all(block.selector_type == "paragraph" for block in blocks)


def test_prose_short_paragraphs_merge_until_minimum() -> None:
    doc = _doc("prose.md")
    blocks = select(doc, {"type": "paragraph", "min_chars": 80, "max_chars": 4000})

    assert len(blocks) == 2
    assert "Tidepools reward patience" in blocks[0].text
    assert "second paragraph is long enough" in blocks[0].text
    assert "short closing note" in blocks[1].text
    assert "final paragraph records" in blocks[1].text


def test_prose_long_paragraph_splits_at_sentences() -> None:
    text = (
        "First sentence carries enough words to matter. "
        "Second sentence also carries enough words to matter. "
        "Third sentence closes the paragraph with more words.\n"
    )
    doc = scan_document(text)
    blocks = select(doc, {"type": "paragraph", "min_chars": 0, "max_chars": 60})

    assert len(blocks) >= 2
    assert all(len(block.text) <= 60 for block in blocks)
    compact = " ".join(" ".join(block.text.split()) for block in blocks)
    assert compact == " ".join(text.split())
    assert [block.line_start for block in blocks] == [1] * len(blocks)


def test_prose_hard_limit_when_no_sentence_boundary() -> None:
    text = "x" * 25 + "\n"
    doc = scan_document(text)
    blocks = select(doc, {"type": "paragraph", "min_chars": 0, "max_chars": 10})

    assert len(blocks) == 3
    assert [len(block.text) for block in blocks] == [10, 10, 5]


def test_separator_selector_excludes_frontmatter_and_fences() -> None:
    text = (
        "---\nid: x\n---\n\n"
        "alpha\n\n---\n\nbeta\n\n---\n\ngamma\n\n"
        "```\n***\n```\n"
    )
    doc = scan_document(text)
    blocks = select(doc, {"type": "separator"})

    assert [block.text.split("\n")[0] for block in blocks] == [
        "alpha",
        "beta",
        "gamma",
    ]
    assert [block.line_start for block in blocks] == [5, 9, 13]
    assert len(blocks) == 3
    assert "***" in blocks[2].text
    assert blocks[2].text.endswith("```")


def test_regex_selector() -> None:
    text = (
        "# Log\n\n"
        "OBS-1 first observation\n"
        "noise\n"
        "OBS-2 second observation\n"
    )
    doc = scan_document(text)
    blocks = select(doc, {"type": "regex", "pattern": r"^OBS-\d+ .*$"})

    assert [block.text for block in blocks] == [
        "OBS-1 first observation",
        "OBS-2 second observation",
    ]
    with pytest.raises(SelectorError, match="pattern"):
        select(doc, {"type": "regex", "pattern": "("})


def test_manual_range_selector() -> None:
    doc = _doc("manual.md")
    blocks = select(doc, {"type": "manual_range", "start_line": 3, "end_line": 9})

    assert len(blocks) == 1
    block = blocks[0]
    assert block.line_start == 3 and block.line_end == 9
    assert block.text.startswith("## Sensor calibration window")
    assert block.text.endswith("All readings in this window are synthetic.")
    assert block.metadata["manual"] is True

    with pytest.raises(SelectorError, match="line"):
        select(doc, {"type": "manual_range", "start_line": 3, "end_line": 999})
    with pytest.raises(SelectorError, match="line"):
        select(doc, {"type": "manual_range", "start_line": 0, "end_line": 2})


def test_manual_anchor_selector() -> None:
    doc = _doc("manual.md")
    blocks = select(
        doc,
        {
            "type": "manual_anchor",
            "start_anchor": "## Sensor calibration window",
            "end_anchor": "All readings in this window are synthetic.",
        },
    )

    assert len(blocks) == 1
    assert (blocks[0].line_start, blocks[0].line_end) == (3, 9)
    assert blocks[0].anchor.startswith("manual:")

    with pytest.raises(SelectorError, match="not found"):
        select(
            doc,
            {
                "type": "manual_anchor",
                "start_anchor": "does not exist",
                "end_anchor": "All readings in this window are synthetic.",
            },
        )


def test_manual_anchor_ambiguity_is_rejected() -> None:
    text = "## Target\n\nA\n\n## Target\n\nB\n"
    doc = scan_document(text)
    with pytest.raises(SelectorError, match="ambiguous"):
        select(
            doc,
            {"type": "manual_anchor", "start_anchor": "## Target", "end_anchor": "B"},
        )


def test_whole_document_selector() -> None:
    doc = _doc("no-heading.md")
    blocks = select(doc, {"type": "whole_document"})

    assert len(blocks) == 1
    assert blocks[0].line_start == 1
    assert blocks[0].line_end == 5
    assert "final paragraph" in blocks[0].text
    assert blocks[0].anchor == "document"

    assert select(scan_document(""), {"type": "whole_document"}) == []


def test_frontmatter_fenced_code_and_table_selectors() -> None:
    front = _doc("frontmatter.md")
    blocks = select(front, {"type": "frontmatter"})
    assert len(blocks) == 1
    assert (blocks[0].line_start, blocks[0].line_end) == (1, 6)
    assert blocks[0].text.startswith("---")
    assert blocks[0].anchor == "DOC-2026-01-05"

    fences = select(_doc("fence.md"), {"type": "fenced_code"})
    assert len(fences) == 2
    assert fences[0].selector_type == "fenced_code"
    assert "Not A Heading" in fences[0].text
    assert fences[0].metadata["info"] == "markdown"

    tables = select(_doc("table.md"), {"type": "table"})
    assert len(tables) == 1
    assert "Sample" in tables[0].anchor
    assert "sample-001" in tables[0].text

    assert select(_doc("no-heading.md"), {"type": "frontmatter"}) == []
    assert select(_doc("no-heading.md"), {"type": "fenced_code"}) == []
    assert select(_doc("no-heading.md"), {"type": "table"}) == []


def test_composite_selector_preserves_source_order() -> None:
    doc = _doc("frontmatter.md")
    blocks = select(
        doc,
        {
            "type": "composite",
            "selectors": [
                {"type": "heading", "level": 1},
                {"type": "frontmatter"},
            ],
        },
    )

    assert [block.selector_type for block in blocks] == ["frontmatter", "heading"]
    assert blocks[0].line_start < blocks[1].line_start


def test_unknown_selector_is_a_clear_error() -> None:
    with pytest.raises(SelectorError, match="unknown selector"):
        select(scan_document("text\n"), {"type": "wat"})
