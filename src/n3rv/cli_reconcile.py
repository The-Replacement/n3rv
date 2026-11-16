"""``n3rv plan`` and ``n3rv apply`` (n3rv#9)."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from n3rv.init.binary import resolve_n3rv_binary
from n3rv.reconcile import StateError, load_state
from n3rv.reconcile.apply import apply_plan, apply_plan_file
from n3rv.reconcile.plan import PLAN_CODES, build_plan
from n3rv.reconcile.planfile import PlanFileError, PlanRejectedError

reconcile_app = typer.Typer(help="Plan and apply the declared state")

_TEMPLATES = Path(__file__).parent / "init" / "templates"


def _load(root: Path) -> object:
    try:
        return load_state(root)
    except StateError as exc:
        typer.echo(f"✗ {exc}", err=True)
        raise typer.Exit(code=64) from exc


def _version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("n3rv")
    except PackageNotFoundError:
        return "dev"


def _print_plan(plan: object) -> None:
    if plan.empty:
        typer.echo("✓ Nothing to do: the tree matches the declared state.")
    else:
        typer.echo(f"{len(plan.actions)} action(s):")
        for action in plan.actions:
            typer.echo(f"  {action.kind.upper():10s} {action.target}  ({action.reason})")
    for finding in plan.findings:
        marker = {"warn": "⚠", "fail": "✗"}.get(finding.level, "·")
        typer.echo(f"  {marker} {finding.message}")
    typer.echo(f"plan {plan.plan_hash[:16]}  exit {plan.exit_code}")


@reconcile_app.command("plan")
def plan_cmd(
    root: Path = typer.Option(Path.cwd(), "--root", help="Project root directory"),
    json_output: bool = typer.Option(False, "--json", help="Emit the plan as JSON"),
    output: Path | None = typer.Option(None, "--output", help="Write the plan JSON to a file"),
    force_project_files: bool = typer.Option(
        False, "--force-project-files", help="Include project-owned files whose managed region changed"
    ),
    global_config: Path | None = typer.Option(
        None, "--global-config", help="Global opencode config used for the policy check"
    ),
) -> None:
    """Show what apply would do. Reads only; never writes."""
    if output is not None and json_output:
        typer.echo("✗ --output and --json are mutually exclusive", err=True)
        raise typer.Exit(code=PLAN_CODES["usage"])
    state = _load(root)
    plan = build_plan(
        root,
        state,
        templates_dir=_TEMPLATES,
        n3rv_version=_version(),
        n3rv_binary=resolve_n3rv_binary() or "n3rv",
        force_project_files=force_project_files,
        global_config=global_config,
    )
    if output is not None:
        output.write_text(json.dumps(plan.to_dict(), indent=2) + "\n", encoding="utf-8")
    elif json_output:
        typer.echo(json.dumps(plan.to_dict(), indent=2))
    else:
        _print_plan(plan)
    raise typer.Exit(code=plan.exit_code)


@reconcile_app.command("apply")
def apply_cmd(
    root: Path = typer.Option(Path.cwd(), "--root", help="Project root directory"),
    json_output: bool = typer.Option(False, "--json", help="Emit the result as JSON"),
    plan_file: Path | None = typer.Option(None, "--plan-file", help="Apply a saved plan file"),
    force_project_files: bool = typer.Option(
        False, "--force-project-files", help="Overwrite project-owned files whose managed region changed"
    ),
    global_config: Path | None = typer.Option(
        None, "--global-config", help="Global opencode config used for the policy check"
    ),
) -> None:
    """Converge the tree to the declared state.

    Conflicts and inert actions are skipped, never guessed at.
    """
    state = _load(root)
    if plan_file is not None:
        try:
            result = apply_plan_file(
                root,
                state,
                plan_file=plan_file,
                templates_dir=_TEMPLATES,
                n3rv_version=_version(),
                n3rv_binary=resolve_n3rv_binary() or "n3rv",
            )
        except PlanFileError as exc:
            typer.echo(f"✗ {exc}", err=True)
            raise typer.Exit(code=PLAN_CODES["usage"]) from exc
        except PlanRejectedError as exc:
            typer.echo(f"✗ plan rejected: {exc.field} {exc.detail}; nothing written", err=True)
            raise typer.Exit(code=PLAN_CODES["rejected"]) from exc
    else:
        result = apply_plan(
            root,
            state,
            templates_dir=_TEMPLATES,
            n3rv_version=_version(),
            n3rv_binary=resolve_n3rv_binary() or "n3rv",
            force_project_files=force_project_files,
            global_config=global_config,
        )

    if json_output:
        typer.echo(
            json.dumps(
                {
                    "plan_hash": result.plan_hash,
                    "ok": result.ok,
                    "applied": [a.__dict__ for a in result.applied],
                    "skipped": [a.__dict__ for a in result.skipped],
                    "error": result.error,
                    "lockfile_entries": result.lockfile_entries,
                },
                indent=2,
            )
        )
    else:
        for applied in result.applied:
            typer.echo(f"✓ {applied.result:8s} {applied.target}")
        for skipped in result.skipped:
            typer.echo(f"⊘ {skipped.result:8s} {skipped.target}")
        if not result.applied and not result.skipped:
            typer.echo("✓ Already converged.")
        if result.error:
            typer.echo(f"✗ {result.error}", err=True)
        typer.echo(f"plan {result.plan_hash[:16]}")

    raise typer.Exit(code=0 if result.ok else 1)
