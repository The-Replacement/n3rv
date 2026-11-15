"""``n3rv state`` - declare and inspect the desired state (n3rv#9)."""

from __future__ import annotations

from pathlib import Path

import typer

from n3rv.reconcile import StateError, load_state, save_state, state_from_tree, state_path, state_summary

state_app = typer.Typer(help="Declare and inspect the desired state")


@state_app.command("init")
def state_init_cmd(
    root: Path = typer.Option(Path.cwd(), "--root", help="Project root directory"),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing state file"),
) -> None:
    """Freeze what this tree already expresses into .n3rv/state.yaml.

    Components are the generated files that are actually present; MCP servers
    come from the project's opencode.json, falling back to detection when no
    config exists yet. Detection never silently becomes the declared state.
    """
    try:
        state = state_from_tree(root)
        path = save_state(root, state, force=force)
    except StateError as exc:
        typer.echo(f"✗ {exc}", err=True)
        raise typer.Exit(code=1) from exc

    summary = state_summary(state)
    components = summary["components"]
    typer.echo(f"✓ Wrote {path.relative_to(root)}")
    typer.echo(
        "  "
        f"{summary['project']['name']} ({summary['project']['stack']}) · "
        f"{components['agents']} agents · {components['skills']} skills · "
        f"{components['commands']} commands · {components['hooks']} hooks · "
        f"{len(summary['mcp_servers'])} MCP servers"
    )
    typer.echo(f"  sha256 {summary['sha256'][:16]}")


@state_app.command("show")
def state_show_cmd(
    root: Path = typer.Option(Path.cwd(), "--root", help="Project root directory"),
    json_output: bool = typer.Option(False, "--json", help="Emit a machine-readable summary"),
) -> None:
    """Print the declared state and its hash."""
    import json as json_lib

    try:
        state = load_state(root)
    except StateError as exc:
        typer.echo(f"✗ {exc}", err=True)
        raise typer.Exit(code=1) from exc

    if json_output:
        typer.echo(json_lib.dumps(state_summary(state), indent=2))
        return

    path = state_path(root)
    typer.echo(f"{path}")
    typer.echo(state.to_yaml().rstrip())
    typer.echo(f"sha256 {state.sha256()}")
