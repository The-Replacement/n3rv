"""Plan file loading and validation (n3rv#19)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import n3rv
from n3rv.init.binary import resolve_n3rv_binary
from n3rv.init.context import Stack
from n3rv.init.update import run_update
from n3rv.reconcile import state_from_tree
from n3rv.reconcile.plan import build_plan, templates_sha
from n3rv.reconcile.planfile import PlanFileError, PlanRejectedError, load_plan_file, validate_plan_file
from n3rv.reconcile.state import ProjectIdentity, ProjectState

TEMPLATES = Path(n3rv.__file__).parent / "init" / "templates"
BINARY = resolve_n3rv_binary() or "n3rv"


def _plan(root: Path, state=None, **kw):
    return build_plan(
        root,
        state or state_from_tree(root),
        templates_dir=TEMPLATES,
        n3rv_version="dev",
        n3rv_binary=BINARY,
        **kw,
    )


def _converged(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "demo"\nversion = "0.1.0"\n')
    run_update(tmp_path)
    return tmp_path


class TestLoadPlanFile:
    def test_load_valid_plan(self, tmp_path: Path, capsys) -> None:
        root = _converged(tmp_path)
        capsys.readouterr()
        plan = _plan(root)
        pf = tmp_path / "plan.json"
        pf.write_text(json.dumps(plan.to_dict(), indent=2))
        loaded, recorded = load_plan_file(pf)
        assert loaded.to_dict() == plan.to_dict()
        assert recorded == plan.plan_hash

    def test_load_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(PlanFileError):
            load_plan_file(tmp_path / "nope.json")

    def test_load_malformed_json(self, tmp_path: Path) -> None:
        pf = tmp_path / "plan.json"
        pf.write_text("{not json")
        with pytest.raises(PlanFileError):
            load_plan_file(pf)

    def test_load_non_object(self, tmp_path: Path) -> None:
        pf = tmp_path / "plan.json"
        pf.write_text("[]")
        with pytest.raises(PlanFileError):
            load_plan_file(pf)


class _StateWithSha:
    def __init__(self, sha: str) -> None:
        self._sha = sha

    def sha256(self) -> str:
        return self._sha


class TestValidatePlanFile:
    def test_matching_preconditions(self, tmp_path: Path, capsys) -> None:
        root = _converged(tmp_path)
        capsys.readouterr()
        state = state_from_tree(root)
        plan = _plan(root, state)
        validate_plan_file(plan, root=root, state=state, templates_dir=TEMPLATES, n3rv_version="dev")

    def test_state_sha_mismatch(self, tmp_path: Path, capsys) -> None:
        root = _converged(tmp_path)
        capsys.readouterr()
        state = state_from_tree(root)
        plan = _plan(root, state)
        bad_state = _StateWithSha("0" * 64)
        with pytest.raises(PlanRejectedError) as exc:
            validate_plan_file(plan, root=root, state=bad_state, templates_dir=TEMPLATES, n3rv_version="dev")
        assert exc.value.field == "state_sha"

    def test_templates_sha_mismatch(self, tmp_path: Path, capsys) -> None:
        root = _converged(tmp_path)
        capsys.readouterr()
        state = state_from_tree(root)
        plan = _plan(root, state)
        tdir = tmp_path / "tmpl"
        tdir.mkdir()
        # copy templates and modify one
        import shutil
        shutil.copytree(TEMPLATES, tdir, dirs_exist_ok=True)
        any_j2 = next(tdir.rglob("*.j2"))
        any_j2.write_text(any_j2.read_text() + "\nchanged\n")
        with pytest.raises(PlanRejectedError) as exc:
            validate_plan_file(plan, root=root, state=state, templates_dir=tdir, n3rv_version="dev")
        assert exc.value.field == "templates_sha"

    def test_version_mismatch(self, tmp_path: Path, capsys) -> None:
        root = _converged(tmp_path)
        capsys.readouterr()
        state = state_from_tree(root)
        plan = _plan(root, state)
        with pytest.raises(PlanRejectedError) as exc:
            validate_plan_file(plan, root=root, state=state, templates_dir=TEMPLATES, n3rv_version="other")
        assert exc.value.field == "n3rv_version"

    def test_target_content_drift(self, tmp_path: Path, capsys) -> None:
        root = _converged(tmp_path)
        capsys.readouterr()
        target = root / ".opencode/agents/n3rv.md"
        target.write_text("wrecked\n")
        state = state_from_tree(root)
        plan = _plan(root, state)
        assert plan.actions  # has update/regenerate
        # restore to converged-ish state? easier: plan from drifted, then mutate target differently
        target.write_text("wrecked-again\n")
        with pytest.raises(PlanRejectedError) as exc:
            validate_plan_file(plan, root=root, state=state, templates_dir=TEMPLATES, n3rv_version="dev")
        assert exc.value.field == ".opencode/agents/n3rv.md"

    def test_first_divergence_state_before_templates(self, tmp_path: Path, capsys) -> None:
        root = _converged(tmp_path)
        capsys.readouterr()
        state = state_from_tree(root)
        plan = _plan(root, state)
        bad_state = _StateWithSha("0" * 64)
        tdir = tmp_path / "tmpl"
        import shutil
        shutil.copytree(TEMPLATES, tdir, dirs_exist_ok=True)
        any_j2 = next(tdir.rglob("*.j2"))
        any_j2.write_text(any_j2.read_text() + "\nchanged\n")
        with pytest.raises(PlanRejectedError) as exc:
            validate_plan_file(plan, root=root, state=bad_state, templates_dir=tdir, n3rv_version="dev")
        assert exc.value.field == "state_sha"

    def test_first_divergence_templates_before_version(self, tmp_path: Path, capsys) -> None:
        root = _converged(tmp_path)
        capsys.readouterr()
        state = state_from_tree(root)
        plan = _plan(root, state)
        tdir = tmp_path / "tmpl"
        import shutil
        shutil.copytree(TEMPLATES, tdir, dirs_exist_ok=True)
        any_j2 = next(tdir.rglob("*.j2"))
        any_j2.write_text(any_j2.read_text() + "\nchanged\n")
        with pytest.raises(PlanRejectedError) as exc:
            validate_plan_file(plan, root=root, state=state, templates_dir=tdir, n3rv_version="other")
        assert exc.value.field == "templates_sha"
