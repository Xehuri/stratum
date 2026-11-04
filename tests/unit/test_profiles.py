"""Profile loading, validation and resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from stratum.markdown import scan_document
from stratum.segmentation.profiles import (
    ProfileError,
    builtin_profile_set,
    load_profiles,
    load_profiles_text,
    segment_document,
)

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "markdown"


def _doc(name: str):
    return scan_document((FIXTURES / name).read_text(encoding="utf-8"))


def test_load_demo_profiles() -> None:
    profiles = load_profiles(ROOT / "demo" / "profiles" / "demo.toml")

    heading = profiles.get("heading")
    assert heading.selector == "heading"
    assert heading.level == 2
    assert heading.include_heading is True
    assert heading.fallback == "whole_document"
    assert heading.retrieval_type == "structured"
    assert heading.version == 1

    claim = profiles.get("claim")
    assert claim.heading_prefixes == ("OBS-", "FIND-", "CHANGE-", "PATTERN-")

    manual = profiles.get("manual")
    assert manual.selector == "manual_range"
    assert manual.options["start_line"] == 12
    assert manual.options["end_line"] == 24
    assert manual.options["start_anchor"] == "## Sensor calibration window"
    assert manual.options["end_anchor"] == "## Deployment notes"

    assert profiles.workspaces["tidepool-lab"]["root"] == "workspaces/tidepool-lab"
    assert profiles.workspaces["tidepool-lab"]["profile"] == "heading"
    assert profiles.workspaces["lantern-library"]["claim_profile"] == "claim"


def test_builtin_profiles_exist_and_prose_defaults() -> None:
    profiles = builtin_profile_set()

    assert profiles.get("generic-heading").selector == "heading"
    assert profiles.get("generic-heading").fallback == "whole-document"
    prose = profiles.get("prose-paragraph")
    assert prose.selector == "paragraph"
    assert prose.min_chars == 80
    assert prose.max_chars == 4000
    assert prose.retrieval_type == "prose"
    assert profiles.get("whole-document").selector == "whole_document"


def test_explicit_profile_beats_document_hint() -> None:
    profiles = load_profiles(ROOT / "demo" / "profiles" / "demo.toml")
    doc = _doc("frontmatter.md")

    resolved = profiles.resolve(explicit="prose-paragraph", document=doc)

    assert resolved.name == "prose-paragraph"


def test_frontmatter_profile_hint() -> None:
    profiles = builtin_profile_set()
    doc = scan_document("---\nindex_profile: prose-paragraph\n---\n\nA paragraph.\n")

    assert profiles.resolve(document=doc).name == "prose-paragraph"


def test_path_glob_and_workspace_resolution() -> None:
    profiles = load_profiles_text(
        """
[profiles.journal]
selector = "heading"
level = 2

[profiles.notes]
selector = "paragraph"

[workspaces.lab]
root = "lab"
profile = "journal"

[path_profiles]
"inbox/**" = "notes"
"**/*.md" = "notes"
"""
    )
    prose_doc = scan_document("A plain paragraph with no heading.\n")

    assert (
        profiles.resolve(document=prose_doc, relative_path="inbox/a.md").name
        == "notes"
    )
    assert (
        profiles.resolve(document=prose_doc, relative_path="notes/a.md").name
        == "notes"
    )
    assert profiles.resolve(workspace="lab", document=prose_doc).name == "journal"


def test_fallback_chain_uses_document_shape() -> None:
    profiles = builtin_profile_set()

    assert profiles.resolve(document=_doc("atx.md")).name == "generic-heading"
    assert profiles.resolve(document=_doc("no-heading.md")).name == "prose-paragraph"
    assert profiles.resolve(document=scan_document("")).name == "whole-document"


def test_invalid_profiles_report_name_and_field() -> None:
    with pytest.raises(ProfileError, match="bad.*selector|selector.*bad"):
        load_profiles_text("[profiles.bad]\nselector = \"nope\"\n")

    with pytest.raises(ProfileError, match="bad.*level"):
        load_profiles_text("[profiles.bad]\nselector = \"heading\"\nlevel = 9\n")

    with pytest.raises(ProfileError, match="bad.*level"):
        load_profiles_text(
            "[profiles.bad]\nselector = \"heading\"\nlevel = \"two\"\n"
        )

    with pytest.raises(ProfileError, match="bad.*min_chars"):
        load_profiles_text(
            "[profiles.bad]\nselector = \"paragraph\"\nmin_chars = 400\nmax_chars = 80\n"
        )

    with pytest.raises(ProfileError, match="bad.*fallback"):
        load_profiles_text(
            "[profiles.bad]\nselector = \"heading\"\nfallback = \"missing-profile\"\n"
        )

    with pytest.raises(ProfileError, match="bad.*surprise"):
        load_profiles_text(
            "[profiles.bad]\nselector = \"heading\"\nsurprise = true\n"
        )

    with pytest.raises(ProfileError, match="lab.*profile"):
        load_profiles_text(
            """
[profiles.one]
selector = "heading"

[workspaces.lab]
profile = "missing"
"""
        )


def test_profile_fallback_when_selector_matches_nothing() -> None:
    profiles = load_profiles_text(
        """
[profiles.deep-heading]
selector = "heading"
level = 6
fallback = "whole-document"
"""
    )
    doc = _doc("no-heading.md")

    drafts = segment_document(
        doc,
        profiles.get("deep-heading"),
        relative_path="notes/plain.md",
        profile_set=profiles,
    )

    assert len(drafts) == 1
    assert drafts[0].selector_type == "whole_document"
    assert drafts[0].doc_id == "notes/plain.md"


def test_segment_document_uses_frontmatter_doc_id_and_prose_type() -> None:
    profiles = builtin_profile_set()
    doc = _doc("frontmatter.md")

    drafts = segment_document(
        doc,
        profiles.get("prose-paragraph"),
        relative_path="elsewhere/frontmatter.md",
    )

    assert len(drafts) == 1
    assert drafts[0].doc_id == "DOC-2026-01-05"
    assert drafts[0].retrieval_type == "prose"
    assert drafts[0].profile_id == "prose-paragraph"


def test_missing_profile_name_is_a_clear_error() -> None:
    profiles = builtin_profile_set()
    with pytest.raises(ProfileError, match="nope"):
        profiles.get("nope")
    with pytest.raises(ProfileError, match="nope"):
        profiles.resolve(explicit="nope", document=scan_document("text\n"))
