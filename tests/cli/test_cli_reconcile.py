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


class TestPlanFile:
    def test_plan_output_equals_json(self, tmp_path: Path) -> None:
        root = _converged(tmp_path)
        out = root / "plan.json"
        r_json = runner.invoke(app, ["plan", "--root", str(root), "--json", "--global-config", str(_policy(tmp_path))])
        r_out = runner.invoke(app, ["plan", "--root", str(root), "--output", str(out), "--global-config", str(_policy(tmp_path))])
        assert r_out.exit_code == r_json.exit_code
        assert out.read_text() == r_json.stdout if r_json.stdout.endswith("\n") else r_json.stdout + "\n"

    def test_plan_output_and_json_mutually_exclusive(self, tmp_path: Path) -> None:
        root = _converged(tmp_path)
        out = root / "plan.json"
        result = runner.invoke(app, ["plan", "--root", str(root), "--json", "--output", str(out)])
        assert result.exit_code == PLAN_CODES["usage"]

    def test_apply_plan_file_converges(self, tmp_path: Path) -> None:
        root = _converged(tmp_path)
        target = root / ".opencode/agents/n3rv.md"
        target.write_text("wrecked\n")
        pf = root / "plan.json"
        runner.invoke(app, ["plan", "--root", str(root), "--output", str(pf), "--global-config", str(_policy(tmp_path))])
        result = runner.invoke(app, ["apply", "--root", str(root), "--plan-file", str(pf)])
        assert result.exit_code == 0
        assert target.read_text() != "wrecked\n"

    def _snapshot(self, root: Path) -> dict[str, bytes]:
        return {
            str(p.relative_to(root)): p.read_bytes()
            for p in sorted(root.rglob("*"))
            if p.is_file() and p.name != "state.yaml" and p.name != "plan.json"
        }

    def test_apply_plan_file_state_drift_rejected(self, tmp_path: Path) -> None:
        root = _converged(tmp_path)
        target = root / ".opencode/agents/n3rv.md"
        target.write_text("wrecked\n")
        pf = root / "plan.json"
        runner.invoke(app, ["plan", "--root", str(root), "--output", str(pf), "--global-config", str(_policy(tmp_path))])
        # mutate state in a way that changes canonical sha
        state_path = root / ".n3rv/state.yaml"
        state_path.write_text(state_path.read_text().replace("name: demo", "name: demo2", 1))
        before = self._snapshot(root)
        result = runner.invoke(app, ["apply", "--root", str(root), "--plan-file", str(pf)])
        assert result.exit_code == PLAN_CODES["rejected"]
        assert "state_sha" in (result.stdout + result.stderr)
        assert self._snapshot(root) == before

    def test_apply_plan_file_template_drift_rejected(self, tmp_path: Path, monkeypatch) -> None:
        import shutil
        import n3rv.cli_reconcile as cli_rec
        root = _converged(tmp_path)
        target = root / ".opencode/agents/n3rv.md"
        target.write_text("wrecked\n")
        # plan against a temp templates dir we control
        tdir = tmp_path / "tmpl"
        shutil.copytree(cli_rec._TEMPLATES, tdir, dirs_exist_ok=True)
        monkeypatch.setattr(cli_rec, "_TEMPLATES", tdir)
        pf = root / "plan.json"
        runner.invoke(app, ["plan", "--root", str(root), "--output", str(pf), "--global-config", str(_policy(tmp_path))])
        any_j2 = next(tdir.rglob("*.j2"))
        any_j2.write_text(any_j2.read_text() + "\nchanged\n")
        before = self._snapshot(root)
        result = runner.invoke(app, ["apply", "--root", str(root), "--plan-file", str(pf)])
        assert result.exit_code == PLAN_CODES["rejected"]
        assert "templates_sha" in (result.stdout + result.stderr)
        assert self._snapshot(root) == before

    def test_apply_plan_file_version_drift_rejected(self, tmp_path: Path, monkeypatch) -> None:
        import n3rv.cli_reconcile as cli_rec
        root = _converged(tmp_path)
        target = root / ".opencode/agents/n3rv.md"
        target.write_text("wrecked\n")
        pf = root / "plan.json"
        runner.invoke(app, ["plan", "--root", str(root), "--output", str(pf), "--global-config", str(_policy(tmp_path))])
        monkeypatch.setattr(cli_rec, "_version", lambda: "9.9.9")
        before = self._snapshot(root)
        result = runner.invoke(app, ["apply", "--root", str(root), "--plan-file", str(pf)])
        assert result.exit_code == PLAN_CODES["rejected"]
        assert "n3rv_version" in (result.stdout + result.stderr)
        assert self._snapshot(root) == before

    def test_apply_plan_file_target_drift_rejected(self, tmp_path: Path) -> None:
        root = _converged(tmp_path)
        target = root / ".opencode/agents/n3rv.md"
        target.write_text("wrecked\n")
        pf = root / "plan.json"
        runner.invoke(app, ["plan", "--root", str(root), "--output", str(pf), "--global-config", str(_policy(tmp_path))])
        target.write_text("wrecked-again\n")
        before = self._snapshot(root)
        result = runner.invoke(app, ["apply", "--root", str(root), "--plan-file", str(pf)])
        assert result.exit_code == PLAN_CODES["rejected"]
        assert ".opencode/agents/n3rv.md" in (result.stdout + result.stderr)
        assert self._snapshot(root) == before
