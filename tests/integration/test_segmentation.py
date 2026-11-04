"""End-to-end segmentation over fixtures and the synthetic demo corpus."""

from __future__ import annotations

from pathlib import Path

from stratum.markdown import scan_document, scan_file
from stratum.segmentation.profiles import (
    builtin_profile_set,
    load_profiles,
    segment_document,
)

ROOT = Path(__file__).resolve().parents[2]
DEMO = ROOT / "src" / "stratum" / "demo_data"
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "markdown"


def _fixture(name: str):
    return scan_file(FIXTURES / name)


def test_fixture_matrix_has_expected_structure() -> None:
    expected = {
        "atx.md": {"headings": 6, "paragraphs": 6},
        "fence.md": {"headings": 2, "fenced_code": 2},
        "frontmatter.md": {"headings": 2, "frontmatter": True},
        "cjk.md": {"headings": 4},
        "duplicate-heading.md": {"headings": 4},
        "no-heading.md": {"headings": 0, "paragraphs": 3},
        "table.md": {"tables": 1},
        "manual.md": {"headings": 4},
        "prose.md": {"headings": 0, "paragraphs": 4},
        "crlf.md": {"headings": 2},
        "bom.md": {"headings": 2},
    }

    for name, wanted in expected.items():
        doc = _fixture(name)
        if "headings" in wanted:
            assert len(doc.headings) == wanted["headings"], name
        if "paragraphs" in wanted:
            assert len(doc.paragraphs) == wanted["paragraphs"], name
        if "fenced_code" in wanted:
            assert len(doc.fenced_code) == wanted["fenced_code"], name
        if "tables" in wanted:
            assert len(doc.tables) == wanted["tables"], name
        if wanted.get("frontmatter"):
            assert doc.frontmatter is not None, name
        for span in doc.headings:
            assert 1 <= span.line_start <= len(doc.lines), name


def test_heading_profile_segments_demo_document_deterministically() -> None:
    profiles = load_profiles(DEMO / "profiles" / "demo.toml")
    document = scan_file(
        DEMO / "workspaces" / "tidepool-lab" / "projects" / "salinity-sensor.md"
    )
    profile = profiles.get("heading")

    first = segment_document(
        document, profile, relative_path="projects/salinity-sensor.md"
    )
    second = segment_document(
        document, profile, relative_path="projects/salinity-sensor.md"
    )

    assert [draft.anchor for draft in first] == [
        "Background",
        "Sensor calibration window",
        "Deployment notes",
        "Open questions",
    ]
    assert [draft.block_id for draft in first] == [
        draft.block_id for draft in second
    ]
    assert all(draft.doc_id == "projects/salinity-sensor.md" for draft in first)
    assert all(draft.retrieval_type == "structured" for draft in first)
    assert all(draft.snapshot_sha256 for draft in first)
    assert first[1].snapshot_text.startswith("## Sensor calibration window")
    assert first[1].snapshot_text.endswith("All readings in this window are synthetic.")


def test_manual_profile_segments_demo_range() -> None:
    profiles = load_profiles(DEMO / "profiles" / "demo.toml")
    document = scan_file(
        DEMO / "workspaces" / "tidepool-lab" / "projects" / "salinity-sensor.md"
    )

    drafts = segment_document(
        document, profiles.get("manual"), relative_path="projects/salinity-sensor.md"
    )

    assert len(drafts) == 1
    assert (drafts[0].line_start, drafts[0].line_end) == (12, 24)
    assert drafts[0].selector_type == "manual_range"
    assert "calibration run" in drafts[0].snapshot_text


def test_fenced_markdown_headings_are_never_segmented() -> None:
    profiles = builtin_profile_set()
    document = _fixture("fence.md")

    drafts = segment_document(
        document, profiles.get("generic-heading"), relative_path="fence.md"
    )

    anchors = [draft.anchor for draft in drafts]
    assert anchors == ["Real Heading", "Final Heading"]
    assert "Not A Heading" not in anchors
    assert len(drafts) == 2
    assert drafts[0].line_end < drafts[1].line_start
    assert "## Not A Heading" in drafts[0].snapshot_text
    assert "## Not A Heading" not in drafts[1].snapshot_text


def test_crlf_and_bom_produce_identical_block_identity() -> None:
    profiles = builtin_profile_set()
    profile = profiles.get("generic-heading")

    crlf = segment_document(_fixture("crlf.md"), profile, relative_path="same.md")
    bom = segment_document(_fixture("bom.md"), profile, relative_path="same.md")

    assert [draft.block_id for draft in crlf] == [draft.block_id for draft in bom]
    assert [draft.snapshot_sha256 for draft in crlf] == [
        draft.snapshot_sha256 for draft in bom
    ]


def test_prose_profile_end_to_end() -> None:
    profiles = builtin_profile_set()
    document = _fixture("prose.md")

    drafts = segment_document(
        document, profiles.get("prose-paragraph"), relative_path="notes/prose.md"
    )

    assert len(drafts) == 2
    assert [draft.selector_type for draft in drafts] == ["paragraph", "paragraph"]
    assert [draft.retrieval_type for draft in drafts] == ["prose", "prose"]
    assert [draft.line_start for draft in drafts] == [1, 5]
    assert [draft.line_end for draft in drafts] == [3, 7]
    assert all(draft.snapshot_sha256 for draft in drafts)
    assert all(draft.doc_id == "notes/prose.md" for draft in drafts)


def test_resegmentation_of_unchanged_document_reports_no_changes() -> None:
    profiles = load_profiles(DEMO / "profiles" / "demo.toml")
    document = scan_file(
        DEMO / "workspaces" / "tidepool-lab" / "projects" / "salinity-sensor.md"
    )
    profile = profiles.get("heading")
    drafts = segment_document(
        document, profile, relative_path="projects/salinity-sensor.md"
    )

    from stratum.segmentation.resegment import resegment

    result = resegment(drafts, drafts)

    assert len(result.kept) == len(drafts)
    assert result.updated == ()
    assert result.lineage == ()
    assert result.missing == ()
    assert result.created == ()
