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
from .claims import CLAIM_TYPES
from .db import open_db
from .errors import StratumError
from .ingest_report import ingest_report_file
from .ingest_source import ingest_source_file
from .query import format_query_rows, query_items
from .review import (
    ALL_ENTITY_STATUSES,
    bulk_promote,
    list_items,
    promote,
    reject,
    reopen,
    show_item,
)
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
    card_parser.add_argument(
        "--date",
        metavar="YYYY-MM-DD",
        help="card period start; period ends the following day by default",
    )
    card_parser.add_argument(
        "--period-end",
        metavar="YYYY-MM-DD",
        help="exclusive card period end; requires --date",
    )

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

    review_parser = subparsers.add_parser(
        "review", help="review, promote, reject and reopen drafts"
    )
    review_sub = review_parser.add_subparsers(
        dest="review_command", metavar="ACTION"
    )

    review_list = review_sub.add_parser(
        "list", help="list draft claims and cards"
    )
    review_list.add_argument(
        "--type", dest="entity_type", choices=("claim", "card")
    )
    review_list.add_argument(
        "--status", choices=ALL_ENTITY_STATUSES
    )
    review_list.add_argument("--date", metavar="YYYY-MM-DD")
    review_list.add_argument("--limit", type=int, default=50, metavar="N")

    review_show = review_sub.add_parser(
        "show", help="show one claim or card with evidence and audit trail"
    )
    review_show.add_argument("entity_id", metavar="ID")

    for action in ("promote", "reject", "reopen"):
        action_parser = review_sub.add_parser(
            action, help=f"{action} one draft claim or card"
        )
        action_parser.add_argument("entity_id", metavar="ID")
        action_parser.add_argument("--author", required=True, metavar="NAME")
        action_parser.add_argument("--reason", required=True, metavar="TEXT")

    bulk_parser = review_sub.add_parser(
        "bulk-promote", help="promote every card or daily claim for one date"
    )
    bulk_parser.add_argument(
        "--type", required=True, dest="entity_type", choices=("claim", "card")
    )
    bulk_parser.add_argument("--date", required=True, metavar="YYYY-MM-DD")
    bulk_parser.add_argument("--author", required=True, metavar="NAME")
    bulk_parser.add_argument("--reason", required=True, metavar="TEXT")

    query_parser = subparsers.add_parser(
        "query", help="query claims, cards, reports and blocks"
    )
    query_parser.add_argument(
        "--type",
        dest="entity_type",
        default="claim",
        choices=("claim", "card", "report", "block", "prose", "structured"),
    )
    query_parser.add_argument("--from", dest="date_from", metavar="DATE")
    query_parser.add_argument("--to", dest="date_to", metavar="DATE")
    query_parser.add_argument("--status", choices=sorted(
        {
            "draft",
            "active",
            "superseded",
            "rejected",
            "stale",
            "archived",
            "missing",
        }
    ))
    query_parser.add_argument("--claim-type", choices=CLAIM_TYPES)
    query_parser.add_argument("--grep", metavar="TEXT")
    query_parser.add_argument("--include-draft", action="store_true")
    query_parser.add_argument("--limit", type=int, default=50, metavar="N")
    query_parser.add_argument("--context", type=int, default=0, metavar="N")

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
            date=args.date,
            period_end=args.period_end,
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


def _one_line(text: str) -> str:
    return " ".join(str(text).split())


def _truncate(text: str, width: int = 120) -> str:
    flat = _one_line(text)
    if len(flat) <= width:
        return flat
    return flat[: width - 3] + "..."


def _workspace_conn(workspace: str | None) -> sqlite3.Connection:
    selected = resolve_workspace(explicit=workspace)
    database = Path(selected.db_path)
    if str(database) != ":memory:" and not database.is_file():
        raise WorkspaceError(f"workspace database not found: {selected.db_path}")
    return open_db(selected.db_path)


def _review_detail_text(detail) -> str:
    row = detail.row
    lines = [f"{detail.entity_type.capitalize()} {detail.entity_id}"]
    lines.append(f"Status: {detail.status}")
    if detail.entity_type == "claim":
        lines.append(f"Type: {row['claim_type']}")
    else:
        lines.append(f"Type: {row['card_type']}")
        lines.append(f"Title: {_one_line(row['title'])}")
    start = row["period_start"] if "period_start" in row.keys() else None
    end = row["period_end"] if "period_end" in row.keys() else None
    if start and end:
        lines.append(f"Period: {start}..{end}")
    elif start:
        lines.append(f"Period: {start}..")
    else:
        lines.append("Period: -")
    author = row["author"] if "author" in row.keys() else None
    lines.append(f"Author: {author if author else '-'}")
    if detail.entity_type == "claim" and row["data_gap"]:
        lines.append(f"Data gap: {row['data_gap']}")
    lines.append(f"Body: {_one_line(row['body'])}")

    lines.append("Evidence:")
    if not detail.evidence:
        lines.append("  none")
    for item in detail.evidence:
        if detail.entity_type == "claim":
            lines.append(
                f"  {item['target_type']} {item['target_id']} "
                f"({item['role']})"
            )
        else:
            state = item["block_status"] or "missing"
            kind = item["retrieval_type"] or "unknown"
            lines.append(
                f"  block {item['target_id']} ({kind}, {state}, "
                f"{item['role']})"
            )

    lines.append("Relations:")
    if not detail.relations:
        lines.append("  none")
    for item in detail.relations:
        lines.append(f"  {item['relation_type']} -> {item['to_claim_id']}")

    lines.append("Review events:")
    if not detail.events:
        lines.append("  none")
    for event in detail.events:
        lines.append(
            f"  {event['created_at']} {event['action']} "
            f"{event['from_status']} -> {event['to_status']} "
            f"by {event['author']}: {event['reason']}"
        )
    return "\n".join(lines) + "\n"


def _handle_review(args: argparse.Namespace) -> int:
    action = args.review_command
    if action is None:
        print(
            "stratum: error: review requires an action "
            "(list, show, promote, reject, reopen, bulk-promote)",
            file=sys.stderr,
        )
        return EXIT_USAGE

    conn = _workspace_conn(args.workspace)
    try:
        if action == "list":
            items = list_items(
                conn,
                entity_type=args.entity_type,
                status=args.status,
                day=args.date,
                limit=args.limit,
            )
            for item in items:
                _print(
                    f"{item.entity_id}  {item.status}  {item.entity_type}  "
                    f"{item.period}  {_truncate(item.text)}"
                )
            _print(f"{len(items)} items")
            return EXIT_OK

        if action == "show":
            detail = show_item(conn, args.entity_id)
            sys.stdout.write(_review_detail_text(detail))
            return EXIT_OK

        if action in {"promote", "reject", "reopen"}:
            operation = {
                "promote": promote,
                "reject": reject,
                "reopen": reopen,
            }[action]
            result = operation(
                conn,
                args.entity_id,
                author=args.author,
                reason=args.reason,
            )
            label = result.entity_type.capitalize()
            if result.changed:
                _print(
                    f"{label} {result.entity_id}: "
                    f"{result.from_status} -> {result.to_status}"
                )
            else:
                _print(
                    f"{label} {result.entity_id} is already "
                    f"{result.from_status}; no change"
                )
            return EXIT_OK

        if action == "bulk-promote":
            result = bulk_promote(
                conn,
                entity_type=args.entity_type,
                day=args.date,
                author=args.author,
                reason=args.reason,
            )
            for item in result.results:
                label = item.entity_type.capitalize()
                if item.changed:
                    _print(
                        f"{label} {item.entity_id}: "
                        f"{item.from_status} -> {item.to_status}"
                    )
                else:
                    _print(
                        f"{label} {item.entity_id} is already "
                        f"{item.from_status}; no change"
                    )
            _print(
                f"Promoted {result.promoted_count} {result.entity_type}s "
                f"for {result.day}"
            )
            return EXIT_OK

        print(
            f"stratum: error: unknown review action: {action}",
            file=sys.stderr,
        )
        return EXIT_USAGE
    finally:
        conn.close()


def _handle_query(args: argparse.Namespace) -> int:
    conn = _workspace_conn(args.workspace)
    try:
        rows = query_items(
            conn,
            entity_type=args.entity_type,
            date_from=args.date_from,
            date_to=args.date_to,
            status=args.status,
            claim_type=args.claim_type,
            grep=args.grep,
            include_draft=args.include_draft,
            limit=args.limit,
            context=args.context,
        )
        sys.stdout.write(format_query_rows(rows))
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
    if args.command == "review":
        return _handle_review(args)
    if args.command == "query":
        return _handle_query(args)
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
