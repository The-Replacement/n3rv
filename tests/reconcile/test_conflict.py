"""Conflict semantics: when the two authorities disagree, nothing is written.

A conflict is not an error. It is the plan saying "I cannot decide this
without a human", and the contract is that apply performs zero writes until
someone does.
"""

from __future__ import annotations

import json
from pathlib import Path

from n3rv.init.binary import resolve_n3rv_binary
from n3rv.init.update import MARKER_START, run_update
from n3rv.reconcile import load_state, save_state, state_from_tree
from n3rv.reconcile.apply import apply_plan
from n3rv.reconcile.plan import PLAN_CODES, ActionKind, build_plan

TEMPLATES = Path(__import__("n3rv").__file__).parent / "init" / "templates"
BINARY = resolve_n3rv_binary() or "n3rv"


def _plan(root: Path, **kwargs):
    return build_plan(root, load_state(root), templates_dir=TEMPLATES, n3rv_version="dev", n3rv_binary=BINARY, **kwargs)


def _apply(root: Path):
    return apply_plan(root, load_state(root), templates_dir=TEMPLATES, n3rv_version="dev", n3rv_binary=BINARY)


def _edit_managed_region(root: Path) -> None:
    """Change AGENTS.md inside the generated block, so it differs from desired."""
    agents = root / "AGENTS.md"
    text = agents.read_text(encoding="utf-8")
    line_end = text.find("\n", text.find(MARKER_START))
    agents.write_text(text[: line_end + 1] + "edited by hand\n" + text[line_end + 1 :], encoding="utf-8")


def _null_agents_entry(root: Path) -> None:
    lock_path = root / ".n3rv/template-lock.json"
    lock = json.loads(lock_path.read_text())
    lock["entries"]["AGENTS.md"] = None
    lock_path.write_text(json.dumps(lock, indent=2))


def _converged(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "demo"\nversion = "0.1.0"\n')
    run_update(tmp_path)
    save_state(tmp_path, state_from_tree(tmp_path))
    return tmp_path


class TestDeletedButEdited:
    def test_a_file_removed_from_state_is_deleted(self, tmp_path: Path, capsys) -> None:
        root = _converged(tmp_path)
        capsys.readouterr()
        state = load_state(root)
        save_state(
            root,
            state.model_copy(update={"components": state.components.model_copy(update={"agents": []})}),
            force=True,
        )

        plan = _plan(root)

        deletes = [a for a in plan.actions if a.kind == ActionKind.DELETE]
        assert deletes
        assert not [a for a in plan.actions if a.kind == ActionKind.CONFLICT]

    def test_a_deleted_but_edited_file_is_a_conflict(self, tmp_path: Path, capsys) -> None:
        root = _converged(tmp_path)
        capsys.readouterr()
        edited = root / ".opencode/agents/git-ops.md"
        edited.write_text("my own version\n")
        state = load_state(root)
        save_state(
            root,
            state.model_copy(update={"components": state.components.model_copy(update={"agents": []})}),
            force=True,
        )

        plan = _plan(root)

        target = ".opencode/agents/git-ops.md"
        kinds = [(a.kind, a.target) for a in plan.actions]
        assert (ActionKind.CONFLICT, target) in kinds
        assert (ActionKind.DELETE, target) not in kinds
        assert any(kind == ActionKind.DELETE for kind, _ in kinds)

    def test_a_conflict_writes_nothing(self, tmp_path: Path, capsys) -> None:
        root = _converged(tmp_path)
        capsys.readouterr()
        edited = root / ".opencode/agents/git-ops.md"
        edited.write_text("my own version\n")
        state = load_state(root)
        save_state(
            root,
            state.model_copy(update={"components": state.components.model_copy(update={"agents": []})}),
            force=True,
        )

        result = _apply(root)

        assert edited.read_text() == "my own version\n"
        assert not [a for a in result.applied if a.target == ".opencode/agents/git-ops.md"]
        assert [a for a in result.skipped if a.target == ".opencode/agents/git-ops.md"]


class TestUnknownProvenance:
    def test_a_null_lockfile_entry_is_a_conflict_not_a_silent_overwrite(self, tmp_path: Path, capsys) -> None:
        """radio's real case: the lockfile holds null for AGENTS.md."""
        root = _converged(tmp_path)
        capsys.readouterr()
        _edit_managed_region(root)
        _null_agents_entry(root)

        plan = _plan(root)

        conflicts = [a for a in plan.actions if a.kind == ActionKind.CONFLICT and a.target == "AGENTS.md"]
        assert conflicts
        assert "no provenance" in conflicts[0].reason
        assert plan.exit_code & PLAN_CODES["conflict"]

    def test_unknown_provenance_is_resolved_only_by_an_explicit_flag(self, tmp_path: Path, capsys) -> None:
        root = _converged(tmp_path)
        capsys.readouterr()
        _edit_managed_region(root)
        _null_agents_entry(root)

        forced = _plan(root, force_project_files=True)

        kinds = {a.target: a.kind for a in forced.actions}
        assert kinds.get("AGENTS.md") in {ActionKind.UPDATE, ActionKind.REGENERATE}


class TestExitContract:
    def test_conflict_bit_is_independent_of_drift(self) -> None:
        assert PLAN_CODES["conflict"] & PLAN_CODES["drift"] == 0

    def test_plan_with_only_conflicts_still_exits_non_zero(self, tmp_path: Path, capsys) -> None:
        root = _converged(tmp_path)
        capsys.readouterr()
        _edit_managed_region(root)
        _null_agents_entry(root)

        plan = _plan(root)

        assert plan.exit_code != 0
        assert plan.exit_code & PLAN_CODES["conflict"]
