"""build_plan: convergence, drift, determinism and read-only guarantees (n3rv#9)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import n3rv
from n3rv.init.binary import resolve_n3rv_binary
from n3rv.init.context import Stack
from n3rv.init.update import MARKER_START, run_update
from n3rv.reconcile import state_from_tree
from n3rv.reconcile.plan import PLAN_CODES, ActionKind, build_plan, env_hash, templates_sha
from n3rv.reconcile.state import Components, Detection, ProjectIdentity, ProjectState, StructureState

TEMPLATES = Path(n3rv.__file__).parent / "init" / "templates"


def _plan(root: Path, state=None):
    return build_plan(
        root,
        state or state_from_tree(root),
        templates_dir=TEMPLATES,
        n3rv_version="dev",
        n3rv_binary=resolve_n3rv_binary() or "n3rv",
    )


def _converged(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "demo"\nversion = "0.1.0"\n')
    run_update(tmp_path)
    return tmp_path


def _snapshot(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


class TestConvergence:
    def test_converged_tree_plans_nothing(self, tmp_path: Path, capsys) -> None:
        root = _converged(tmp_path)
        capsys.readouterr()

        plan = _plan(root)

        assert plan.empty, [f"{a.kind} {a.target} {a.reason}" for a in plan.actions]
        assert plan.exit_code == 0

    def test_plan_is_read_only(self, tmp_path: Path, capsys) -> None:
        root = _converged(tmp_path)
        capsys.readouterr()
        before = _snapshot(root)

        _plan(root)

        assert _snapshot(root) == before


class TestDrift:
    def test_edited_generated_file_becomes_an_update(self, tmp_path: Path, capsys) -> None:
        root = _converged(tmp_path)
        capsys.readouterr()
        target = root / ".opencode/agents/n3rv.md"
        target.write_text(target.read_text() + "\nhand edit\n")

        plan = _plan(root)

        kinds = {a.target: a.kind for a in plan.actions}
        assert kinds[".opencode/agents/n3rv.md"] in {ActionKind.UPDATE, ActionKind.REGENERATE}
        assert plan.exit_code & PLAN_CODES["drift"]

    def test_missing_file_becomes_a_create(self, tmp_path: Path) -> None:
        state = ProjectState(
            project=ProjectIdentity(name="demo", stack=Stack.GENERIC),
            detection=Detection(structure=StructureState()),
            components=Components(agents=["git-ops"]),
        )

        plan = _plan(tmp_path, state)

        targets = {a.target for a in plan.actions if a.kind == ActionKind.CREATE}
        assert ".opencode/agents/git-ops.md" in targets

    def test_unknown_component_is_reported_not_ignored(self, tmp_path: Path) -> None:
        state = ProjectState(
            project=ProjectIdentity(name="demo", stack=Stack.GENERIC),
            components=Components(skills=["does-not-exist"]),
        )

        plan = _plan(tmp_path, state)

        assert any(f.code == "unknown_component" for f in plan.findings)
        assert plan.exit_code & PLAN_CODES["usage"]

    def test_project_owned_modification_is_a_conflict_the_plan_refuses_to_guess(self, tmp_path: Path, capsys) -> None:
        root = _converged(tmp_path)
        capsys.readouterr()
        agents = root / "AGENTS.md"
        text = agents.read_text(encoding="utf-8")
        marker_line_end = text.find("\n", text.find(MARKER_START))
        agents.write_text(
            text[: marker_line_end + 1] + "hand edit inside the managed region\n" + text[marker_line_end + 1 :],
            encoding="utf-8",
        )

        plan = _plan(root)

        conflicts = [a for a in plan.actions if a.kind == ActionKind.CONFLICT and a.target == "AGENTS.md"]
        assert conflicts, "an edit inside the managed region must surface as a conflict"
        assert plan.exit_code & PLAN_CODES["conflict"]

    def test_force_project_files_turns_the_conflict_into_an_update(self, tmp_path: Path, capsys) -> None:
        root = _converged(tmp_path)
        capsys.readouterr()
        agents = root / "AGENTS.md"
        text = agents.read_text(encoding="utf-8")
        marker_line_end = text.find("\n", text.find(MARKER_START))
        agents.write_text(text[: marker_line_end + 1] + "hand edit\n" + text[marker_line_end + 1 :], encoding="utf-8")

        plan = build_plan(
            root,
            state_from_tree(root),
            templates_dir=TEMPLATES,
            n3rv_version="dev",
            n3rv_binary=resolve_n3rv_binary() or "n3rv",
            force_project_files=True,
        )

        kinds = {a.target: a.kind for a in plan.actions}
        assert kinds.get("AGENTS.md") in {ActionKind.UPDATE, ActionKind.REGENERATE}


class TestDeterminism:
    def test_same_inputs_produce_the_same_plan_hash(self, tmp_path: Path, capsys) -> None:
        root = _converged(tmp_path)
        capsys.readouterr()
        state = state_from_tree(root)

        assert _plan(root, state).plan_hash == _plan(root, state).plan_hash

    def test_plan_hash_ignores_the_environment_but_env_hash_does_not(self, tmp_path: Path) -> None:
        state = ProjectState(project=ProjectIdentity(name="demo", stack=Stack.GENERIC))
        other = tmp_path / "elsewhere"
        other.mkdir()

        one = build_plan(tmp_path, state, templates_dir=TEMPLATES, n3rv_version="dev", n3rv_binary="/a/n3rv")
        two = build_plan(other, state, templates_dir=TEMPLATES, n3rv_version="dev", n3rv_binary="/b/n3rv")

        assert one.plan_hash == two.plan_hash
        assert one.env_hash != two.env_hash

    def test_template_digest_changes_when_a_template_changes(self, tmp_path: Path) -> None:
        (tmp_path / "a.j2").write_text("one")
        first = templates_sha(tmp_path)

        (tmp_path / "a.j2").write_text("two")

        assert templates_sha(tmp_path) != first

    def test_env_hash_tracks_root_and_binary(self, tmp_path: Path) -> None:
        assert env_hash(tmp_path, "/a/n3rv") != env_hash(tmp_path, "/b/n3rv")


class TestExitBits:
    def test_bits_are_distinct_and_or_able(self) -> None:
        values = [PLAN_CODES["drift"], PLAN_CODES["conflict"], PLAN_CODES["divergence"], PLAN_CODES["inert"]]
        assert len({v & (v - 1) for v in values}) == 1  # each is a single bit
        assert len(values) == len(set(values))

    def test_usage_bit_is_independent(self) -> None:
        assert PLAN_CODES["usage"] & PLAN_CODES["drift"] == 0


class TestEnvNeutralization:
    HUB = ".n3rv/systemd/n3rv-hub.service"

    def test_env_only_diff_becomes_environment_finding_not_update(self, tmp_path: Path, capsys) -> None:
        root = _converged(tmp_path)
        capsys.readouterr()
        state = state_from_tree(root)

        plan = build_plan(
            root,
            state,
            templates_dir=TEMPLATES,
            n3rv_version="dev",
            n3rv_binary="/different/install/bin/n3rv",
        )

        hub_actions = [a for a in plan.actions if a.target == self.HUB]
        assert hub_actions == [], [f"{a.kind} {a.reason}" for a in hub_actions]
        env_findings = [f for f in plan.findings if f.code == "environment" and self.HUB in f.message]
        assert env_findings, [f.message for f in plan.findings]

    def test_converged_tree_plans_empty_from_any_install(self, tmp_path: Path, capsys) -> None:
        root = _converged(tmp_path)
        capsys.readouterr()
        state = state_from_tree(root)

        for binary in ("/a/n3rv", "/b/n3rv", "/totally/different/path/n3rv"):
            plan = build_plan(
                root, state, templates_dir=TEMPLATES, n3rv_version="dev", n3rv_binary=binary
            )
            assert plan.empty, [f"{a.kind} {a.target} {a.reason}" for a in plan.actions]
            assert plan.exit_code == 0

    def test_missing_env_derived_file_is_still_created(self, tmp_path: Path, capsys) -> None:
        root = _converged(tmp_path)
        capsys.readouterr()
        hub = root / self.HUB
        hub.unlink()
        state = state_from_tree(root)

        plan = build_plan(
            root, state, templates_dir=TEMPLATES, n3rv_version="dev", n3rv_binary="/other/n3rv"
        )

        hub_actions = [a for a in plan.actions if a.target == self.HUB]
        assert len(hub_actions) == 1
        assert hub_actions[0].kind == ActionKind.CREATE

    def test_real_non_env_change_still_produces_update(self, tmp_path: Path, capsys) -> None:
        root = _converged(tmp_path)
        capsys.readouterr()
        hub = root / self.HUB
        # change a non-env part of the on-disk file (keep env values as-is)
        hub.write_text(hub.read_text().replace("RestartSec=5", "RestartSec=99"))
        state = state_from_tree(root)

        plan = build_plan(
            root, state, templates_dir=TEMPLATES, n3rv_version="dev", n3rv_binary=resolve_n3rv_binary() or "n3rv"
        )

        hub_actions = [a for a in plan.actions if a.target == self.HUB]
        assert hub_actions and hub_actions[0].kind in {ActionKind.UPDATE, ActionKind.REGENERATE}
        env_findings = [f for f in plan.findings if f.code == "environment" and self.HUB in f.message]
        assert not env_findings
