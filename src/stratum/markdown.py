"""Markdown scanning for segmentation.

The scanner is deliberately small and deterministic. It does not try to be a
full CommonMark implementation; it recognises the structures Stratum needs to
segment safely (frontmatter, headings, fences, tables, separators, HTML and
paragraphs) and reports every structure as a line span so malformed or
unsupported content is never silently dropped.
"""

from __future__ import annotations

import fnmatch
import posixpath
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

__all__ = [
    "FrontMatter",
    "ScannedDocument",
    "Span",
    "normalize_anchor",
    "normalize_relative_path",
    "normalize_text",
    "resolve_default_profile",
    "scan_document",
    "scan_file",
]

_BOM = "\ufeff"
_ATX_RE = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?[ \t]*$")
_SETEXT_RE = re.compile(r"^ {0,3}(=+|-+)[ \t]*$")
_FENCE_OPEN_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_SEPARATOR_RE = re.compile(
    r"^ {0,3}(?:(?:\*[ \t]*){3,}|(?:-[ \t]*){3,}|(?:_[ \t]*){3,})$"
)
_TABLE_DELIMITER_CELL_RE = re.compile(r"^[ \t]*:?-{1,}:?[ \t]*$")
_HTML_START_RE = re.compile(
    r"^ {0,3}<(?:[A-Za-z][\w:-]*|!--|!DOCTYPE|/[A-Za-z][\w:-]*)"
)
_LIST_OR_BLOCK_RE = re.compile(r"^(?:[-*+>|<#`~]|\d+[.)])[ \t]")


@dataclass(frozen=True)
class Span:
    """A structural span in a scanned Markdown document.

    ``line_start`` and ``line_end`` are 1-based and inclusive. ``text`` is
    always LF-normalised and never includes the trailing newline of the
    document.
    """

    kind: str
    line_start: int
    line_end: int
    text: str
    level: int | None = None
    info: str | None = None
    heading_text: str | None = None
    occurrence: int = 0


@dataclass(frozen=True)
class FrontMatter:
    """Raw YAML frontmatter text plus simple scalar values."""

    raw: str
    values: Mapping[str, Any]
    line_start: int
    line_end: int

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)


@dataclass(frozen=True)
class ScannedDocument:
    """A parsed Markdown document and its structural spans."""

    text: str
    lines: tuple[str, ...]
    frontmatter: FrontMatter | None = None
    headings: tuple[Span, ...] = ()
    paragraphs: tuple[Span, ...] = ()
    fenced_code: tuple[Span, ...] = ()
    tables: tuple[Span, ...] = ()
    separators: tuple[Span, ...] = ()
    html_blocks: tuple[Span, ...] = ()

    def frontmatter_value(self, key: str, default: Any = None) -> Any:
        if self.frontmatter is None:
            return default
        return self.frontmatter.get(key, default)

    def suggest_profile(
        self,
        relative_path: str | None = None,
        glob_profiles: Mapping[str, str] | None = None,
    ) -> str:
        return resolve_default_profile(
            self,
            relative_path=relative_path,
            glob_profiles=glob_profiles,
        )

    @property
    def spans(self) -> tuple[Span, ...]:
        spans: list[Span] = []
        for group in (
            self.headings,
            self.paragraphs,
            self.fenced_code,
            self.tables,
            self.separators,
            self.html_blocks,
        ):
            spans.extend(group)
        return tuple(sorted(spans, key=lambda span: (span.line_start, span.line_end)))


def normalize_text(value: str | bytes) -> str:
    """Normalise a document for parsing and hashing.

    A leading UTF-8 BOM is removed and every line ending becomes ``\\n``.
    """

    if isinstance(value, bytes):
        value = value.decode("utf-8-sig")
    if value.startswith(_BOM):
        value = value[1:]
    return value.replace("\r\n", "\n").replace("\r", "\n")


def normalize_anchor(value: str) -> str:
    """Normalise heading or text used as an anchor."""

    normalized = unicodedata.normalize("NFC", value)
    return re.sub(r"\s+", " ", normalized).strip()


def normalize_relative_path(value: str | Path) -> str:
    """Return a platform-independent POSIX relative path."""

    text = unicodedata.normalize("NFC", str(value)).replace("\\", "/").strip()
    text = re.sub(r"^[A-Za-z]:", "", text)
    text = text.lstrip("/")
    normalized = posixpath.normpath(text)
    return "" if normalized == "." else normalized


def _parse_scalar(value: str) -> Any:
    text = value.strip()
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        if not inner:
            return []
        return [_unquote(item.strip()) for item in inner.split(",")]
    return _unquote(text)


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _parse_frontmatter(lines: tuple[str, ...]) -> FrontMatter | None:
    if not lines or lines[0].strip() != "---":
        return None
    closing = None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            closing = index
            break
    if closing is None:
        return None

    values: dict[str, Any] = {}
    for raw_line in lines[1:closing]:
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        key = key.strip()
        if key:
            values[key] = _parse_scalar(value)

    return FrontMatter(
        raw="\n".join(lines[1:closing]),
        values=values,
        line_start=1,
        line_end=closing + 1,
    )


def _is_table_delimiter(line: str) -> bool:
    stripped = line.strip()
    if "|" not in stripped or "-" not in stripped:
        return False
    cells = stripped.strip("|").split("|")
    return bool(cells) and all(
        _TABLE_DELIMITER_CELL_RE.match(cell) for cell in cells
    )


def _match_path_profile(
    relative_path: str | None,
    glob_profiles: Mapping[str, str] | None,
) -> str | None:
    if not relative_path or not glob_profiles:
        return None
    path = normalize_relative_path(relative_path)
    pure = PurePosixPath(path)
    for pattern, profile in glob_profiles.items():
        normalized_pattern = str(pattern).replace("\\", "/")
        if fnmatch.fnmatchcase(path, normalized_pattern):
            return str(profile)
        try:
            if pure.match(normalized_pattern):
                return str(profile)
        except ValueError:
            continue
    return None


def resolve_default_profile(
    document: ScannedDocument,
    relative_path: str | None = None,
    glob_profiles: Mapping[str, str] | None = None,
) -> str:
    """Resolve the design fallback order for an unknown document.

    ``frontmatter.index_profile`` > path glob profile > ``generic-heading`` >
    ``prose-paragraph`` > ``whole-document``.
    """

    hint = document.frontmatter_value("index_profile")
    if hint:
        return str(hint)
    matched = _match_path_profile(relative_path, glob_profiles)
    if matched:
        return matched
    if document.headings:
        return "generic-heading"
    if document.paragraphs:
        return "prose-paragraph"
    return "whole-document"


def _heading_content(raw: str) -> str:
    return re.sub(r"[ \t]+#+[ \t]*$", "", raw).strip()


def scan_document(value: str | bytes) -> ScannedDocument:
    """Scan Markdown into structural spans."""

    text = normalize_text(value)
    raw_lines = text.split("\n")
    if raw_lines and raw_lines[-1] == "":
        raw_lines.pop()
    lines = tuple(raw_lines)
    line_count = len(lines)
    occupied: list[str | None] = [None] * line_count

    frontmatter = _parse_frontmatter(lines)
    content_start = 0
    if frontmatter is not None:
        for index in range(frontmatter.line_end):
            occupied[index] = "frontmatter"
        content_start = frontmatter.line_end

    # Fenced code first: nothing inside a fence may become another structure.
    fenced_code: list[Span] = []
    index = content_start
    while index < line_count:
        if occupied[index] is not None:
            index += 1
            continue
        match = _FENCE_OPEN_RE.match(lines[index])
        if match is None:
            index += 1
            continue

        marker = match.group(1)
        character = marker[0]
        info = match.group(2).strip()
        closing_re = re.compile(
            rf"^ {{0,3}}{re.escape(character)}{{{len(marker)},}}[ \t]*$"
        )
        closing = None
        cursor = index + 1
        while cursor < line_count:
            if closing_re.match(lines[cursor]):
                closing = cursor
                break
            cursor += 1

        end = closing if closing is not None else line_count - 1
        for position in range(index, end + 1):
            occupied[position] = "fenced_code"
        fenced_code.append(
            Span(
                kind="fenced_code",
                line_start=index + 1,
                line_end=end + 1,
                text="\n".join(lines[index : end + 1]),
                info=info,
            )
        )
        index = end + 1

    # HTML blocks: block-level lines starting with a tag or comment.
    html_blocks: list[Span] = []
    index = content_start
    while index < line_count:
        if occupied[index] is not None or not _HTML_START_RE.match(lines[index]):
            index += 1
            continue
        end = index
        cursor = index + 1
        while cursor < line_count and lines[cursor].strip():
            end = cursor
            cursor += 1
        for position in range(index, end + 1):
            occupied[position] = "html_block"
        html_blocks.append(
            Span(
                kind="html_block",
                line_start=index + 1,
                line_end=end + 1,
                text="\n".join(lines[index : end + 1]),
            )
        )
        index = end + 1

    # Headings: ATX and Setext, never inside fenced code.
    heading_records: list[tuple[int, int, int, str]] = []
    index = content_start
    while index < line_count:
        if occupied[index] is not None:
            index += 1
            continue

        match = _ATX_RE.match(lines[index])
        if match is not None:
            level = len(match.group(1))
            heading_records.append(
                (index, index, level, _heading_content(match.group(2) or ""))
            )
            occupied[index] = "heading"
            index += 1
            continue

        if (
            index + 1 < line_count
            and occupied[index + 1] is None
            and lines[index].strip()
            and _SETEXT_RE.match(lines[index + 1])
            and not _LIST_OR_BLOCK_RE.match(lines[index].strip())
            and not lines[index].lstrip().startswith("|")
        ):
            underline = _SETEXT_RE.match(lines[index + 1])
            assert underline is not None
            level = 1 if underline.group(1)[0] == "=" else 2
            heading_records.append(
                (index, index + 1, level, lines[index].strip())
            )
            occupied[index] = "heading"
            occupied[index + 1] = "heading"
            index += 2
            continue

        index += 1

    occurrences: dict[str, int] = {}
    headings: list[Span] = []
    for start, end, level, heading_text in sorted(heading_records):
        key = normalize_anchor(heading_text)
        occurrence = occurrences.get(key, 0)
        occurrences[key] = occurrence + 1
        headings.append(
            Span(
                kind="heading",
                line_start=start + 1,
                line_end=end + 1,
                text="\n".join(lines[start : end + 1]),
                level=level,
                heading_text=heading_text,
                occurrence=occurrence,
            )
        )

    # Tables: a header row followed by a delimiter row.
    tables: list[Span] = []
    index = content_start
    while index < line_count:
        if occupied[index] is not None:
            index += 1
            continue
        if (
            "|" in lines[index]
            and index + 1 < line_count
            and occupied[index + 1] is None
            and _is_table_delimiter(lines[index + 1])
        ):
            end = index + 1
            cursor = index + 2
            while (
                cursor < line_count
                and occupied[cursor] is None
                and lines[cursor].strip()
                and "|" in lines[cursor]
            ):
                end = cursor
                cursor += 1
            for position in range(index, end + 1):
                occupied[position] = "table"
            tables.append(
                Span(
                    kind="table",
                    line_start=index + 1,
                    line_end=end + 1,
                    text="\n".join(lines[index : end + 1]),
                )
            )
            index = end + 1
            continue
        index += 1

    # Separators: horizontal rules outside frontmatter and fences.
    separators: list[Span] = []
    index = content_start
    while index < line_count:
        if occupied[index] is None and _SEPARATOR_RE.match(lines[index]):
            occupied[index] = "separator"
            separators.append(
                Span(
                    kind="separator",
                    line_start=index + 1,
                    line_end=index + 1,
                    text=lines[index],
                )
            )
        index += 1

    # Remaining non-blank lines are paragraphs.
    paragraphs: list[Span] = []
    index = content_start
    while index < line_count:
        if occupied[index] is not None or not lines[index].strip():
            index += 1
            continue
        start = index
        while (
            index < line_count
            and occupied[index] is None
            and lines[index].strip()
        ):
            index += 1
        paragraphs.append(
            Span(
                kind="paragraph",
                line_start=start + 1,
                line_end=index,
                text="\n".join(lines[start:index]),
            )
        )

    return ScannedDocument(
        text=text,
        lines=lines,
        frontmatter=frontmatter,
        headings=tuple(headings),
        paragraphs=tuple(paragraphs),
        fenced_code=tuple(fenced_code),
        tables=tuple(tables),
        separators=tuple(separators),
        html_blocks=tuple(html_blocks),
    )


def scan_file(path: str | Path) -> ScannedDocument:
    """Read and scan a Markdown file as UTF-8 with optional BOM."""

    return scan_document(Path(path).read_bytes())
