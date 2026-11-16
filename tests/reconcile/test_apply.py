"""apply_plan: effects, idempotency and refusals (n3rv#9)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from n3rv import doctor
from n3rv.init.binary import resolve_n3rv_binary
from n3rv.init.context import Stack
from n3rv.init.update import MARKER_START, run_update
from n3rv.reconcile import load_state, save_state, state_from_tree
from n3rv.reconcile.apply import apply_plan
from n3rv.reconcile.plan import build_plan
from n3rv.reconcile.state import Components, ProjectIdentity, ProjectState

TEMPLATES = Path(__import__("n3rv").__file__).parent / "init" / "templates"
BINARY = resolve_n3rv_binary() or "n3rv"


def _plan(root: Path):
    return build_plan(root, load_state(root), templates_dir=TEMPLATES, n3rv_version="dev", n3rv_binary=BINARY)


def _apply(root: Path):
    return apply_plan(root, load_state(root), templates_dir=TEMPLATES, n3rv_version="dev", n3rv_binary=BINARY)


def _snapshot(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file() and p.name != "state.yaml"
    }


class TestConvergence:
    def test_apply_converges_a_drifted_tree(self, tmp_path: Path, capsys) -> None:
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "demo"\nversion = "0.1.0"\n')
        run_update(tmp_path)
        capsys.readouterr()
        save_state(tmp_path, state_from_tree(tmp_path))
        target = tmp_path / ".opencode/agents/n3rv.md"
        target.write_text("wrecked\n")

        result = _apply(tmp_path)

        assert result.ok
        assert _plan(tmp_path).empty
        assert target.read_text(encoding="utf-8") != "wrecked\n"

    def test_apply_twice_writes_nothing_the_second_time(self, tmp_path: Path, capsys) -> None:
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "demo"\nversion = "0.1.0"\n')
        run_update(tmp_path)
        capsys.readouterr()
        save_state(tmp_path, state_from_tree(tmp_path))

        first = _apply(tmp_path)
        after_first = _snapshot(tmp_path)
        lock_after_first = (tmp_path / ".n3rv/template-lock.json").read_bytes()

        second = _apply(tmp_path)

        assert first.ok and second.ok
        assert second.applied == []
        assert _snapshot(tmp_path) == after_first
        assert (tmp_path / ".n3rv/template-lock.json").read_bytes() == lock_after_first

    def test_apply_creates_a_missing_file(self, tmp_path: Path) -> None:
        state = ProjectState(
            project=ProjectIdentity(name="demo", stack=Stack.GENERIC),
            components=Components(agents=["git-ops"]),
        )
        save_state(tmp_path, state)

        result = _apply(tmp_path)

        assert result.ok
        assert (tmp_path / ".opencode/agents/git-ops.md").is_file()


class TestRefusals:
    def test_update_refuses_when_a_state_file_exists(self, tmp_path: Path) -> None:
        save_state(tmp_path, ProjectState(project=ProjectIdentity(name="demo", stack=Stack.GENERIC)))

        code = run_update(tmp_path)

        assert code == 1

    def test_update_refusal_names_the_alternative(self, tmp_path: Path, capsys) -> None:
        save_state(tmp_path, ProjectState(project=ProjectIdentity(name="demo", stack=Stack.GENERIC)))

        run_update(tmp_path)

        out = capsys.readouterr().out
        assert "n3rv plan" in out
        assert "n3rv apply" in out

    def test_conflicts_are_never_written(self, tmp_path: Path) -> None:
        """A CONFLICT action must leave the file exactly as it was."""
        state = ProjectState(project=ProjectIdentity(name="demo", stack=Stack.GENERIC))
        save_state(tmp_path, state)
        _apply(tmp_path)
        agents = tmp_path / "AGENTS.md"
        text = agents.read_text(encoding="utf-8")
        line_end = text.find("\n", text.find(MARKER_START))
        tampered = text[: line_end + 1] + "edited by hand\n" + text[line_end + 1 :]
        agents.write_text(tampered, encoding="utf-8")

        result = _apply(tmp_path)

        assert agents.read_text(encoding="utf-8") == tampered
        assert not [a for a in result.applied if a.target == "AGENTS.md"]


class TestLockfileHonesty:
    def test_entries_exist_only_for_files_that_landed(self, tmp_path: Path) -> None:
        state = ProjectState(project=ProjectIdentity(name="demo", stack=Stack.GENERIC))
        save_state(tmp_path, state)

        _apply(tmp_path)

        import json

        entries = json.loads((tmp_path / ".n3rv/template-lock.json").read_text())["entries"]
        for rel_path in entries:
            assert (tmp_path / rel_path).is_file(), f"lockfile describes {rel_path}, which does not exist"

    def test_context_hash_records_the_state(self, tmp_path: Path) -> None:
        state = ProjectState(project=ProjectIdentity(name="demo", stack=Stack.GENERIC))
        save_state(tmp_path, state)

        _apply(tmp_path)

        import json

        entries = json.loads((tmp_path / ".n3rv/template-lock.json").read_text())["entries"]
        assert {e["context_hash"] for e in entries.values()} == {state.sha256()}


class TestProvenance:
    def test_a_file_apply_writes_records_the_template_revision(self, tmp_path: Path, capsys) -> None:
        """n3rv#17: an entry without template_sha256 cannot be traced to a
        revision, so a drifted file cannot be told apart from a moved
        template. Every write path records it."""
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "demo"\nversion = "0.1.0"\n')
        run_update(tmp_path)
        capsys.readouterr()
        save_state(tmp_path, state_from_tree(tmp_path))
        target_rel = ".opencode/agents/n3rv.md"
        (tmp_path / target_rel).write_text("wrecked\n")

        result = _apply(tmp_path)

        assert result.ok
        entries = json.loads((tmp_path / ".n3rv/template-lock.json").read_text())["entries"]
        entry = entries[target_rel]
        # The invariant: the recorded revision is the hash of the file the
        # entry itself names, so a later template change is detectable.
        template = TEMPLATES / entry["template"]
        assert template.is_file()
        assert entry["template_sha256"] == hashlib.sha256(template.read_bytes()).hexdigest()

    def test_an_entry_without_a_recorded_revision_is_untraced_not_failed(self, tmp_path: Path) -> None:
        """Old entries must be reported as untraced, not as failures."""
        (tmp_path / ".opencode").mkdir()
        lock = tmp_path / ".n3rv" / "template-lock.json"
        lock.parent.mkdir()
        lock.write_text(
            json.dumps(
                {
                    "entries": {
                        "opencode.json": {
                            "sha256": "x",
                            "template": "opencode/agents.md.j2",
                            "context_hash": "y",
                        }
                    }
                }
            ),
            encoding="utf-8",
        )

        result = doctor.check_convergence(tmp_path, templates_dir=TEMPLATES)

        assert not result.failed
        assert any("untraced" in finding.message for finding in result.findings)
        assert not any("all 1 tracked files match" in finding.message for finding in result.findings)


class TestInvalidState:
    def test_an_unknown_component_writes_nothing_at_all(self, tmp_path: Path, capsys) -> None:
        """A typo in components must not become 'delete everything else'."""
        from n3rv.init.context import Stack
        from n3rv.reconcile.state import ProjectIdentity, ProjectState

        (tmp_path / "pyproject.toml").write_text('[project]\nname = "demo"\nversion = "0.1.0"\n')
        run_update(tmp_path)
        capsys.readouterr()
        save_state(tmp_path, state_from_tree(tmp_path))
        populated = _snapshot(tmp_path)

        save_state(
            tmp_path,
            ProjectState(
                project=ProjectIdentity(name="demo", stack=Stack.GENERIC),
                components=Components(skills=["does-not-exist"]),
            ),
            force=True,
        )
        result = _apply(tmp_path)

        assert not result.ok
        assert "invalid" in (result.error or "")
        assert result.applied == []
        assert _snapshot(tmp_path) == populated
