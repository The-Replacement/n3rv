"""Divergence advisor and policy inertness (n3rv#9)."""

from __future__ import annotations

import json
from pathlib import Path

from n3rv.init.binary import resolve_n3rv_binary
from n3rv.init.update import run_update
from n3rv.reconcile import load_state, save_state, state_from_tree
from n3rv.reconcile.apply import apply_plan
from n3rv.reconcile.plan import PLAN_CODES, ActionKind, build_plan
from n3rv.reconcile.policy import allowlisted, inert_targets

TEMPLATES = Path(__import__("n3rv").__file__).parent / "init" / "templates"
BINARY = resolve_n3rv_binary() or "n3rv"


def _plan(root: Path, config: Path | None = None):
    return build_plan(
        root,
        load_state(root),
        templates_dir=TEMPLATES,
        n3rv_version="dev",
        n3rv_binary=BINARY,
        global_config=config,
    )


def _policy(tmp_path: Path, *, servers: list[str], skills: list[str] | None = None) -> Path:
    config = tmp_path / "global.json"
    statements = [
        {"action": "mcp.use", "resource": "*", "effect": "deny"},
        {"action": "skill.use", "resource": "*", "effect": "deny"},
    ]
    statements += [{"action": "mcp.use", "resource": name, "effect": "allow"} for name in servers]
    statements += [{"action": "skill.use", "resource": name, "effect": "allow"} for name in (skills or [])]
    config.write_text(json.dumps({"policies": statements}))
    return config


def _converged(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text('[project]\nname = "demo"\nversion = "0.1.0"\n')
    run_update(project)
    save_state(project, state_from_tree(project))
    return project


class TestDivergence:
    def test_a_new_web_tree_is_reported_and_changes_nothing(self, tmp_path: Path, capsys) -> None:
        root = _converged(tmp_path)
        capsys.readouterr()
        no_policy = tmp_path / "missing.json"
        before = _plan(root, no_policy)

        (root / "web").mkdir()
        (root / "web/index.html").write_text("<html></html>")
        after = _plan(root, no_policy)

        assert {a.target for a in after.actions} == {a.target for a in before.actions}
        assert any("chrome-devtools" in f.message for f in after.findings if f.code == "divergence")
        assert after.exit_code & PLAN_CODES["divergence"]

    def test_divergence_never_produces_an_action(self, tmp_path: Path, capsys) -> None:
        root = _converged(tmp_path)
        capsys.readouterr()
        (root / "web").mkdir()
        (root / "web/index.html").write_text("<html></html>")

        plan = _plan(root, tmp_path / "missing.json")

        assert not [a for a in plan.actions if "divergence" in a.reason]

    def test_no_divergence_when_the_tree_matches_the_state(self, tmp_path: Path, capsys) -> None:
        root = _converged(tmp_path)
        capsys.readouterr()

        plan = _plan(root, tmp_path / "missing.json")

        assert not [f for f in plan.findings if f.code == "divergence"]


class TestPolicy:
    def test_unlisted_server_makes_the_config_inert(self, tmp_path: Path, capsys) -> None:
        root = _converged(tmp_path)
        capsys.readouterr()
        state = load_state(root)
        assert state.mcp.servers, "the fixture must declare a server"
        config = _policy(tmp_path, servers=[])  # nothing allowed
        (root / "opencode.json").write_text("{}\n")  # force an action

        plan = _plan(root, config)

        action = next((a for a in plan.actions if a.target == "opencode.json"), None)
        assert action is not None and action.kind == ActionKind.INERT
        assert plan.exit_code & PLAN_CODES["inert"]

    def test_listed_server_is_not_inert(self, tmp_path: Path, capsys) -> None:
        root = _converged(tmp_path)
        capsys.readouterr()
        names = [s.name for s in load_state(root).mcp.servers]
        config = _policy(tmp_path, servers=names)
        (root / "opencode.json").write_text("{}\n")

        plan = _plan(root, config)

        action = next((a for a in plan.actions if a.target == "opencode.json"), None)
        assert action is not None and action.kind != ActionKind.INERT

    def test_missing_policy_is_reported_not_assumed(self, tmp_path: Path, capsys) -> None:
        root = _converged(tmp_path)
        capsys.readouterr()

        plan = _plan(root, tmp_path / "nope.json")

        assert any(f.code == "policy_unknown" for f in plan.findings)
        assert not [a for a in plan.actions if a.kind == ActionKind.INERT]

    def test_inert_action_is_never_written(self, tmp_path: Path, capsys) -> None:
        root = _converged(tmp_path)
        capsys.readouterr()
        config = _policy(tmp_path, servers=[])
        (root / "opencode.json").write_text("{}\n")

        result = apply_plan(
            root,
            load_state(root),
            templates_dir=TEMPLATES,
            n3rv_version="dev",
            n3rv_binary=BINARY,
            global_config=config,
        )

        assert (root / "opencode.json").read_text() == "{}\n"
        assert not [a for a in result.applied if a.target == "opencode.json"]

    def test_the_policy_file_is_never_modified(self, tmp_path: Path, capsys) -> None:
        root = _converged(tmp_path)
        capsys.readouterr()
        config = _policy(tmp_path, servers=[])
        before = config.read_bytes()

        apply_plan(
            root,
            load_state(root),
            templates_dir=TEMPLATES,
            n3rv_version="dev",
            n3rv_binary=BINARY,
            global_config=config,
        )

        assert config.read_bytes() == before

    def test_allowlisted_returns_none_without_policies(self, tmp_path: Path) -> None:
        config = tmp_path / "plain.json"
        config.write_text(json.dumps({"permissions": []}))

        allow, reason = allowlisted(config)

        assert allow is None
        assert "no policies" in (reason or "")

    def test_inert_targets_names_what_would_be_dropped(self) -> None:
        from n3rv.reconcile.policy import Allowlist

        blocked = inert_targets(
            Allowlist(servers=frozenset({"github"}), skills=frozenset({"code"})),
            servers=["github", "transmission"],
            skills=["code", "testing"],
        )

        assert set(blocked) == {"mcp:transmission", "skill:testing"}
