"""Segmentation profiles: TOML loading, validation and resolution."""

from __future__ import annotations

import fnmatch
import re
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from ..markdown import ScannedDocument, normalize_relative_path
from .identity import BlockDraft, build_block_drafts, document_id
from .selectors import select_blocks

__all__ = [
    "Profile",
    "ProfileError",
    "ProfileSet",
    "builtin_profile_set",
    "load_profiles",
    "load_profiles_text",
    "resolve_profile",
    "segment_document",
]

SELECTOR_TYPES = frozenset(
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
RETRIEVAL_TYPES = frozenset({"structured", "prose", "legacy"})

_PROFILE_FIELDS = frozenset(
    {
        "selector",
        "selectors",
        "version",
        "level",
        "include_heading",
        "heading_prefixes",
        "min_chars",
        "max_chars",
        "fallback",
        "retrieval_type",
        "pattern",
        "separator",
        "sentence_split",
        "start_line",
        "end_line",
        "start_anchor",
        "end_anchor",
    }
)
_SELECTOR_SPEC_FIELDS = frozenset(
    {
        "type",
        "selector",
        "level",
        "include_heading",
        "heading_prefixes",
        "min_chars",
        "max_chars",
        "pattern",
        "separator",
        "sentence_split",
        "start_line",
        "end_line",
        "start_anchor",
        "end_anchor",
    }
)
_WORKSPACE_FIELDS = frozenset(
    {"root", "profile", "claim_profile", "profile_globs", "globs"}
)


class ProfileError(ValueError):
    """Raised when a segmentation profile is invalid or missing."""


@dataclass(frozen=True)
class Profile:
    name: str
    selector: str
    version: int = 1
    selectors: tuple[Mapping[str, Any], ...] = ()
    level: int | None = None
    include_heading: bool | None = None
    heading_prefixes: tuple[str, ...] = ()
    min_chars: int | None = None
    max_chars: int | None = None
    fallback: str | None = None
    retrieval_type: str = "structured"
    options: Mapping[str, Any] = field(default_factory=dict)

    def to_selector_spec(self) -> dict[str, Any]:
        if self.selectors:
            spec: dict[str, Any] = {
                "type": "composite",
                "selectors": [dict(item) for item in self.selectors],
            }
        else:
            spec = {"type": self.selector}

        if self.level is not None:
            spec["level"] = self.level
        if self.include_heading is not None:
            spec["include_heading"] = self.include_heading
        if self.heading_prefixes:
            spec["heading_prefixes"] = list(self.heading_prefixes)
        if self.min_chars is not None:
            spec["min_chars"] = self.min_chars
        if self.max_chars is not None:
            spec["max_chars"] = self.max_chars
        for key, value in self.options.items():
            spec.setdefault(key, value)
        return spec


@dataclass(frozen=True)
class ProfileSet:
    profiles: Mapping[str, Profile]
    workspaces: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    path_profiles: tuple[tuple[str, str], ...] = ()
    source: str | None = None

    def __getitem__(self, name: str) -> Profile:
        return self.get(name)

    def __contains__(self, name: object) -> bool:
        return name in self.profiles

    def __iter__(self):
        return iter(self.profiles)

    def __len__(self) -> int:
        return len(self.profiles)

    def get(self, name: str) -> Profile:
        try:
            return self.profiles[name]
        except KeyError as exc:
            raise ProfileError(f"profile '{name}': not found") from exc

    def resolve(
        self,
        *,
        explicit: str | None = None,
        document: ScannedDocument | None = None,
        relative_path: str | None = None,
        workspace: str | None = None,
    ) -> Profile:
        if explicit:
            return self.get(explicit)

        if document is not None:
            hint = document.frontmatter_value("index_profile")
            if not hint:
                hint = document.frontmatter_value("profile")
            if hint:
                return self.get(str(hint))

        workspace_config = self.workspaces.get(workspace) if workspace else None
        if workspace_config is not None:
            workspace_globs = (
                workspace_config.get("profile_globs")
                or workspace_config.get("globs")
                or {}
            )
            matched = _match_glob(relative_path, workspace_globs)
            if matched:
                return self.get(matched)

        matched = _match_glob(relative_path, dict(self.path_profiles))
        if matched:
            return self.get(matched)

        if workspace_config is not None and workspace_config.get("profile"):
            return self.get(str(workspace_config["profile"]))

        if document is not None:
            if document.headings:
                return self.get("generic-heading")
            if document.paragraphs:
                return self.get("prose-paragraph")
        return self.get("whole-document")


def _match_glob(
    relative_path: str | None, globs: Mapping[str, str] | None
) -> str | None:
    if not relative_path or not globs:
        return None
    path = normalize_relative_path(relative_path)
    pure = PurePosixPath(path)
    for pattern, profile_name in globs.items():
        normalized_pattern = str(pattern).replace("\\", "/")
        if PurePosixPath(path).match(normalized_pattern):
            return str(profile_name)
        # PurePosixPath.match treats ** differently from shell globs; a
        # fallback keeps simple "journal/**" patterns predictable.
        if fnmatch.fnmatchcase(path, normalized_pattern):
            return str(profile_name)
    return None


def _error(profile: str, field_name: str, message: str) -> ProfileError:
    return ProfileError(f"profile '{profile}' field '{field_name}': {message}")


def _workspace_error(workspace: str, field_name: str, message: str) -> ProfileError:
    return ProfileError(
        f"workspace '{workspace}' field '{field_name}': {message}"
    )


def _as_int(profile: str, field_name: str, value: Any, *, minimum: int = 1) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise _error(profile, field_name, "expected integer")
    if value < minimum:
        raise _error(profile, field_name, f"expected >= {minimum}")
    return value


def _as_bool(profile: str, field_name: str, value: Any) -> bool:
    if not isinstance(value, bool):
        raise _error(profile, field_name, "expected boolean")
    return value


def _as_string_list(profile: str, field_name: str, value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item for item in value
    ):
        raise _error(profile, field_name, "expected list of non-empty strings")
    return tuple(value)


def _validate_selector_spec(
    profile: str, context: str, spec: Mapping[str, Any]
) -> dict[str, Any]:
    for key in spec:
        if key not in _SELECTOR_SPEC_FIELDS:
            raise _error(profile, context, f"unknown field '{key}'")
    selector = spec.get("type", spec.get("selector"))
    if not isinstance(selector, str) or not selector:
        raise _error(profile, context, "missing selector type")
    if selector not in SELECTOR_TYPES:
        raise _error(profile, context, f"unknown selector '{selector}'")
    normalized = dict(spec)
    normalized["type"] = selector
    normalized.pop("selector", None)
    if "level" in normalized:
        _as_int(profile, f"{context}.level", normalized["level"])
        if normalized["level"] > 6:
            raise _error(profile, f"{context}.level", "expected <= 6")
    if "min_chars" in normalized:
        _as_int(profile, f"{context}.min_chars", normalized["min_chars"], minimum=0)
    if "max_chars" in normalized:
        _as_int(profile, f"{context}.max_chars", normalized["max_chars"], minimum=0)
    if "pattern" in normalized:
        pattern = normalized["pattern"]
        if not isinstance(pattern, str) or not pattern:
            raise _error(profile, f"{context}.pattern", "expected non-empty string")
        try:
            re.compile(pattern)
        except re.error as exc:
            raise _error(profile, f"{context}.pattern", f"invalid regex: {exc}") from exc
    return normalized


def _profile_from_mapping(name: str, mapping: Mapping[str, Any]) -> Profile:
    for key in mapping:
        if key not in _PROFILE_FIELDS:
            raise _error(name, str(key), "unknown field")

    selector = mapping.get("selector")
    raw_selectors = mapping.get("selectors")
    selectors: tuple[Mapping[str, Any], ...] = ()
    if raw_selectors is not None:
        if not isinstance(raw_selectors, list) or not raw_selectors:
            raise _error(name, "selectors", "expected a non-empty list")
        if selector is not None and selector != "composite":
            raise _error(name, "selector", "expected 'composite' when selectors are set")
        selector = "composite"
        normalized_specs: list[dict[str, Any]] = []
        for index, raw_spec in enumerate(raw_selectors):
            if not isinstance(raw_spec, Mapping):
                raise _error(name, f"selectors[{index}]", "expected a mapping")
            normalized_specs.append(
                _validate_selector_spec(name, f"selectors[{index}]", raw_spec)
            )
        selectors = tuple(normalized_specs)
    elif not isinstance(selector, str) or not selector:
        raise _error(name, "selector", "required string")
    elif selector not in SELECTOR_TYPES:
        raise _error(name, "selector", f"unknown selector '{selector}'")

    version = _as_int(name, "version", mapping.get("version", 1))

    level = None
    if "level" in mapping:
        level = _as_int(name, "level", mapping["level"])
        if level > 6:
            raise _error(name, "level", "expected <= 6")

    include_heading = None
    if "include_heading" in mapping:
        include_heading = _as_bool(name, "include_heading", mapping["include_heading"])

    heading_prefixes: tuple[str, ...] = ()
    if "heading_prefixes" in mapping:
        heading_prefixes = _as_string_list(
            name, "heading_prefixes", mapping["heading_prefixes"]
        )

    min_chars = None
    if "min_chars" in mapping:
        min_chars = _as_int(name, "min_chars", mapping["min_chars"], minimum=0)
    max_chars = None
    if "max_chars" in mapping:
        max_chars = _as_int(name, "max_chars", mapping["max_chars"], minimum=0)
    if min_chars is not None and max_chars is not None and min_chars > max_chars:
        raise _error(name, "min_chars", "must be <= max_chars")

    fallback = mapping.get("fallback")
    if fallback is not None and (not isinstance(fallback, str) or not fallback):
        raise _error(name, "fallback", "expected non-empty profile name")

    default_retrieval = (
        "prose" if selector in {"paragraph", "whole_document"} else "structured"
    )
    retrieval_type = mapping.get("retrieval_type", default_retrieval)
    if retrieval_type not in RETRIEVAL_TYPES:
        raise _error(
            name,
            "retrieval_type",
            "expected one of structured, prose, legacy",
        )

    options: dict[str, Any] = {}
    for key in (
        "pattern",
        "separator",
        "sentence_split",
        "start_line",
        "end_line",
        "start_anchor",
        "end_anchor",
    ):
        if key in mapping:
            options[key] = mapping[key]

    if "pattern" in options:
        pattern = options["pattern"]
        if not isinstance(pattern, str) or not pattern:
            raise _error(name, "pattern", "expected non-empty string")
        try:
            re.compile(pattern)
        except re.error as exc:
            raise _error(name, "pattern", f"invalid regex: {exc}") from exc
    for key in ("start_line", "end_line"):
        if key in options:
            options[key] = _as_int(name, key, options[key])
    for key in ("start_anchor", "end_anchor"):
        if key in options and not isinstance(options[key], str):
            raise _error(name, key, "expected string")
    if "sentence_split" in options:
        options["sentence_split"] = _as_bool(
            name, "sentence_split", options["sentence_split"]
        )
    if "separator" in options and not isinstance(options["separator"], str):
        raise _error(name, "separator", "expected string")

    if selectors:
        # Keep the composite's child specs in options-free form; they are
        # already complete selector specs.
        pass

    return Profile(
        name=name,
        selector=selector,
        version=version,
        selectors=selectors,
        level=level,
        include_heading=include_heading,
        heading_prefixes=heading_prefixes,
        min_chars=min_chars,
        max_chars=max_chars,
        fallback=fallback,
        retrieval_type=str(retrieval_type),
        options=options,
    )


def _builtin_profiles() -> dict[str, Profile]:
    profiles = {
        "generic-heading": Profile(
            name="generic-heading",
            selector="heading",
            include_heading=True,
            fallback="whole-document",
            retrieval_type="structured",
        ),
        "prose-paragraph": Profile(
            name="prose-paragraph",
            selector="paragraph",
            min_chars=80,
            max_chars=4000,
            fallback="whole-document",
            retrieval_type="prose",
        ),
        "whole-document": Profile(
            name="whole-document",
            selector="whole_document",
            retrieval_type="prose",
        ),
    }
    # The provisional demo profile refers to ``whole_document`` with an
    # underscore. Keep an alias so both spellings resolve.
    for name in tuple(profiles):
        alias = name.replace("-", "_")
        if alias != name and alias not in profiles:
            profiles[alias] = replace(profiles[name], name=alias)
    return profiles


def builtin_profile_set() -> ProfileSet:
    return ProfileSet(profiles=_builtin_profiles())


def _build_profile_set(data: Mapping[str, Any], source: str | None) -> ProfileSet:
    profiles = _builtin_profiles()

    raw_profiles = data.get("profiles", {})
    if not isinstance(raw_profiles, Mapping):
        raise ProfileError("field 'profiles': expected a table")
    for name, mapping in raw_profiles.items():
        if not isinstance(name, str) or not name:
            raise ProfileError("field 'profiles': profile names must be non-empty strings")
        if not isinstance(mapping, Mapping):
            raise ProfileError(f"profile '{name}': expected a table")
        profiles[name] = _profile_from_mapping(name, mapping)

    for profile in profiles.values():
        if profile.fallback and profile.fallback not in profiles:
            raise _error(
                profile.name,
                "fallback",
                f"unknown profile '{profile.fallback}'",
            )

    raw_path_profiles = data.get("path_profiles", {})
    if not isinstance(raw_path_profiles, Mapping):
        raise ProfileError("field 'path_profiles': expected a table")
    path_profiles: list[tuple[str, str]] = []
    for pattern, profile_name in raw_path_profiles.items():
        if not isinstance(pattern, str) or not pattern:
            raise ProfileError("field 'path_profiles': patterns must be non-empty")
        if not isinstance(profile_name, str) or profile_name not in profiles:
            raise ProfileError(
                f"field 'path_profiles' pattern '{pattern}': unknown profile "
                f"'{profile_name}'"
            )
        path_profiles.append((pattern, profile_name))

    raw_workspaces = data.get("workspaces", {})
    if not isinstance(raw_workspaces, Mapping):
        raise ProfileError("field 'workspaces': expected a table")
    workspaces: dict[str, Mapping[str, Any]] = {}
    for name, mapping in raw_workspaces.items():
        if not isinstance(name, str) or not name:
            raise ProfileError("field 'workspaces': names must be non-empty strings")
        if not isinstance(mapping, Mapping):
            raise _workspace_error(name, "config", "expected a table")
        for key in mapping:
            if key not in _WORKSPACE_FIELDS:
                raise _workspace_error(name, str(key), "unknown field")
        for key in ("profile", "claim_profile"):
            if key in mapping:
                profile_name = mapping[key]
                if not isinstance(profile_name, str) or profile_name not in profiles:
                    raise _workspace_error(
                        name, key, f"unknown profile '{profile_name}'"
                    )
        for key in ("profile_globs", "globs"):
            if key in mapping:
                globs = mapping[key]
                if not isinstance(globs, Mapping):
                    raise _workspace_error(name, key, "expected a table")
                for pattern, profile_name in globs.items():
                    if (
                        not isinstance(pattern, str)
                        or not pattern
                        or not isinstance(profile_name, str)
                        or profile_name not in profiles
                    ):
                        raise _workspace_error(
                            name, key, f"invalid glob entry '{pattern}'"
                        )
        workspaces[name] = dict(mapping)

    return ProfileSet(
        profiles=profiles,
        workspaces=workspaces,
        path_profiles=tuple(path_profiles),
        source=source,
    )


def load_profiles_text(text: str, source: str | None = None) -> ProfileSet:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ProfileError(f"invalid TOML: {exc}") from exc
    return _build_profile_set(data, source)


def load_profiles(path: str | Path) -> ProfileSet:
    location = Path(path).expanduser()
    if location.is_dir():
        merged: dict[str, Any] = {
            "profiles": {},
            "workspaces": {},
            "path_profiles": {},
        }
        for item in sorted(location.glob("*.toml")):
            try:
                data = tomllib.loads(item.read_text(encoding="utf-8"))
            except tomllib.TOMLDecodeError as exc:
                raise ProfileError(f"invalid TOML in {item}: {exc}") from exc
            for section in ("profiles", "workspaces", "path_profiles"):
                section_data = data.get(section, {})
                if not isinstance(section_data, Mapping):
                    raise ProfileError(
                        f"field '{section}' in {item}: expected a table"
                    )
                merged[section].update(section_data)
        return _build_profile_set(merged, source=str(location))
    return load_profiles_text(
        location.read_text(encoding="utf-8"), source=str(location)
    )


def resolve_profile(
    profile_set: ProfileSet,
    *,
    explicit: str | None = None,
    document: ScannedDocument | None = None,
    relative_path: str | None = None,
    workspace: str | None = None,
) -> Profile:
    """Module-level convenience wrapper around :meth:`ProfileSet.resolve`."""

    return profile_set.resolve(
        explicit=explicit,
        document=document,
        relative_path=relative_path,
        workspace=workspace,
    )


def segment_document(
    document: ScannedDocument,
    profile: Profile | str,
    *,
    relative_path: str | None = None,
    doc_id: str | None = None,
    profile_set: ProfileSet | None = None,
    legacy_block_ids: Mapping[Any, str] | None = None,
) -> list[BlockDraft]:
    """Run a profile (with fallback) and return block drafts."""

    if isinstance(profile, str):
        if profile_set is None:
            profile_set = builtin_profile_set()
        profile = profile_set.get(profile)

    active = profile
    selections = select_blocks(document, active)
    seen = {active.name}
    while not selections and active.fallback and active.fallback not in seen:
        seen.add(active.fallback)
        active = (profile_set or builtin_profile_set()).get(active.fallback)
        selections = select_blocks(document, active)

    if not selections:
        return []

    frontmatter_values = (
        document.frontmatter.values if document.frontmatter is not None else {}
    )
    identifier = doc_id or document_id(frontmatter_values, relative_path or "")
    return build_block_drafts(
        identifier,
        active.name,
        selections,
        retrieval_type=active.retrieval_type,
        legacy_block_ids=legacy_block_ids,
    )
