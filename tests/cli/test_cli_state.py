"""CLI surface for ``n3rv state``: init and show."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from n3rv.cli import app
from n3rv.reconcile import load_state, state_path

runner = CliRunner()


def _seed_tree(root: Path) -> None:
    (root / ".opencode/skills/code").mkdir(parents=True)
    (root / ".opencode/skills/code/SKILL.md").write_text("x")
    (root / "opencode.json").write_text(json.dumps({"mcp": {"servers": {"sequential-thinking": {}}}}))


def test_state_init_writes_the_file_and_reports(tmp_path: Path) -> None:
    _seed_tree(tmp_path)

    result = runner.invoke(app, ["state", "init", "--root", str(tmp_path)])

    assert result.exit_code == 0
    assert "Wrote .n3rv/state.yaml" in result.stdout
    assert "1 skills" in result.stdout
    assert state_path(tmp_path).is_file()


def test_state_init_is_idempotent_with_force(tmp_path: Path) -> None:
    _seed_tree(tmp_path)
    runner.invoke(app, ["state", "init", "--root", str(tmp_path)])
    first = state_path(tmp_path).read_bytes()

    result = runner.invoke(app, ["state", "init", "--root", str(tmp_path), "--force"])

    assert result.exit_code == 0
    assert state_path(tmp_path).read_bytes() == first


def test_state_init_refuses_to_clobber(tmp_path: Path) -> None:
    _seed_tree(tmp_path)
    runner.invoke(app, ["state", "init", "--root", str(tmp_path)])
    before = state_path(tmp_path).read_bytes()

    result = runner.invoke(app, ["state", "init", "--root", str(tmp_path)])

    assert result.exit_code == 1
    assert "--force" in result.stdout + result.stderr
    assert state_path(tmp_path).read_bytes() == before


def test_state_show_prints_yaml_and_hash(tmp_path: Path) -> None:
    _seed_tree(tmp_path)
    runner.invoke(app, ["state", "init", "--root", str(tmp_path)])

    result = runner.invoke(app, ["state", "show", "--root", str(tmp_path)])

    assert result.exit_code == 0
    assert "schema: 2" in result.stdout
    assert "sha256 " in result.stdout


def test_state_show_json_is_parseable_and_matches_the_file(tmp_path: Path) -> None:
    _seed_tree(tmp_path)
    runner.invoke(app, ["state", "init", "--root", str(tmp_path)])

    result = runner.invoke(app, ["state", "show", "--root", str(tmp_path), "--json"])

    payload = json.loads(result.stdout)
    assert payload["project"]["name"] == tmp_path.name
    assert payload["sha256"] == load_state(tmp_path).sha256()


def test_state_show_without_a_state_fails_cleanly(tmp_path: Path) -> None:
    result = runner.invoke(app, ["state", "show", "--root", str(tmp_path)])

    assert result.exit_code == 1
    assert "state init" in result.stdout + result.stderr


def test_state_init_accepts_a_relative_dot_root(tmp_path: Path, monkeypatch) -> None:
    """The way a human actually types it: stand in the repo, use ``--root .``."""
    repo = tmp_path / "infra"
    repo.mkdir()
    monkeypatch.chdir(repo)

    result = runner.invoke(app, ["state", "init", "--root", "."])

    assert result.exit_code == 0, result.stdout + result.stderr
    assert load_state(repo).project.name == "infra"

def test_state_migrate_rewrites_schema_v2(tmp_path: Path) -> None:
    path = state_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "schema: 1\nproject:\n  name: radio\n  stack: generic\n",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["state", "migrate", "--root", str(tmp_path)])

    assert result.exit_code == 0
    assert "Migrated" in result.stdout
    assert "schema 2" in result.stdout
    assert "schema: 2" in path.read_text()
    assert load_state(tmp_path).schema_version == 2
