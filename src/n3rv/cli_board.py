"""CLI for project board operations."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from n3rv.board import BoardError, detect_repo, sync_issue

board_app = typer.Typer(help="Project board operations")


@board_app.command("sync")
def board_sync(
    phase: str = typer.Option(
        ...,
        "--phase",
        help="SDD phase that just completed: todo, explore, propose, spec, design, tasks, apply, verify, archive",
    ),
    repo: str | None = typer.Option(
        None,
        "--repo",
        help="Issue repository as owner/repo (defaults to the origin remote of the current directory)",
    ),
    issue: int | None = typer.Option(None, "--issue", help="Issue number"),
    issue_id: str | None = typer.Option(
        None,
        "--issue-id",
        help="Issue GraphQL node id (alternative to --issue)",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Resolve and report without writing"),
) -> None:
    """Update an issue's Status and SDD Phase together.

    Emits a JSON result on stdout. Exit codes: 0 success, 1 tool/GraphQL
    failure, 2 ambiguous item, 3 item absent from the project, 4 invalid input
    or project not found. An absent item is an error, never a silent skip.
    """
    try:
        resolved_repo = repo or (None if issue_id else detect_repo(Path.cwd()))
        if not issue_id and not resolved_repo:
            raise BoardError(
                "cannot infer owner/repo from the origin remote; pass --repo owner/repo",
                code=4,
            )
        result = sync_issue(
            phase=phase,
            repo=resolved_repo,
            issue=issue,
            issue_node_id=issue_id,
            dry_run=dry_run,
        )
    except BoardError as exc:
        typer.echo(json.dumps({"ok": False, "error": str(exc), "code": exc.code}, indent=2))
        raise typer.Exit(code=exc.code) from exc

    typer.echo(json.dumps(result, indent=2))
