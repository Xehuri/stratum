"""Selector engine for Markdown segmentation."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from ..markdown import (
    ScannedDocument,
    Span,
    normalize_anchor,
    normalize_text,
)

__all__ = [
    "Selection",
    "SelectorError",
    "apply_prose_limits",
    "select",
    "select_blocks",
]

_TIME_RE = re.compile(r"(?<!\d)([01]?\d|2[0-3]):([0-5]\d)(?!\d)")
_SENTENCE_END_RE = re.compile(r"[。！？；]|[.!?;](?=\s|$)")
_SELECTOR_TYPES = frozenset(
    {
        "heading",
        "time_heading",
        "paragraph",
        "separator",
        "regex",
        "manual_range",
        "manual_anchor",
        "whole_document",
        "frontmatter",
        "fenced_code",
        "table",
        "composite",
    }
)


class SelectorError(ValueError):
    """Raised when a selector spec is invalid or cannot be applied."""


@dataclass(frozen=True)
class Selection:
    """A block selected from a document before identity is assigned."""

    selector_type: str
    anchor: str
    text: str
    line_start: int
    line_end: int
    occurrence: int = 0
    level: int | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


def _spec_mapping(spec: Any) -> Mapping[str, Any]:
    if hasattr(spec, "to_selector_spec"):
        spec = spec.to_selector_spec()
    if not isinstance(spec, Mapping):
        raise SelectorError(f"selector spec must be a mapping, got {type(spec)!r}")
    return spec


def _selector_type(spec: Mapping[str, Any]) -> str:
    selector = spec.get("type", spec.get("selector"))
    if not isinstance(selector, str) or not selector:
        raise SelectorError("selector spec field 'type': required string")
    if selector not in _SELECTOR_TYPES:
        raise SelectorError(f"unknown selector: {selector!r}")
    return selector


def _anchor_from_text(text: str, prefix: str = "text") -> str:
    lines = [normalize_anchor(line) for line in normalize_text(text).split("\n")]
    lines = [line for line in lines if line]
    if not lines:
        return f"{prefix}:"
    start = lines[0][:80]
    end = lines[-1][:80]
    return f"{prefix}:{start}|{end}"


def _levels(spec: Mapping[str, Any]) -> set[int] | None:
    raw = spec.get("level")
    if raw is None:
        return None
    values: Iterable[Any]
    if isinstance(raw, int) and not isinstance(raw, bool):
        values = (raw,)
    elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        values = raw
    else:
        raise SelectorError("selector field 'level': expected integer or list")
    levels: set[int] = set()
    for value in values:
        if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 6:
            raise SelectorError("selector field 'level': expected integer 1..6")
        levels.add(value)
    return levels


def _prefixes(spec: Mapping[str, Any]) -> tuple[str, ...]:
    raw = spec.get("heading_prefixes")
    if raw is None:
        return ()
    if isinstance(raw, str):
        return (raw,)
    if not isinstance(raw, Sequence) or isinstance(raw, bytes):
        raise SelectorError("selector field 'heading_prefixes': expected list")
    prefixes = tuple(str(item) for item in raw)
    if not all(prefixes):
        raise SelectorError("selector field 'heading_prefixes': values must be non-empty")
    return prefixes


def _int_option(spec: Mapping[str, Any], key: str, *, minimum: int | None = None) -> int | None:
    raw = spec.get(key)
    if raw is None:
        return None
    if not isinstance(raw, int) or isinstance(raw, bool):
        raise SelectorError(f"selector field '{key}': expected integer")
    if minimum is not None and raw < minimum:
        raise SelectorError(f"selector field '{key}': expected >= {minimum}")
    return raw


def _make_selection(
    selector_type: str,
    text: str,
    line_start: int,
    line_end: int,
    *,
    anchor: str | None = None,
    level: int | None = None,
    occurrence: int = 0,
    metadata: Mapping[str, Any] | None = None,
) -> Selection:
    normalized_text = normalize_text(text)
    return Selection(
        selector_type=selector_type,
        anchor=normalize_anchor(anchor) if anchor else _anchor_from_text(normalized_text),
        text=normalized_text,
        line_start=line_start,
        line_end=line_end,
        occurrence=occurrence,
        level=level,
        metadata=dict(metadata or {}),
    )


def _trim_blank_lines(
    document: ScannedDocument, start: int, end: int, *, leading: bool
) -> tuple[int, int]:
    while start <= end and not document.lines[start - 1].strip():
        start += 1
    while end >= start and not document.lines[end - 1].strip():
        end -= 1
    return start, end


def _line_for_offset(document: ScannedDocument, base_line: int, offset: int) -> int:
    source = "\n".join(document.lines[base_line - 1 :])
    return base_line + source[:offset].count("\n")


def _sentence_fragments(text: str, max_chars: int) -> list[str]:
    boundaries: list[int] = []
    for match in _SENTENCE_END_RE.finditer(text):
        end = match.end()
        while end < len(text) and text[end] in " \t":
            end += 1
        boundaries.append(end)
    if not boundaries or boundaries[-1] < len(text):
        boundaries.append(len(text))

    fragments: list[str] = []
    start = 0
    for end in boundaries:
        if end > start:
            fragments.append(text[start:end])
        start = end

    result: list[str] = []
    for fragment in fragments:
        if len(fragment) <= max_chars:
            result.append(fragment)
        else:
            result.extend(
                fragment[index : index + max_chars]
                for index in range(0, len(fragment), max_chars)
            )
    return result


def _merge_short(
    document: ScannedDocument,
    selections: Sequence[Selection],
    min_chars: int,
) -> list[Selection]:
    if min_chars <= 0:
        return list(selections)

    merged: list[Selection] = []
    index = 0
    while index < len(selections):
        start = selections[index].line_start
        end = selections[index].line_end
        while index + 1 < len(selections):
            text = "\n".join(document.lines[start - 1 : end])
            if len(text) >= min_chars:
                break
            index += 1
            end = selections[index].line_end
        text = "\n".join(document.lines[start - 1 : end])
        merged.append(
            _make_selection("paragraph", text, start, end, metadata={"prose": True})
        )
        index += 1
    return merged


def _split_long(
    document: ScannedDocument,
    selection: Selection,
    max_chars: int,
) -> list[Selection]:
    if max_chars <= 0 or len(selection.text) <= max_chars:
        return [selection]

    pieces: list[Selection] = []
    cursor = 0
    for fragment in _sentence_fragments(selection.text, max_chars):
        stripped = fragment.strip()
        if not stripped:
            continue
        position = selection.text.find(stripped, cursor)
        if position < 0:
            position = cursor
        cursor = position + len(stripped)
        line_start = _line_for_offset(document, selection.line_start, position)
        line_end = _line_for_offset(
            document, selection.line_start, position + len(stripped) - 1
        )
        pieces.append(
            _make_selection(
                "paragraph",
                stripped,
                line_start,
                line_end,
                metadata=dict(selection.metadata),
            )
        )
    return pieces or [selection]


def apply_prose_limits(
    document: ScannedDocument,
    selections: Sequence[Selection],
    *,
    min_chars: int | None = None,
    max_chars: int | None = None,
) -> list[Selection]:
    """Merge short prose paragraphs and split long ones.

    Splitting uses Chinese and English sentence boundaries first, then a hard
    character limit for runs without punctuation. Concatenating the resulting
    snapshots reproduces the original paragraph apart from whitespace.
    """

    result = list(selections)
    if min_chars is not None:
        result = _merge_short(document, result, min_chars)
    if max_chars is not None:
        result = [
            piece
            for selection in result
            for piece in _split_long(document, selection, max_chars)
        ]
    return result


def _heading_blocks(
    document: ScannedDocument,
    selector_type: str,
    predicate,
    *,
    include_heading: bool,
    filtered: bool,
) -> list[Selection]:
    selections: list[Selection] = []
    headings = document.headings
    for index, heading in enumerate(headings):
        if not predicate(heading):
            continue

        end_line = len(document.lines)
        for later in headings[index + 1 :]:
            if filtered and later.level is not None and heading.level is not None:
                if later.level > heading.level:
                    continue
            end_line = later.line_start - 1
            break

        start_line = heading.line_start if include_heading else heading.line_end + 1
        start_line, end_line = _trim_blank_lines(
            document,
            start_line,
            end_line,
            leading=not include_heading,
        )
        if start_line > end_line:
            continue

        text = "\n".join(document.lines[start_line - 1 : end_line])
        selections.append(
            _make_selection(
                selector_type,
                text,
                start_line,
                end_line,
                anchor=heading.heading_text or f"heading-line-{heading.line_start}",
                level=heading.level,
                metadata={
                    "heading_line_start": heading.line_start,
                    "heading_line_end": heading.line_end,
                    "include_heading": include_heading,
                },
            )
        )
    return selections


def _select_heading(document: ScannedDocument, spec: Mapping[str, Any]) -> list[Selection]:
    levels = _levels(spec)
    prefixes = _prefixes(spec)
    include_heading = bool(spec.get("include_heading", True))
    filtered = levels is not None or bool(prefixes)

    def predicate(heading: Span) -> bool:
        if levels is not None and heading.level not in levels:
            return False
        if prefixes and not any(
            (heading.heading_text or "").startswith(prefix) for prefix in prefixes
        ):
            return False
        return True

    return _heading_blocks(
        document,
        "heading",
        predicate,
        include_heading=include_heading,
        filtered=filtered,
    )


def _select_time_heading(
    document: ScannedDocument, spec: Mapping[str, Any]
) -> list[Selection]:
    levels = _levels(spec)
    include_heading = bool(spec.get("include_heading", True))

    def predicate(heading: Span) -> bool:
        if levels is not None and heading.level not in levels:
            return False
        return bool(_TIME_RE.search(heading.heading_text or ""))

    return _heading_blocks(
        document,
        "time_heading",
        predicate,
        include_heading=include_heading,
        filtered=True,
    )


def _select_paragraph(
    document: ScannedDocument, spec: Mapping[str, Any]
) -> list[Selection]:
    selections = [
        _make_selection("paragraph", span.text, span.line_start, span.line_end)
        for span in document.paragraphs
    ]
    return apply_prose_limits(
        document,
        selections,
        min_chars=_int_option(spec, "min_chars", minimum=0),
        max_chars=_int_option(spec, "max_chars", minimum=0),
    )


def _segment_from_lines(
    document: ScannedDocument,
    start: int,
    end: int,
    selector_type: str,
    **kwargs: Any,
) -> Selection | None:
    start, end = _trim_blank_lines(document, start, end, leading=True)
    if start > end:
        return None
    text = "\n".join(document.lines[start - 1 : end])
    return _make_selection(selector_type, text, start, end, **kwargs)


def _select_separator(
    document: ScannedDocument, spec: Mapping[str, Any]
) -> list[Selection]:
    if not document.separators:
        return []
    content_start = (
        document.frontmatter.line_end + 1 if document.frontmatter else 1
    )
    selections: list[Selection] = []
    start = content_start
    for separator in document.separators:
        block = _segment_from_lines(
            document, start, separator.line_start - 1, "separator"
        )
        if block is not None:
            selections.append(block)
        start = separator.line_end + 1
    block = _segment_from_lines(document, start, len(document.lines), "separator")
    if block is not None:
        selections.append(block)
    return selections


def _select_regex(
    document: ScannedDocument, spec: Mapping[str, Any]
) -> list[Selection]:
    pattern = spec.get("pattern")
    if not isinstance(pattern, str) or not pattern:
        raise SelectorError("selector field 'pattern': required non-empty string")
    try:
        compiled = re.compile(pattern, re.MULTILINE)
    except re.error as exc:
        raise SelectorError(f"invalid regex pattern: {exc}") from exc

    selections: list[Selection] = []
    occurrences: dict[str, int] = {}
    for match in compiled.finditer(document.text):
        text = normalize_text(match.group(0))
        if not text:
            continue
        line_start = document.text.count("\n", 0, match.start()) + 1
        last_offset = max(match.start(), match.end() - 1)
        line_end = document.text.count("\n", 0, last_offset) + 1
        anchor = _anchor_from_text(text, prefix="regex")
        occurrence = occurrences.get(anchor, 0)
        occurrences[anchor] = occurrence + 1
        selections.append(
            _make_selection(
                "regex",
                text,
                line_start,
                line_end,
                anchor=anchor,
                occurrence=occurrence,
            )
        )
    return selections


def _select_manual_range(
    document: ScannedDocument, spec: Mapping[str, Any]
) -> list[Selection]:
    start = _int_option(spec, "start_line", minimum=1)
    end = _int_option(spec, "end_line", minimum=1)
    if start is None or end is None:
        raise SelectorError(
            "selector fields 'start_line' and 'end_line': required for manual_range"
        )
    if not document.lines:
        raise SelectorError(
            f"manual_range line {start}-{end}: document has no lines"
        )
    if start > end:
        raise SelectorError(
            f"manual_range line range {start}-{end}: start_line must be <= end_line"
        )
    if end > len(document.lines):
        raise SelectorError(
            f"manual_range line {end}: document has only {len(document.lines)} lines"
        )

    start_anchor = spec.get("start_anchor")
    end_anchor = spec.get("end_anchor")
    text = "\n".join(document.lines[start - 1 : end])
    manual_anchor = False
    if start_anchor or end_anchor:
        anchor = (
            f"manual:{normalize_anchor(str(start_anchor or ''))}"
            f"|{normalize_anchor(str(end_anchor or ''))}"
        )
        manual_anchor = True
    else:
        anchor = _anchor_from_text(text, prefix="range")
    return [
        _make_selection(
            "manual_range",
            text,
            start,
            end,
            anchor=anchor,
            metadata={
                "manual": True,
                "start_anchor": start_anchor,
                "end_anchor": end_anchor,
                "manual_anchor": manual_anchor,
            },
        )
    ]


def _find_unique_anchor(
    lines: Sequence[str],
    anchor: str,
    *,
    start_index: int = 0,
    label: str,
) -> int:
    normalized = normalize_anchor(anchor)
    if not normalized:
        raise SelectorError(f"{label} anchor: must not be empty")
    matches = [
        index
        for index in range(start_index, len(lines))
        if normalized in normalize_anchor(lines[index])
    ]
    if not matches:
        raise SelectorError(f"{label} anchor not found: {anchor!r}")
    if len(matches) > 1:
        raise SelectorError(f"{label} anchor is ambiguous: {anchor!r}")
    return matches[0]


def _select_manual_anchor(
    document: ScannedDocument, spec: Mapping[str, Any]
) -> list[Selection]:
    start_anchor = spec.get("start_anchor")
    end_anchor = spec.get("end_anchor")
    if not isinstance(start_anchor, str) or not isinstance(end_anchor, str):
        raise SelectorError(
            "selector fields 'start_anchor' and 'end_anchor': required strings"
        )
    if not document.lines:
        raise SelectorError("manual_anchor: document has no lines")

    start_index = _find_unique_anchor(
        document.lines, start_anchor, label="start"
    )
    end_index = _find_unique_anchor(
        document.lines,
        end_anchor,
        start_index=start_index,
        label="end",
    )
    if end_index < start_index:
        raise SelectorError("end anchor appears before start anchor")

    text = "\n".join(document.lines[start_index : end_index + 1])
    anchor = f"manual:{normalize_anchor(start_anchor)}|{normalize_anchor(end_anchor)}"
    return [
        _make_selection(
            "manual_anchor",
            text,
            start_index + 1,
            end_index + 1,
            anchor=anchor,
            metadata={
                "manual": True,
                "start_anchor": start_anchor,
                "end_anchor": end_anchor,
            },
        )
    ]


def _select_whole_document(
    document: ScannedDocument, spec: Mapping[str, Any]
) -> list[Selection]:
    if not document.lines or not any(line.strip() for line in document.lines):
        return []
    text = "\n".join(document.lines)
    return [
        _make_selection(
            "whole_document",
            text,
            1,
            len(document.lines),
            anchor="document",
        )
    ]


def _select_frontmatter(
    document: ScannedDocument, spec: Mapping[str, Any]
) -> list[Selection]:
    frontmatter = document.frontmatter
    if frontmatter is None:
        return []
    text = "\n".join(
        document.lines[frontmatter.line_start - 1 : frontmatter.line_end]
    )
    raw_id = frontmatter.values.get("id")
    anchor = normalize_anchor(str(raw_id)) if raw_id else "frontmatter"
    return [
        _make_selection(
            "frontmatter",
            text,
            frontmatter.line_start,
            frontmatter.line_end,
            anchor=anchor,
            metadata={"values": dict(frontmatter.values), "raw": frontmatter.raw},
        )
    ]


def _select_fenced_code(
    document: ScannedDocument, spec: Mapping[str, Any]
) -> list[Selection]:
    selections: list[Selection] = []
    for span in document.fenced_code:
        info = span.info or ""
        selections.append(
            _make_selection(
                "fenced_code",
                span.text,
                span.line_start,
                span.line_end,
                anchor=f"code:{info}" if info else "code",
                metadata={"info": info},
            )
        )
    return selections


def _select_table(
    document: ScannedDocument, spec: Mapping[str, Any]
) -> list[Selection]:
    return [
        _make_selection(
            "table",
            span.text,
            span.line_start,
            span.line_end,
            anchor=_anchor_from_text(span.text, prefix="table"),
        )
        for span in document.tables
    ]


def _select_composite(
    document: ScannedDocument, spec: Mapping[str, Any]
) -> list[Selection]:
    raw = spec.get("selectors")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)) or not raw:
        raise SelectorError(
            "selector field 'selectors': expected a non-empty list"
        )

    collected: list[tuple[int, Selection]] = []
    for order, sub_spec in enumerate(raw):
        if not isinstance(sub_spec, Mapping):
            raise SelectorError(
                "selector field 'selectors': entries must be mappings"
            )
        merged = dict(sub_spec)
        for key in ("min_chars", "max_chars"):
            if key not in merged and key in spec:
                merged[key] = spec[key]
        for selection in select(document, merged):
            collected.append((order, selection))

    seen: set[tuple[Any, ...]] = set()
    result: list[Selection] = []
    for _, selection in sorted(
        collected, key=lambda item: (item[1].line_start, item[1].line_end, item[0])
    ):
        key = (
            selection.selector_type,
            selection.line_start,
            selection.line_end,
            selection.text,
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(selection)
    return result


_DISPATCH = {
    "heading": _select_heading,
    "time_heading": _select_time_heading,
    "paragraph": _select_paragraph,
    "separator": _select_separator,
    "regex": _select_regex,
    "manual_range": _select_manual_range,
    "manual_anchor": _select_manual_anchor,
    "whole_document": _select_whole_document,
    "frontmatter": _select_frontmatter,
    "fenced_code": _select_fenced_code,
    "table": _select_table,
    "composite": _select_composite,
}


def select(document: ScannedDocument, spec: Any) -> list[Selection]:
    """Apply one selector spec to a scanned document."""

    mapping = _spec_mapping(spec)
    selector_type = _selector_type(mapping)
    return _DISPATCH[selector_type](document, mapping)


def select_blocks(document: ScannedDocument, profile: Any) -> list[Selection]:
    """Apply a profile (or a profile-like selector spec) to a document."""

    if hasattr(profile, "to_selector_spec"):
        return select(document, profile.to_selector_spec())
    return select(document, profile)
