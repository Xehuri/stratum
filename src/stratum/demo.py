"""Deterministic synthetic demo runner for ``stratum demo``.

The corpus ships inside the package at ``stratum/demo_data`` so the demo works
from any working directory, including a clean wheel install. The runner never
writes into the packaged tree: it copies the synthetic workspaces into a
temporary directory, initialises one workspace per copy, ingests every Markdown
source, creates one manual card and one L2 claim, runs fsck, commits each
workspace and prints a fixed plain-text summary.

Output deliberately excludes temporary absolute paths, timestamps, UUIDs and
the random fsck run id, so two runs on the same corpus produce identical text.
``--keep`` preserves the temporary directory and prints its path as the last
line.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .add import add_card, add_claim
from .db import open_db
from .errors import BusinessError, UsageProblem
from .fsck import run_fsck
from .history import commit as history_commit, list_commits
from .ingest_source import ingest_source_file
from .query import query_items
from .review import promote
from .workspace import init_workspace

__all__ = ["DemoWorkspace", "run_demo"]

CLAIM_ID = "OBS-D-2026-02-02-01"
CARD_DATE = "2026-01-05"
DEMO_AUTHOR = "demo"


@dataclass(frozen=True)
class DemoWorkspace:
    name: str
    workspace_id: str
    root: Path
    db_path: str


def _default_demo_root() -> Path:
    return Path(__file__).resolve().parent / "demo_data"


def _resolve_demo_root(root: str | os.PathLike[str] | None) -> Path:
    candidate = _default_demo_root() if root is None else Path(root)
    candidate = candidate.expanduser()
    try:
        resolved = candidate.resolve()
    except OSError as exc:
        raise UsageProblem(f"cannot resolve demo root {candidate}: {exc}") from exc
    if not resolved.is_dir():
        raise UsageProblem(f"demo root not found: {resolved}")
    if not (resolved / "workspaces").is_dir():
        raise UsageProblem(f"demo root has no workspaces directory: {resolved}")
    if not (resolved / "profiles").is_dir():
        raise UsageProblem(f"demo root has no profiles directory: {resolved}")
    return resolved


def _workspace_sources(demo_root: Path) -> list[Path]:
    workspaces_dir = demo_root / "workspaces"
    sources = sorted(
        (
            item
            for item in workspaces_dir.iterdir()
            if item.is_dir() and not item.name.startswith(".")
        ),
        key=lambda item: item.name,
    )
    if not sources:
        raise UsageProblem(f"demo has no synthetic workspaces: {workspaces_dir}")
    return sources


def _profile_files(demo_root: Path) -> list[Path]:
    profiles = sorted((demo_root / "profiles").glob("*.toml"))
    if not profiles:
        raise UsageProblem(f"demo has no profile files: {demo_root / 'profiles'}")
    return profiles


def _copy_workspace(source: Path, destination: Path) -> None:
    shutil.copytree(
        source,
        destination,
        ignore=shutil.ignore_patterns(
            ".stratum",
            "*.db",
            "*.db-wal",
            "*.db-shm",
            "*.sqlite",
            "*.sqlite3",
        ),
    )


def _install_profiles(root: Path, profiles: list[Path]) -> None:
    target = root / ".stratum" / "profiles"
    target.mkdir(parents=True, exist_ok=True)
    for profile in profiles:
        shutil.copyfile(profile, target / profile.name)


def _ingest_tree(workspace: DemoWorkspace) -> None:
    for path in sorted(workspace.root.rglob("*.md")):
        relative_parts = path.relative_to(workspace.root).parts
        if ".stratum" in relative_parts:
            continue
        ingest_source_file(path, workspace=workspace.workspace_id)


def _count(conn, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _manual_block(conn) -> tuple[str, int, int]:
    row = conn.execute(
        """
        SELECT id, line_start, line_end FROM blocks
        WHERE selector_type = 'manual_range'
        ORDER BY id LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise UsageProblem(
            "demo corpus is missing a manual_range block; "
            "check the manual profile and source line range"
        )
    return str(row["id"]), int(row["line_start"]), int(row["line_end"])


def _claim_block(conn) -> str:
    row = conn.execute(
        """
        SELECT id FROM blocks
        WHERE selector_type = 'heading' AND anchor LIKE 'OBS-%'
        ORDER BY line_start, id LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise UsageProblem(
            "demo corpus is missing an atomic claim block; "
            "check the claim profile and reading-log.md"
        )
    return str(row["id"])


def _render(
    *,
    workspaces: dict[str, DemoWorkspace],
    manual: tuple[str, int, int],
    card_id: str,
    card_status: str,
    claim_id: str,
    claim_status: str,
    prose_hit,
    legacy_hit,
    fsck_findings: int,
    commits: dict[str, tuple[str, int]],
    logs: dict[str, int],
    active_claims: int,
) -> str:
    lines = [
        "Stratum demo",
        f"Workspaces: {len(workspaces)}",
    ]
    for name, workspace in workspaces.items():
        conn = open_db(workspace.db_path)
        try:
            lines.append(
                f"Status: {name} blocks={_count(conn, 'blocks')} "
                f"cards={_count(conn, 'cards')} reports={_count(conn, 'reports')}"
            )
        finally:
            conn.close()

    manual_id, manual_start, manual_end = manual
    lines.append(f"Manual block: {manual_id} lines {manual_start}-{manual_end}")
    lines.append(f"Card: {card_id} {card_status}")
    lines.append(f"Claim: {claim_id} {claim_status}")
    lines.append(
        f"Prose hit: {prose_hit.entity_id} {prose_hit.document_id} "
        f"line {prose_hit.line_start}"
    )
    lines.append(
        f"Legacy hit: {legacy_hit.entity_id} {legacy_hit.document_id} "
        f"line {legacy_hit.line_start}"
    )
    lines.append(f"Fsck: clean ({fsck_findings} findings)")
    for name in workspaces:
        commit_id, seq = commits[name]
        lines.append(f"Commit: {name} {commit_id} seq {seq}")
    for name in workspaces:
        lines.append(f"Log: {name} {logs[name]} commit(s)")
    lines.append(f"Query: {active_claims} active claim(s)")
    return "\n".join(lines) + "\n"


def run_demo(
    root: str | os.PathLike[str] | None = None,
    *,
    keep: bool = False,
) -> str:
    """Run the synthetic demo and return the plain-text output."""

    demo_root = _resolve_demo_root(root)
    sources = _workspace_sources(demo_root)
    profiles = _profile_files(demo_root)
    temp_root = Path(tempfile.mkdtemp(prefix="stratum-demo-"))
    home = temp_root / "home"
    previous_home = os.environ.get("STRATUM_HOME")
    os.environ["STRATUM_HOME"] = str(home)
    succeeded = False
    try:
        workspaces: dict[str, DemoWorkspace] = {}
        for source in sources:
            destination = temp_root / "workspaces" / source.name
            _copy_workspace(source, destination)
            result = init_workspace(destination, name=source.name, home=home)
            _install_profiles(destination, profiles)
            workspace = DemoWorkspace(
                name=source.name,
                workspace_id=result.workspace.workspace_id,
                root=destination,
                db_path=result.workspace.db_path,
            )
            workspaces[workspace.name] = workspace
            _ingest_tree(workspace)

        if "tidepool-lab" not in workspaces or "lantern-library" not in workspaces:
            raise UsageProblem(
                "demo requires tidepool-lab and lantern-library workspaces"
            )

        tidepool = workspaces["tidepool-lab"]
        lantern = workspaces["lantern-library"]

        tidepool_conn = open_db(tidepool.db_path)
        try:
            manual_id, manual_start, manual_end = _manual_block(tidepool_conn)
        finally:
            tidepool_conn.close()

        card_result = add_card(
            blocks=[manual_id],
            card_type="observation",
            body=(
                "The synthetic calibration window is selected by the demo "
                "manual profile."
            ),
            title="Synthetic calibration window",
            author=DEMO_AUTHOR,
            date=CARD_DATE,
            workspace=tidepool.workspace_id,
        )
        tidepool_conn = open_db(tidepool.db_path)
        try:
            card_status = promote(
                tidepool_conn,
                card_result.card_id,
                author=DEMO_AUTHOR,
                reason="synthetic demo promotion",
            ).to_status
        finally:
            tidepool_conn.close()

        lantern_conn = open_db(lantern.db_path)
        try:
            claim_block_id = _claim_block(lantern_conn)
        finally:
            lantern_conn.close()

        claim_result = add_claim(
            claim_id=CLAIM_ID,
            claim_type="observation",
            period_start="2026-02-02",
            period_end="2026-02-03",
            granularity="day",
            entities=["lantern-library"],
            evidence=[claim_block_id],
            confidence="medium",
            body=(
                "The synthetic reading log records a pace change after "
                "shorter sessions."
            ),
            author=DEMO_AUTHOR,
            workspace=lantern.workspace_id,
        )
        lantern_conn = open_db(lantern.db_path)
        try:
            claim_status = promote(
                lantern_conn,
                claim_result.claim_id,
                author=DEMO_AUTHOR,
                reason="synthetic demo promotion",
            ).to_status
            active_claims = len(
                query_items(lantern_conn, entity_type="claim", status="active")
            )
        finally:
            lantern_conn.close()

        if "prose-notes" not in workspaces:
            raise UsageProblem("demo requires a prose-notes workspace")
        prose_conn = open_db(workspaces["prose-notes"].db_path)
        try:
            prose_hit = query_items(
                prose_conn,
                entity_type="prose",
                grep="fictional walk",
                limit=1,
            )[0]
        finally:
            prose_conn.close()

        legacy_conn = open_db(lantern.db_path)
        try:
            legacy_rows = [
                row
                for row in query_items(
                    legacy_conn,
                    entity_type="block",
                    grep="protagonist",
                    limit=20,
                )
                if row.type_label == "legacy"
            ]
            if not legacy_rows:
                raise UsageProblem(
                    "demo corpus is missing a legacy narrative block; "
                    "check notes/the-dispossessed.md and the legacy profile"
                )
            legacy_hit = legacy_rows[0]
        finally:
            legacy_conn.close()

        fsck_findings = 0
        for workspace in workspaces.values():
            conn = open_db(workspace.db_path)
            try:
                result = run_fsck(conn)
                fsck_findings += len(result.findings)
            finally:
                conn.close()
        if fsck_findings:
            raise BusinessError(
                f"demo fsck reported {fsck_findings} finding(s); "
                "the synthetic corpus should be clean"
            )

        commits: dict[str, tuple[str, int]] = {}
        logs: dict[str, int] = {}
        for name, workspace in workspaces.items():
            conn = open_db(workspace.db_path)
            try:
                commit_result = history_commit(
                    conn,
                    author=DEMO_AUTHOR,
                    message="Synthetic demo ingest",
                )
                if commit_result.commit_id is None or commit_result.seq is None:
                    raise BusinessError(
                        f"demo commit for {name} produced no commit"
                    )
                commits[name] = (commit_result.commit_id, commit_result.seq)
                logs[name] = len(list_commits(conn, limit=5))
            finally:
                conn.close()

        output = _render(
            workspaces=workspaces,
            manual=(manual_id, manual_start, manual_end),
            card_id=card_result.card_id,
            card_status=card_status,
            claim_id=claim_result.claim_id,
            claim_status=claim_status,
            prose_hit=prose_hit,
            legacy_hit=legacy_hit,
            fsck_findings=fsck_findings,
            commits=commits,
            logs=logs,
            active_claims=active_claims,
        )
        succeeded = True
    finally:
        if previous_home is None:
            os.environ.pop("STRATUM_HOME", None)
        else:
            os.environ["STRATUM_HOME"] = previous_home
        if not (keep and succeeded):
            shutil.rmtree(temp_root, ignore_errors=True)

    if keep:
        output += f"Kept temporary demo: {temp_root}\n"
    return output
