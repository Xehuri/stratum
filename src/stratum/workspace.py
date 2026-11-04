"""Multi-workspace registry, lifecycle and discovery.

A workspace is a Markdown knowledge base with its own SQLite state. The
global registry lives under ``STRATUM_HOME`` (``~/.config/stratum`` on
POSIX, ``%APPDATA%/stratum`` on Windows) and each workspace keeps a
``.stratum/workspace.toml`` marker plus its database.

Discovery order is: explicit ``--workspace``, ``STRATUM_WORKSPACE``, an
upward search for ``.stratum/workspace.toml``, then the registry default.
One command always operates on one workspace.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import tempfile
import tomllib
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path

from .db import SCHEMA_VERSION, open_db
from .migrations import utcnow

REGISTRY_FILENAME = "registry.toml"
REGISTRY_VERSION = 1
STATE_DIRNAME = ".stratum"
META_FILENAME = "workspace.toml"
DEFAULT_DB_FILENAME = "stratum.db"


class WorkspaceError(RuntimeError):
    """A workspace is missing, ambiguous, or cannot be registered."""


@dataclass(frozen=True)
class Workspace:
    workspace_id: str
    name: str
    root: str
    db_path: str
    created_at: str
    last_opened_at: str | None = None
    schema_version: int = SCHEMA_VERSION
    copied_from: str | None = None


@dataclass(frozen=True)
class InitResult:
    workspace: Workspace
    created: bool


@dataclass
class Registry:
    workspaces: dict[str, Workspace] = field(default_factory=dict)
    default_workspace: str | None = None


# ---------------------------------------------------------------------------
# Paths and serialisation
# ---------------------------------------------------------------------------


def stratum_home(home: str | os.PathLike[str] | None = None) -> Path:
    if home is not None:
        return Path(home).expanduser()
    env_home = os.environ.get("STRATUM_HOME")
    if env_home:
        return Path(env_home).expanduser()
    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
        return base / "stratum"
    return Path.home() / ".config" / "stratum"


def registry_path(home: str | os.PathLike[str] | None = None) -> Path:
    return stratum_home(home) / REGISTRY_FILENAME


def workspace_meta_path(root: str | os.PathLike[str]) -> Path:
    return Path(root).expanduser() / STATE_DIRNAME / META_FILENAME


def _toml_string(value: str) -> str:
    # JSON string escaping is a strict subset of TOML basic-string escaping.
    return json.dumps(str(value), ensure_ascii=False)


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        with contextlib.suppress(OSError):
            os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise


@contextlib.contextmanager
def registry_lock(home: str | os.PathLike[str] | None = None):
    home_path = stratum_home(home)
    home_path.mkdir(parents=True, exist_ok=True)
    lock_path = home_path / "registry.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if os.name == "nt":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX)
        yield home_path
    finally:
        try:
            if os.name == "nt":
                import msvcrt

                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _workspace_from_mapping(mapping: dict) -> Workspace:
    try:
        workspace_id = str(mapping["workspace_id"])
        name = str(mapping["name"])
        root = str(mapping["root"])
        db_path = str(mapping["db_path"])
        created_at = str(mapping["created_at"])
    except KeyError as exc:
        raise WorkspaceError(
            f"workspace metadata is missing required field {exc.args[0]!r}"
        ) from exc

    last_opened = mapping.get("last_opened_at")
    copied_from = mapping.get("copied_from")
    return Workspace(
        workspace_id=workspace_id,
        name=name,
        root=root,
        db_path=db_path,
        created_at=created_at,
        last_opened_at=str(last_opened) if last_opened else None,
        schema_version=int(mapping.get("schema_version", SCHEMA_VERSION)),
        copied_from=str(copied_from) if copied_from else None,
    )


def _workspace_to_toml(workspace: Workspace) -> str:
    lines = [
        f"workspace_id = {_toml_string(workspace.workspace_id)}",
        f"name = {_toml_string(workspace.name)}",
        f"root = {_toml_string(workspace.root)}",
        f"db_path = {_toml_string(workspace.db_path)}",
        f"created_at = {_toml_string(workspace.created_at)}",
        f"schema_version = {workspace.schema_version}",
    ]
    if workspace.last_opened_at:
        lines.append(f"last_opened_at = {_toml_string(workspace.last_opened_at)}")
    if workspace.copied_from:
        lines.append(f"copied_from = {_toml_string(workspace.copied_from)}")
    return "\n".join(lines) + "\n"


def _registry_to_toml(registry: Registry) -> str:
    lines = [f"version = {REGISTRY_VERSION}"]
    if registry.default_workspace:
        lines.append(
            f"default_workspace = {_toml_string(registry.default_workspace)}"
        )
    for workspace in sorted(
        registry.workspaces.values(), key=lambda item: (item.created_at, item.name)
    ):
        lines.append("")
        lines.append(f"[workspaces.{_toml_string(workspace.workspace_id)}]")
        lines.append(f"workspace_id = {_toml_string(workspace.workspace_id)}")
        lines.append(f"name = {_toml_string(workspace.name)}")
        lines.append(f"root = {_toml_string(workspace.root)}")
        lines.append(f"db_path = {_toml_string(workspace.db_path)}")
        lines.append(f"created_at = {_toml_string(workspace.created_at)}")
        lines.append(f"schema_version = {workspace.schema_version}")
        if workspace.last_opened_at:
            lines.append(
                f"last_opened_at = {_toml_string(workspace.last_opened_at)}"
            )
        if workspace.copied_from:
            lines.append(f"copied_from = {_toml_string(workspace.copied_from)}")
    return "\n".join(lines) + "\n"


def load_registry(home: str | os.PathLike[str] | None = None) -> Registry:
    path = registry_path(home)
    if not path.is_file():
        return Registry()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise WorkspaceError(f"cannot read workspace registry {path}: {exc}") from exc

    workspaces: dict[str, Workspace] = {}
    raw_workspaces = data.get("workspaces", {})
    if not isinstance(raw_workspaces, dict):
        raise WorkspaceError(f"invalid workspace registry {path}: workspaces is not a table")
    for key, mapping in raw_workspaces.items():
        if not isinstance(mapping, dict):
            raise WorkspaceError(f"invalid workspace registry {path}: entry {key!r}")
        merged = dict(mapping)
        merged.setdefault("workspace_id", key)
        workspace = _workspace_from_mapping(merged)
        workspaces[workspace.workspace_id] = workspace

    default = data.get("default_workspace")
    default_id = str(default) if default else None
    if default_id is not None and default_id not in workspaces:
        default_id = None
    return Registry(workspaces=workspaces, default_workspace=default_id)


def save_registry(
    registry: Registry, home: str | os.PathLike[str] | None = None
) -> Path:
    path = registry_path(home)
    _write_text_atomic(path, _registry_to_toml(registry))
    return path


# ---------------------------------------------------------------------------
# Workspace metadata file
# ---------------------------------------------------------------------------


def _meta_path_for(path: str | os.PathLike[str]) -> Path:
    candidate = Path(path).expanduser()
    if candidate.is_file():
        return candidate
    if candidate.name == STATE_DIRNAME:
        return candidate / META_FILENAME
    return candidate / STATE_DIRNAME / META_FILENAME


def load_workspace(path: str | os.PathLike[str]) -> Workspace:
    meta_path = _meta_path_for(path)
    if not meta_path.is_file():
        raise WorkspaceError(
            f"no workspace metadata found at {meta_path}; "
            "run 'stratum init --root <path>' first"
        )
    try:
        data = tomllib.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise WorkspaceError(
            f"cannot read workspace metadata {meta_path}: {exc}"
        ) from exc
    return _workspace_from_mapping(data)


def _write_workspace_meta(workspace: Workspace, meta_path: Path) -> None:
    _write_text_atomic(meta_path, _workspace_to_toml(workspace))


def _sync_db_metadata(workspace: Workspace) -> None:
    conn = open_db(workspace.db_path)
    try:
        conn.execute(
            """
            INSERT INTO workspace_meta (
                id, workspace_id, name, root, db_path, schema_version,
                copied_from, created_at, updated_at
            ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                workspace_id = excluded.workspace_id,
                name = excluded.name,
                root = excluded.root,
                db_path = excluded.db_path,
                schema_version = excluded.schema_version,
                copied_from = excluded.copied_from,
                updated_at = excluded.updated_at
            """,
            (
                workspace.workspace_id,
                workspace.name,
                workspace.root,
                workspace.db_path,
                workspace.schema_version,
                workspace.copied_from,
                workspace.created_at,
                utcnow(),
            ),
        )
    finally:
        conn.close()


def _ensure_layout(root: Path, db_path: Path) -> None:
    state_dir = root / STATE_DIRNAME
    (state_dir / "profiles").mkdir(parents=True, exist_ok=True)
    (state_dir / "cache").mkdir(parents=True, exist_ok=True)
    existed = db_path.is_file()
    conn = open_db(db_path)
    conn.close()
    if not existed:
        with contextlib.suppress(OSError):
            os.chmod(db_path, 0o600)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def find_workspace_upward(
    start: str | os.PathLike[str] | None = None,
) -> Workspace | None:
    current = Path(start) if start is not None else Path.cwd()
    current = current.expanduser()
    if current.is_file():
        current = current.parent
    current = current.resolve()
    for candidate in (current, *current.parents):
        meta_path = candidate / STATE_DIRNAME / META_FILENAME
        if meta_path.is_file():
            return load_workspace(meta_path)
    return None


def _select_workspace(registry: Registry, reference: str) -> Workspace:
    text = str(reference)
    if text in registry.workspaces:
        return registry.workspaces[text]

    matches = [
        workspace
        for workspace in registry.workspaces.values()
        if workspace.name == text or workspace.workspace_id == text
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise WorkspaceError(
            f"workspace reference {text!r} is ambiguous; use its workspace_id"
        )

    candidate = Path(text).expanduser()
    with contextlib.suppress(OSError):
        resolved = candidate.resolve()
        for workspace in registry.workspaces.values():
            if Path(workspace.root).expanduser().resolve() == resolved:
                return workspace

    raise WorkspaceError(f"workspace not found: {text}")


def _resolve_reference(
    reference: str, home: str | os.PathLike[str] | None = None
) -> Workspace:
    registry = load_registry(home)
    text = str(reference)
    if text in registry.workspaces:
        return registry.workspaces[text]

    candidate = Path(text).expanduser()
    if candidate.exists():
        with contextlib.suppress(WorkspaceError):
            return load_workspace(candidate)

    return _select_workspace(registry, text)


def resolve_workspace(
    explicit: str | None = None,
    *,
    start: str | os.PathLike[str] | None = None,
    home: str | os.PathLike[str] | None = None,
) -> Workspace:
    if explicit:
        return _resolve_reference(explicit, home)

    env_reference = os.environ.get("STRATUM_WORKSPACE")
    if env_reference:
        return _resolve_reference(env_reference, home)

    found = find_workspace_upward(start)
    if found is not None:
        return found

    registry = load_registry(home)
    if (
        registry.default_workspace
        and registry.default_workspace in registry.workspaces
    ):
        return registry.workspaces[registry.default_workspace]
    if len(registry.workspaces) == 1:
        return next(iter(registry.workspaces.values()))

    raise WorkspaceError(
        "no workspace selected; pass --workspace, set STRATUM_WORKSPACE, "
        "or run 'stratum init --root <path>'"
    )


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def init_workspace(
    root: str | os.PathLike[str],
    name: str | None = None,
    db_path: str | os.PathLike[str] | None = None,
    *,
    home: str | os.PathLike[str] | None = None,
    copied_from: str | None = None,
) -> InitResult:
    root_path = Path(root).expanduser().resolve()
    meta_path = root_path / STATE_DIRNAME / META_FILENAME
    home_path = stratum_home(home)

    with registry_lock(home_path):
        registry = load_registry(home_path)

        if meta_path.is_file():
            workspace = load_workspace(meta_path)
            _ensure_layout(Path(workspace.root), Path(workspace.db_path))
            registry.workspaces[workspace.workspace_id] = workspace
            if (
                not registry.default_workspace
                or registry.default_workspace not in registry.workspaces
            ):
                registry.default_workspace = workspace.workspace_id
            save_registry(registry, home_path)
            _sync_db_metadata(workspace)
            return InitResult(workspace=workspace, created=False)

        if db_path is not None:
            database_path = Path(db_path).expanduser().resolve()
        else:
            database_path = root_path / STATE_DIRNAME / DEFAULT_DB_FILENAME

        _ensure_layout(root_path, database_path)

        workspace = Workspace(
            workspace_id=str(uuid.uuid4()),
            name=name or root_path.name or "workspace",
            root=root_path.as_posix(),
            db_path=database_path.as_posix(),
            created_at=utcnow(),
            last_opened_at=None,
            schema_version=SCHEMA_VERSION,
            copied_from=copied_from,
        )
        _write_workspace_meta(workspace, meta_path)
        registry.workspaces[workspace.workspace_id] = workspace
        if not registry.default_workspace:
            registry.default_workspace = workspace.workspace_id
        save_registry(registry, home_path)
        _sync_db_metadata(workspace)
        return InitResult(workspace=workspace, created=True)


def list_workspaces(
    home: str | os.PathLike[str] | None = None,
) -> list[Workspace]:
    registry = load_registry(home)
    return sorted(
        registry.workspaces.values(),
        key=lambda item: (item.name.lower(), item.workspace_id),
    )


def add_workspace(
    path: str | os.PathLike[str],
    *,
    home: str | os.PathLike[str] | None = None,
) -> Workspace:
    workspace = load_workspace(path)
    home_path = stratum_home(home)
    with registry_lock(home_path):
        registry = load_registry(home_path)
        registry.workspaces[workspace.workspace_id] = workspace
        if (
            not registry.default_workspace
            or registry.default_workspace not in registry.workspaces
        ):
            registry.default_workspace = workspace.workspace_id
        save_registry(registry, home_path)
    _sync_db_metadata(workspace)
    return workspace


def remove_workspace(
    reference: str,
    *,
    home: str | os.PathLike[str] | None = None,
) -> Workspace:
    home_path = stratum_home(home)
    with registry_lock(home_path):
        registry = load_registry(home_path)
        workspace = _select_workspace(registry, reference)
        del registry.workspaces[workspace.workspace_id]
        if registry.default_workspace == workspace.workspace_id:
            registry.default_workspace = (
                sorted(registry.workspaces)[0] if registry.workspaces else None
            )
        save_registry(registry, home_path)
    return workspace


def _remap_db_path(
    db_path: str, old_root: Path, new_root: Path
) -> Path:
    path = Path(db_path).expanduser()
    try:
        relative = path.resolve().relative_to(old_root.resolve())
    except (OSError, ValueError):
        return path
    return new_root / relative


def relocate_workspace(
    reference: str,
    root: str | os.PathLike[str] | None = None,
    db_path: str | os.PathLike[str] | None = None,
    *,
    home: str | os.PathLike[str] | None = None,
) -> Workspace:
    if root is None and db_path is None:
        raise WorkspaceError("relocate requires --root and/or --db-path")

    home_path = stratum_home(home)
    with registry_lock(home_path):
        registry = load_registry(home_path)
        workspace = _select_workspace(registry, reference)
        old_root = Path(workspace.root).expanduser()

        if root is not None:
            new_root = Path(root).expanduser().resolve()
            target_meta = new_root / STATE_DIRNAME / META_FILENAME
            if target_meta.is_file():
                existing = load_workspace(target_meta)
                if existing.workspace_id != workspace.workspace_id:
                    raise WorkspaceError(
                        f"target root {new_root} already belongs to a "
                        f"different workspace ({existing.workspace_id})"
                    )
            if db_path is not None:
                new_db = Path(db_path).expanduser().resolve()
            else:
                new_db = _remap_db_path(workspace.db_path, old_root, new_root)
            updated = replace(
                workspace,
                root=new_root.as_posix(),
                db_path=new_db.as_posix(),
            )
        else:
            new_root = old_root
            updated = replace(
                workspace,
                db_path=Path(db_path).expanduser().resolve().as_posix(),
            )

        _ensure_layout(Path(updated.root), Path(updated.db_path))
        _write_workspace_meta(
            updated, Path(updated.root) / STATE_DIRNAME / META_FILENAME
        )
        registry.workspaces[updated.workspace_id] = updated
        if (
            not registry.default_workspace
            or registry.default_workspace not in registry.workspaces
        ):
            registry.default_workspace = updated.workspace_id
        save_registry(registry, home_path)

    _sync_db_metadata(updated)
    return updated


# ---------------------------------------------------------------------------
# Status rendering
# ---------------------------------------------------------------------------


def status_text(conn: sqlite3.Connection, workspace: Workspace) -> str:
    counts = {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("blocks", "cards", "reports")
    }
    head = conn.execute(
        "SELECT id, created_at, author, message FROM commits "
        "ORDER BY seq DESC LIMIT 1"
    ).fetchone()

    lines = [
        "Stratum status",
        f"Workspace: {workspace.name}",
        f"ID: {workspace.workspace_id}",
        f"Root: {workspace.root}",
        f"Database: {workspace.db_path}",
        f"Schema: {workspace.schema_version}",
    ]
    if head is None:
        lines.append("HEAD: none")
    else:
        lines.append(f"HEAD: {head['id'][:7]}  {head['created_at']}")
        lines.append(f"Author: {head['author']}")
        lines.append(f"Message: {head['message']}")

    lines.extend(
        [
            "",
            "Index:",
            f"  blocks {counts['blocks']}",
            f"  cards {counts['cards']}",
            f"  reports {counts['reports']}",
        ]
    )
    return "\n".join(lines) + "\n"
