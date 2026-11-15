"""CLI surface for ``n3rv plan`` / ``n3rv apply`` (n3rv#9)."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from n3rv.cli import app
from n3rv.init.update import run_update
from n3rv.reconcile import save_state, state_from_tree
from n3rv.reconcile.plan import PLAN_CODES

runner = CliRunner()


def _converged(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "demo"\nversion = "0.1.0"\n')
    run_update(tmp_path)
    save_state(tmp_path, state_from_tree(tmp_path))
    return tmp_path


def _policy(tmp_path: Path) -> Path:
    config = tmp_path / "global.json"
    config.write_text(json.dumps({"policies": [{"action": "mcp.use", "resource": "*", "effect": "deny"}]}))
    return config


class TestPlan:
    def test_converged_project_exits_zero(self, tmp_path: Path) -> None:
        root = _converged(tmp_path)

        result = runner.invoke(app, ["plan", "--root", str(root), "--global-config", str(_policy(tmp_path))])

        assert result.exit_code == 0
        assert "Nothing to do" in result.stdout

    def test_missing_state_exits_usage(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["plan", "--root", str(tmp_path)])

        assert result.exit_code == PLAN_CODES["usage"]
        assert "state init" in result.stdout + result.stderr

    def test_json_is_parseable_and_reports_the_hash(self, tmp_path: Path) -> None:
        root = _converged(tmp_path)

        result = runner.invoke(app, ["plan", "--root", str(root), "--json", "--global-config", str(_policy(tmp_path))])

        payload = json.loads(result.stdout)
        assert payload["empty"] is True
        assert len(payload["plan_hash"]) == 64
        assert payload["exit_code"] == 0

    def test_drift_sets_exactly_the_drift_bit(self, tmp_path: Path) -> None:
        root = _converged(tmp_path)
        (root / ".opencode/agents/n3rv.md").write_text("wrecked\n")

        result = runner.invoke(app, ["plan", "--root", str(root), "--json", "--global-config", str(_policy(tmp_path))])

        payload = json.loads(result.stdout)
        assert payload["exit_code"] & PLAN_CODES["drift"]
        assert not payload["empty"]

    def test_plan_does_not_write(self, tmp_path: Path) -> None:
        root = _converged(tmp_path)
        (root / ".opencode/agents/n3rv.md").write_text("wrecked\n")

        runner.invoke(app, ["plan", "--root", str(root), "--global-config", str(_policy(tmp_path))])

        assert (root / ".opencode/agents/n3rv.md").read_text() == "wrecked\n"


class TestApply:
    def test_apply_converges_and_exits_zero(self, tmp_path: Path) -> None:
        root = _converged(tmp_path)
        target = root / ".opencode/agents/n3rv.md"
        target.write_text("wrecked\n")

        result = runner.invoke(app, ["apply", "--root", str(root), "--global-config", str(_policy(tmp_path))])

        assert result.exit_code == 0
        assert target.read_text() != "wrecked\n"
        follow_up = runner.invoke(app, ["plan", "--root", str(root), "--global-config", str(_policy(tmp_path))])
        assert follow_up.exit_code == 0

    def test_apply_json_reports_applied_targets(self, tmp_path: Path) -> None:
        root = _converged(tmp_path)
        (root / ".opencode/agents/n3rv.md").write_text("wrecked\n")

        result = runner.invoke(app, ["apply", "--root", str(root), "--json", "--global-config", str(_policy(tmp_path))])

        payload = json.loads(result.stdout)
        assert payload["ok"] is True
        assert any(item["target"] == ".opencode/agents/n3rv.md" for item in payload["applied"])

    def test_apply_with_missing_state_exits_usage(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["apply", "--root", str(tmp_path)])

        assert result.exit_code == PLAN_CODES["usage"]


class TestSurface:
    def test_converge_command_is_gone(self) -> None:
        """The stub printed a promise it never kept."""
        result = runner.invoke(app, ["converge", "some-change"])

        assert result.exit_code != 0
        assert "No such command" in result.stdout or result.exit_code == 2

    def test_help_lists_plan_and_apply(self) -> None:
        result = runner.invoke(app, ["--help"])

        assert "plan" in result.stdout
        assert "apply" in result.stdout
