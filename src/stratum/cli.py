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
from .add import add_card, add_claim
from .db import open_db
from .errors import StratumError
from .ingest_report import ingest_report_file
from .ingest_source import ingest_source_file
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

    ingest_parser = subparsers.add_parser(
        "ingest", help="index source documents and contract reports"
    )
    ingest_sub = ingest_parser.add_subparsers(
        dest="ingest_command", metavar="ACTION"
    )

    source_parser = ingest_sub.add_parser(
        "source", help="index one Markdown source file"
    )
    source_parser.add_argument("path", metavar="PATH")
    source_parser.add_argument(
        "--profile",
        metavar="NAME",
        help="override the resolved segmentation profile",
    )

    report_parser = ingest_sub.add_parser(
        "report", help="parse and index one Loom v1 report"
    )
    report_parser.add_argument("path", metavar="PATH")

    add_parser = subparsers.add_parser(
        "add", help="create draft cards and claims from text"
    )
    add_sub = add_parser.add_subparsers(dest="add_command", metavar="ACTION")

    card_parser = add_sub.add_parser("card", help="create a draft card")
    card_parser.add_argument(
        "--block",
        action="append",
        required=True,
        metavar="BLOCK",
        help="structured block used as evidence (repeatable)",
    )
    card_parser.add_argument(
        "--type", required=True, dest="card_type", metavar="TYPE"
    )
    card_parser.add_argument("--body", required=True, metavar="TEXT")
    card_parser.add_argument("--title", metavar="TEXT")
    card_parser.add_argument("--author", metavar="NAME")

    claim_parser = add_sub.add_parser("claim", help="create a draft claim")
    claim_parser.add_argument("--claim-id", required=True, metavar="CLAIM_ID")
    claim_parser.add_argument("--claim-type", required=True, metavar="TYPE")
    claim_parser.add_argument("--period-start", required=True, metavar="DATE")
    claim_parser.add_argument("--period-end", required=True, metavar="DATE")
    claim_parser.add_argument("--granularity", required=True, metavar="VALUE")
    claim_parser.add_argument(
        "--entity",
        action="append",
        required=True,
        metavar="ENTITY",
        help="entity name (repeatable)",
    )
    claim_parser.add_argument(
        "--evidence",
        action="append",
        required=True,
        metavar="REF",
        help="card, block or snapshot reference (repeatable)",
    )
    claim_parser.add_argument("--confidence", required=True, metavar="LEVEL")
    claim_parser.add_argument("--body", required=True, metavar="TEXT")
    claim_parser.add_argument("--report-id", metavar="REPORT_ID")
    claim_parser.add_argument("--author", metavar="NAME")

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


def _handle_ingest(args: argparse.Namespace) -> int:
    action = args.ingest_command
    if action is None:
        print(
            "stratum: error: ingest requires an action (source, report)",
            file=sys.stderr,
        )
        return EXIT_USAGE
    if action == "source":
        result = ingest_source_file(
            args.path, profile_name=args.profile, workspace=args.workspace
        )
        if result.status == "unchanged":
            _print("unchanged")
            return EXIT_OK
        _print(f"Ingested source: {result.path}")
        _print(f"Document: {result.document_id}")
        _print(f"Version: {result.version}")
        _print(f"Profile: {result.profile_name}")
        _print(
            f"Blocks: created {result.created}, kept {result.kept}, "
            f"updated {result.updated}, missing {result.missing}, "
            f"lineage {result.lineage}"
        )
        return EXIT_OK
    if action == "report":
        result = ingest_report_file(
            args.path, workspace=args.workspace
        )
        if result.status == "unchanged":
            _print("unchanged")
            return EXIT_OK
        _print(f"Ingested report: {result.report_id}")
        _print(f"Type: {result.report_type}")
        _print(f"Status: {result.status}")
        _print(
            f"Claims: created {result.claims_created}, "
            f"updated {result.claims_updated}, "
            f"rejected {result.claims_rejected}"
        )
        _print(f"Evidence: {result.evidence_count}")
        _print(f"Relations: {result.relations}")
        if result.document is not None:
            _print(
                f"Document: {result.document.path} "
                f"version {result.document.version}"
            )
        return EXIT_OK
    print(f"stratum: error: unknown ingest action: {action}", file=sys.stderr)
    return EXIT_USAGE


def _handle_add(args: argparse.Namespace) -> int:
    action = args.add_command
    if action is None:
        print(
            "stratum: error: add requires an action (card, claim)",
            file=sys.stderr,
        )
        return EXIT_USAGE
    if action == "card":
        result = add_card(
            blocks=list(args.block),
            card_type=args.card_type,
            body=args.body,
            title=args.title,
            author=args.author,
            workspace=args.workspace,
        )
        if result.status == "unchanged":
            _print("unchanged")
            return EXIT_OK
        _print(f"Added card: {result.card_id}")
        _print(f"Blocks: {len(result.block_ids)}")
        return EXIT_OK
    if action == "claim":
        result = add_claim(
            claim_id=args.claim_id,
            claim_type=args.claim_type,
            period_start=args.period_start,
            period_end=args.period_end,
            granularity=args.granularity,
            entities=list(args.entity),
            evidence=list(args.evidence),
            confidence=args.confidence,
            body=args.body,
            report_id=args.report_id,
            author=args.author,
            workspace=args.workspace,
        )
        if result.status == "unchanged":
            _print("unchanged")
            return EXIT_OK
        _print(f"Added claim: {result.claim_id}")
        _print(f"Evidence: {result.evidence_count}")
        return EXIT_OK
    print(f"stratum: error: unknown add action: {action}", file=sys.stderr)
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
    if args.command == "ingest":
        return _handle_ingest(args)
    if args.command == "add":
        return _handle_add(args)
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
    except StratumError as exc:
        print(f"stratum: error: {exc}", file=sys.stderr)
        return exc.exit_code
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
