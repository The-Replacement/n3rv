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
    assert "schema: 1" in result.stdout
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
