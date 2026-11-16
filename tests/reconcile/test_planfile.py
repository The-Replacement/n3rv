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


class TestApplyPlanFile:
    def test_converges_identically_to_plain_apply(self, tmp_path: Path, capsys) -> None:
        from n3rv.reconcile import load_state, save_state
        from n3rv.reconcile.apply import apply_plan, apply_plan_file

        root = _converged(tmp_path)
        capsys.readouterr()
        save_state(root, state_from_tree(root))
        target = root / ".opencode/agents/n3rv.md"
        target.write_text("wrecked\n")
        state = load_state(root)
        plan = _plan(root, state)
        pf = root / "plan.json"
        pf.write_text(json.dumps(plan.to_dict(), indent=2))

        result = apply_plan_file(
            root, state, plan_file=pf, templates_dir=TEMPLATES, n3rv_version="dev", n3rv_binary=BINARY
        )
        assert result.ok
        assert plan.plan_hash == result.plan_hash
        assert target.read_text() != "wrecked\n"

        # compare with plain apply on fresh drifted tree
        root2 = tmp_path / "r2"
        root2.mkdir()
        root2 = _converged(root2)
        capsys.readouterr()
        save_state(root2, state_from_tree(root2))
        (root2 / ".opencode/agents/n3rv.md").write_text("wrecked\n")
        state2 = load_state(root2)
        plain = apply_plan(root2, state2, templates_dir=TEMPLATES, n3rv_version="dev", n3rv_binary=BINARY)
        assert plain.ok
        assert {a.target for a in plain.applied} == {a.target for a in result.applied}

    def test_refusal_writes_nothing(self, tmp_path: Path, capsys) -> None:
        import hashlib
        from n3rv.reconcile import load_state, save_state
        from n3rv.reconcile.apply import apply_plan_file

        def snapshot(r: Path) -> dict[str, str]:
            return {
                str(p.relative_to(r)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(r.rglob("*"))
                if p.is_file() and p.name != "state.yaml"
            }

        root = _converged(tmp_path)
        capsys.readouterr()
        save_state(root, state_from_tree(root))
        target = root / ".opencode/agents/n3rv.md"
        target.write_text("wrecked\n")
        state = load_state(root)
        plan = _plan(root, state)
        pf = root / "plan.json"
        pf.write_text(json.dumps(plan.to_dict(), indent=2))
        # mutate target so content drift (target mismatch)
        target.write_text("wrecked-again\n")
        before = snapshot(root)
        lock_before = (root / ".n3rv/template-lock.json").read_bytes() if (root / ".n3rv/template-lock.json").exists() else None
        with pytest.raises(PlanRejectedError):
            apply_plan_file(root, state, plan_file=pf, templates_dir=TEMPLATES, n3rv_version="dev", n3rv_binary=BINARY)
        assert snapshot(root) == before
        lock_after = (root / ".n3rv/template-lock.json").read_bytes() if (root / ".n3rv/template-lock.json").exists() else None
        assert lock_after == lock_before

    def test_no_apply_resolved_plan_on_refusal(self, tmp_path: Path, capsys, monkeypatch) -> None:
        from n3rv.reconcile import load_state, save_state
        import n3rv.reconcile.apply as apply_mod

        root = _converged(tmp_path)
        capsys.readouterr()
        save_state(root, state_from_tree(root))
        target = root / ".opencode/agents/n3rv.md"
        target.write_text("wrecked\n")
        state = load_state(root)
        plan = _plan(root, state)
        pf = root / "plan.json"
        pf.write_text(json.dumps(plan.to_dict(), indent=2))
        target.write_text("wrecked-again\n")

        called = {"n": 0}
        orig = apply_mod.apply_resolved_plan

        def spy(*a, **k):
            called["n"] += 1
            return orig(*a, **k)

        monkeypatch.setattr(apply_mod, "apply_resolved_plan", spy)
        with pytest.raises(PlanRejectedError):
            apply_mod.apply_plan_file(root, state, plan_file=pf, templates_dir=TEMPLATES, n3rv_version="dev", n3rv_binary=BINARY)
        assert called["n"] == 0

    def test_plan_hash_backstop_on_flag_drift(self, tmp_path: Path, capsys) -> None:
        from n3rv.init.update import MARKER_START
        from n3rv.reconcile import load_state, save_state
        from n3rv.reconcile.apply import apply_plan_file

        root = _converged(tmp_path)
        capsys.readouterr()
        save_state(root, state_from_tree(root))
        agents = root / "AGENTS.md"
        text = agents.read_text(encoding="utf-8")
        line_end = text.find("\n", text.find(MARKER_START))
        agents.write_text(text[: line_end + 1] + "hand edit\n" + text[line_end + 1 :], encoding="utf-8")
        state = load_state(root)

        # plan without force -> conflict
        plan_no = build_plan(root, state, templates_dir=TEMPLATES, n3rv_version="dev", n3rv_binary=BINARY)
        pf_no = root / "plan_no.json"
        pf_no.write_text(json.dumps(plan_no.to_dict(), indent=2))
        # plan with force -> update
        plan_yes = build_plan(root, state, templates_dir=TEMPLATES, n3rv_version="dev", n3rv_binary=BINARY, force_project_files=True)
        pf_yes = root / "plan_yes.json"
        pf_yes.write_text(json.dumps(plan_yes.to_dict(), indent=2))
        assert plan_no.plan_hash != plan_yes.plan_hash

        # apply forced plan converges (recorded flag wins)
        result = apply_plan_file(root, state, plan_file=pf_yes, templates_dir=TEMPLATES, n3rv_version="dev", n3rv_binary=BINARY)
        assert result.ok

        # plan_hash backstop: forge recorded hash while preconditions still match
        state3 = load_state(root)
        plan3 = _plan(root, state3)
        pf3 = root / "plan3.json"
        pf3.write_text(json.dumps(plan3.to_dict(), indent=2))
        d = json.loads(pf3.read_text())
        d["plan_hash"] = "0" * 64
        forged = root / "plan_forged.json"
        forged.write_text(json.dumps(d, indent=2))
        with pytest.raises(PlanRejectedError) as exc:
            apply_plan_file(root, state3, plan_file=forged, templates_dir=TEMPLATES, n3rv_version="dev", n3rv_binary=BINARY)
        assert exc.value.field == "plan_hash"

    def test_empty_plan_converges(self, tmp_path: Path, capsys) -> None:
        from n3rv.reconcile import load_state, save_state
        from n3rv.reconcile.apply import apply_plan_file

        root = _converged(tmp_path)
        capsys.readouterr()
        save_state(root, state_from_tree(root))
        state = load_state(root)
        plan = _plan(root, state)
        assert plan.empty
        pf = root / "plan.json"
        pf.write_text(json.dumps(plan.to_dict(), indent=2))
        result = apply_plan_file(root, state, plan_file=pf, templates_dir=TEMPLATES, n3rv_version="dev", n3rv_binary=BINARY)
        assert result.ok
        assert result.applied == []
