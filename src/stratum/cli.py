"""Stratum command line shell.

v1 outputs plain text only. Exit codes follow the design contract:
0 success, 1 business failure, 2 argument/environment error, 3 incomplete
or manual intervention required.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from . import __version__
from .db import open_db
from .workspace import (
    WorkspaceError,
    add_workspace,
    init_workspace,
    list_workspaces,
    load_registry,
    relocate_workspace,
    remove_workspace,
    resolve_workspace,
    status_text,
)

EXIT_OK = 0
EXIT_BUSINESS_FAILURE = 1
EXIT_USAGE = 2
EXIT_INCOMPLETE = 3

UNIMPLEMENTED_COMMANDS = (
    "ingest",
    "lint",
    "query",
    "commit",
    "log",
    "diff",
    "restore",
    "fsck",
    "migrate-legacy",
    "demo",
)


class _UsageError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # pragma: no cover - exercised via main
        raise _UsageError(message)


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="stratum",
        description="Stratum: queryable, auditable, versioned timeline for Markdown.",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    parser.add_argument(
        "--workspace",
        "-w",
        metavar="WORKSPACE",
        help="workspace name, id, or path (overrides discovery)",
    )

    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    init_parser = subparsers.add_parser(
        "init", help="initialize a workspace and register it"
    )
    init_parser.add_argument(
        "--root", required=True, metavar="PATH", help="knowledge base root"
    )
    init_parser.add_argument("--name", metavar="NAME", help="display name")
    init_parser.add_argument(
        "--db-path", metavar="PATH", help="store the database outside the root"
    )

    workspace_parser = subparsers.add_parser(
        "workspace", help="manage the multi-workspace registry"
    )
    workspace_sub = workspace_parser.add_subparsers(
        dest="workspace_command", metavar="ACTION"
    )

    add_parser = workspace_sub.add_parser(
        "add", help="register an existing initialized workspace"
    )
    add_parser.add_argument("path", metavar="PATH")

    workspace_sub.add_parser("list", help="list registered workspaces")

    remove_parser = workspace_sub.add_parser(
        "remove", help="remove a workspace from the registry only"
    )
    remove_parser.add_argument("reference", metavar="WORKSPACE")

    relocate_parser = workspace_sub.add_parser(
        "relocate", help="update a registered workspace root or database path"
    )
    relocate_parser.add_argument("reference", metavar="WORKSPACE")
    relocate_parser.add_argument("--root", metavar="PATH")
    relocate_parser.add_argument("--db-path", metavar="PATH")

    status_parser = subparsers.add_parser(
        "status", help="show workspace, HEAD and index counts"
    )
    status_parser.add_argument(
        "--workspace",
        "-w",
        dest="workspace",
        default=argparse.SUPPRESS,
        metavar="WORKSPACE",
        help="workspace name, id, or path (overrides discovery)",
    )
    status_parser.add_argument(
        "--short", action="store_true", help="reserved for compact output"
    )

    for command in UNIMPLEMENTED_COMMANDS:
        command_parser = subparsers.add_parser(
            command, help="reserved for a later work wave"
        )
        command_parser.add_argument(
            "arguments", nargs=argparse.REMAINDER, help=argparse.SUPPRESS
        )

    return parser


def _print(message: str = "") -> None:
    print(message)


def _handle_init(args: argparse.Namespace) -> int:
    result = init_workspace(args.root, name=args.name, db_path=args.db_path)
    workspace = result.workspace
    if result.created:
        _print(f"Initialized workspace: {workspace.name}")
    else:
        _print(f"Workspace already initialized: {workspace.name}")
    _print(f"ID: {workspace.workspace_id}")
    _print(f"Root: {workspace.root}")
    _print(f"Database: {workspace.db_path}")
    return EXIT_OK


def _handle_workspace(args: argparse.Namespace) -> int:
    action = args.workspace_command
    if action is None:
        print(
            "stratum: error: workspace requires an action "
            "(add, list, remove, relocate)",
            file=sys.stderr,
        )
        return EXIT_USAGE

    if action == "add":
        workspace = add_workspace(args.path)
        _print(f"Added workspace: {workspace.name} ({workspace.workspace_id})")
        _print(f"Root: {workspace.root}")
        return EXIT_OK

    if action == "list":
        registry = load_registry()
        workspaces = list_workspaces()
        if not workspaces:
            _print("Workspaces: none")
            _print("Run 'stratum init --root <path>' to create one.")
            return EXIT_OK
        _print(f"Workspaces ({len(workspaces)}):")
        for workspace in workspaces:
            marker = "*" if workspace.workspace_id == registry.default_workspace else " "
            _print(f"  {marker} {workspace.name}  {workspace.workspace_id}")
            _print(f"      root: {workspace.root}")
            _print(f"      db:   {workspace.db_path}")
        return EXIT_OK

    if action == "remove":
        workspace = remove_workspace(args.reference)
        _print(f"Removed workspace from registry: {workspace.name}")
        _print("User files were not modified.")
        return EXIT_OK

    if action == "relocate":
        workspace = relocate_workspace(
            args.reference, root=args.root, db_path=args.db_path
        )
        _print(f"Relocated workspace: {workspace.name}")
        _print(f"Root: {workspace.root}")
        _print(f"Database: {workspace.db_path}")
        return EXIT_OK

    print(f"stratum: error: unknown workspace action: {action}", file=sys.stderr)
    return EXIT_USAGE


def _handle_status(args: argparse.Namespace) -> int:
    workspace = resolve_workspace(explicit=args.workspace)
    database = Path(workspace.db_path)
    if str(database) != ":memory:" and not database.is_file():
        raise WorkspaceError(f"workspace database not found: {workspace.db_path}")

    conn = open_db(workspace.db_path)
    try:
        sys.stdout.write(status_text(conn, workspace))
    finally:
        conn.close()
    return EXIT_OK


def _dispatch(args: argparse.Namespace) -> int:
    if args.command is None:
        print(
            "stratum: error: a command is required (try 'stratum --help')",
            file=sys.stderr,
        )
        return EXIT_USAGE
    if args.command == "init":
        return _handle_init(args)
    if args.command == "workspace":
        return _handle_workspace(args)
    if args.command == "status":
        return _handle_status(args)
    if args.command in UNIMPLEMENTED_COMMANDS:
        print(
            f"stratum: error: '{args.command}' is not implemented yet; "
            "it is reserved for a later work wave",
            file=sys.stderr,
        )
        return EXIT_USAGE
    print(f"stratum: error: unknown command: {args.command}", file=sys.stderr)
    return EXIT_USAGE


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except _UsageError as exc:
        print(f"stratum: error: {exc}", file=sys.stderr)
        print("Try 'stratum --help' for more information.", file=sys.stderr)
        return EXIT_USAGE
    except SystemExit as exc:  # --help / --version
        code = exc.code
        if code is None:
            return EXIT_OK
        return code if isinstance(code, int) else EXIT_USAGE

    try:
        return _dispatch(args)
    except WorkspaceError as exc:
        print(f"stratum: error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except sqlite3.Error as exc:
        print(f"stratum: error: database failure: {exc}", file=sys.stderr)
        return EXIT_BUSINESS_FAILURE
    except (OSError, ValueError) as exc:
        print(f"stratum: error: {exc}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
