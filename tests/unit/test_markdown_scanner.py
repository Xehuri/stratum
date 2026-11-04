"""Markdown scanner: spans, normalization and profile hints."""

from __future__ import annotations

from pathlib import Path

import pytest

from stratum.markdown import (
    normalize_relative_path,
    normalize_text,
    resolve_default_profile,
    scan_document,
    scan_file,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "markdown"


def _fixture(name: str) -> Path:
    return FIXTURES / name


def test_atx_and_setext_headings() -> None:
    doc = scan_document(_fixture("atx.md").read_text(encoding="utf-8"))

    found = [(span.heading_text, span.level, span.line_start) for span in doc.headings]
    assert found == [
        ("Top Title", 1, 1),
        ("Section One", 2, 5),
        ("Subsection", 3, 9),
        ("Section Two", 2, 13),
        ("Setext One", 1, 17),
        ("Setext Two", 2, 22),
    ]
    assert doc.headings[4].line_end == 18
    assert doc.headings[5].line_end == 23


def test_duplicate_headings_get_occurrence_numbers() -> None:
    doc = scan_document(_fixture("duplicate-heading.md").read_text(encoding="utf-8"))
    statuses = [span for span in doc.headings if span.heading_text == "Status"]

    assert [span.occurrence for span in statuses] == [0, 1]
    assert [span.line_start for span in statuses] == [3, 7]


def test_headings_inside_fenced_code_are_ignored() -> None:
    doc = scan_document(_fixture("fence.md").read_text(encoding="utf-8"))

    headings = {span.heading_text for span in doc.headings}
    assert "Not A Heading" not in headings
    assert "Also Not A Heading" not in headings
    assert "Tilde Not A Heading" not in headings
    assert headings == {"Real Heading", "Final Heading"}

    fences = doc.fenced_code
    assert len(fences) == 2
    assert fences[0].line_start == 5 and fences[0].line_end == 8
    assert fences[0].info == "markdown"
    assert fences[1].line_start == 12 and fences[1].line_end == 14
    assert fences[1].info == ""


def test_unclosed_fence_runs_to_eof() -> None:
    text = "# Heading\n\n```\n## Still Code\nlast line\n"
    doc = scan_document(text)

    assert [span.heading_text for span in doc.headings] == ["Heading"]
    assert len(doc.fenced_code) == 1
    fence = doc.fenced_code[0]
    assert fence.line_start == 3
    assert fence.line_end == 5
    assert "## Still Code" in fence.text


def test_frontmatter_is_parsed_and_raw_is_preserved() -> None:
    doc = scan_document(_fixture("frontmatter.md").read_text(encoding="utf-8"))

    assert doc.frontmatter is not None
    assert doc.frontmatter.line_start == 1
    assert doc.frontmatter.line_end == 6
    assert doc.frontmatter.values["id"] == "DOC-2026-01-05"
    assert doc.frontmatter.values["index_profile"] == "heading"
    assert doc.frontmatter.values["tags"] == ["alpha", "beta"]
    assert doc.frontmatter.raw.startswith("id: DOC-2026-01-05")
    assert "tags: [alpha, beta]" in doc.frontmatter.raw
    assert doc.frontmatter_value("index_profile") == "heading"
    assert doc.frontmatter_value("missing", "fallback") == "fallback"

    headings = {span.heading_text for span in doc.headings}
    assert headings == {"Frontmatter Body", "Detail"}
    assert all(span.line_start > 6 for span in doc.headings)


def test_crlf_and_bom_normalize_to_identical_text() -> None:
    crlf = scan_file(_fixture("crlf.md"))
    bom = scan_file(_fixture("bom.md"))
    plain = scan_document(
        "# Normalization Document\n\nFirst paragraph.\n\n## Section\n\nSecond paragraph.\n"
    )

    assert crlf.text == bom.text == plain.text
    assert crlf.lines == bom.lines == plain.lines
    assert [span.heading_text for span in crlf.headings] == ["Normalization Document", "Section"]
    assert "\r" not in crlf.text
    assert "\ufeff" not in bom.text


def test_cjk_headings_and_paragraphs() -> None:
    doc = scan_document(_fixture("cjk.md").read_text(encoding="utf-8"))

    assert [span.heading_text for span in doc.headings] == [
        "潮间带观察",
        "盐度变化",
        "备注",
        "第二段",
    ]
    paragraph_text = "\n".join(span.text for span in doc.paragraphs)
    assert "清晨的潮池很安静" in paragraph_text
    assert "表层盐度从 31.2 上升到 34.8 ppt" in paragraph_text


def test_table_span_and_paragraph_exclusion() -> None:
    doc = scan_document(_fixture("table.md").read_text(encoding="utf-8"))

    assert len(doc.tables) == 1
    table = doc.tables[0]
    assert (table.line_start, table.line_end) == (5, 8)
    assert "| Sample | Salinity | Temperature |" in table.text

    paragraph_text = "\n".join(span.text for span in doc.paragraphs)
    assert "Text before the table." in paragraph_text
    assert "Text after the table." in paragraph_text
    assert "sample-001" not in paragraph_text


def test_nested_list_stays_inside_one_paragraph() -> None:
    text = "Intro line\n- one\n  - nested\n- two\n\nAfter.\n"
    doc = scan_document(text)

    assert len(doc.paragraphs) == 2
    assert "- one" in doc.paragraphs[0].text
    assert "nested" in doc.paragraphs[0].text


def test_html_block_is_reported() -> None:
    text = "# H\n\n<div class=\"note\">\nraw html\n</div>\n\nAfter.\n"
    doc = scan_document(text)

    assert len(doc.html_blocks) == 1
    assert doc.html_blocks[0].line_start == 3
    assert doc.html_blocks[0].line_end == 5
    assert not any(span.line_start == 3 for span in doc.paragraphs)


def test_empty_document_has_no_spans() -> None:
    doc = scan_document("")

    assert doc.text == ""
    assert doc.lines == ()
    assert doc.frontmatter is None
    assert doc.headings == ()
    assert doc.paragraphs == ()
    assert doc.fenced_code == ()
    assert doc.tables == ()
    assert doc.separators == ()
    assert resolve_default_profile(doc) == "whole-document"


def test_fallback_profile_order() -> None:
    frontmatter = scan_document(
        "---\nindex_profile: custom-profile\n---\n\n# Heading\n"
    )
    assert resolve_default_profile(frontmatter) == "custom-profile"

    headings = scan_document("# Heading\n\nBody.\n")
    assert resolve_default_profile(headings) == "generic-heading"

    prose = scan_document("Paragraph one.\n\nParagraph two.\n")
    assert resolve_default_profile(prose) == "prose-paragraph"
    assert resolve_default_profile(prose, relative_path="notes/plain.md") == "prose-paragraph"

    assert (
        resolve_default_profile(
            prose,
            relative_path="journal/2026-01-05.md",
            glob_profiles={"journal/**": "journal-profile"},
        )
        == "journal-profile"
    )
    assert resolve_default_profile(scan_document("")) == "whole-document"


def test_normalize_relative_path_is_cross_platform() -> None:
    assert normalize_relative_path("notes\\journal\\2026-01-05.md") == (
        "notes/journal/2026-01-05.md"
    )
    assert normalize_relative_path("./notes//journal/../journal/a.md") == (
        "notes/journal/a.md"
    )
    assert normalize_relative_path("C:\\\\notes\\\\a.md") == "notes/a.md"
    assert normalize_text("\ufeff# A\r\n\r\nB\r\n") == "# A\n\nB\n"


def test_separator_excludes_frontmatter_fences() -> None:
    doc = scan_document(
        "---\nid: x\n---\n\nFirst\n\n---\n\nSecond\n\n```\n***\n```\n"
    )

    assert [span.line_start for span in doc.separators] == [7]
    assert len(doc.frontmatter.raw.splitlines()) == 1


def test_malformed_content_is_not_silently_dropped() -> None:
    text = "# H\n\nplain\n\n| broken | table |\n| not a delimiter |\n\nend\n"
    doc = scan_document(text)
    covered = set()
    for spans in (
        doc.headings,
        doc.paragraphs,
        doc.fenced_code,
        doc.tables,
        doc.separators,
        doc.html_blocks,
    ):
        for span in spans:
            covered.update(range(span.line_start, span.line_end + 1))

    assert "| broken | table |" in "\n".join(span.text for span in doc.paragraphs)
    assert covered >= {1, 3, 5, 6, 8}
