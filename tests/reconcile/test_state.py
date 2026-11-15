"""ProjectState: schema, serialization and tree-derived initialization (n3rv#9)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from n3rv.init import FILE_MANIFEST, family_for
from n3rv.init.context import Stack
from n3rv.reconcile import StateError, load_state, save_state, state_from_tree, state_path
from n3rv.reconcile.state import BoardTarget, Components, McpState, ProjectIdentity, ProjectState


def _state(**overrides: object) -> ProjectState:
    data: dict[str, object] = {
        "project": {"name": "radio", "stack": "generic"},
        "components": {"agents": ["git-ops"], "skills": ["code"], "commands": ["review"], "hooks": ["pre-push"]},
        "mcp": {"servers": ["sequential-thinking"]},
    }
    data.update(overrides)
    return ProjectState.model_validate(data)


def _write_state(root: Path, text: str) -> Path:
    path = state_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class TestSchema:
    def test_minimal_state_round_trips(self, tmp_path: Path) -> None:
        state = _state()
        save_state(tmp_path, state)

        loaded = load_state(tmp_path)

        assert loaded == state
        assert loaded.schema_version == 1

    def test_serialized_yaml_uses_the_alias(self, tmp_path: Path) -> None:
        text = _state().to_yaml()
        assert text.startswith("schema: 1")

    def test_unknown_key_is_rejected(self, tmp_path: Path) -> None:
        _write_state(
            tmp_path,
            "schema: 1\nproject:\n  name: radio\n  stack: generic\ncomponents:\n  agents: []\n  skillz: []\n",
        )

        with pytest.raises(StateError, match="does not match schema"):
            load_state(tmp_path)

    def test_unknown_schema_version_is_rejected(self, tmp_path: Path) -> None:
        _write_state(tmp_path, "schema: 2\nproject:\n  name: radio\n  stack: generic\n")
        with pytest.raises(StateError):
            load_state(tmp_path)

    def test_missing_file_names_the_fix(self, tmp_path: Path) -> None:
        with pytest.raises(StateError, match="n3rv state init"):
            load_state(tmp_path)

    def test_empty_name_is_rejected(self, tmp_path: Path) -> None:
        _write_state(tmp_path, "schema: 1\nproject:\n  name: ''\n  stack: generic\n")
        with pytest.raises(StateError):
            load_state(tmp_path)

    def test_board_target_is_optional(self, tmp_path: Path) -> None:
        state = _state(board={"owner": "The-Replacement", "project": 1})
        save_state(tmp_path, state)
        assert load_state(tmp_path).board == BoardTarget(owner="The-Replacement", project=1)


class TestDeterminism:
    def test_sha_is_stable_across_instances(self) -> None:
        assert _state().sha256() == _state().sha256()

    def test_sha_is_independent_of_list_order_of_construction(self) -> None:
        """Canonical form sorts keys; component lists keep their declared order."""
        a = _state(components={"agents": ["a", "b"], "skills": [], "commands": [], "hooks": []})
        b = _state(components={"hooks": [], "commands": [], "skills": [], "agents": ["a", "b"]})
        assert a.sha256() == b.sha256()

    def test_serialization_has_no_timestamp(self) -> None:
        text = _state().to_yaml()
        assert "timestamp" not in text
        assert "updated_at" not in text

    def test_save_is_byte_identical_on_rewrite(self, tmp_path: Path) -> None:
        save_state(tmp_path, _state())
        first = state_path(tmp_path).read_bytes()
        save_state(tmp_path, _state(), force=True)
        assert state_path(tmp_path).read_bytes() == first


class TestSaveSafety:
    def test_second_save_without_force_refuses_and_keeps_the_file(self, tmp_path: Path) -> None:
        save_state(tmp_path, _state())
        original = state_path(tmp_path).read_bytes()

        with pytest.raises(StateError, match="--force"):
            save_state(tmp_path, _state(project={"name": "other", "stack": "python"}))

        assert state_path(tmp_path).read_bytes() == original


class TestStateFromTree:
    def test_records_only_components_present_on_disk(self, tmp_path: Path) -> None:
        (tmp_path / ".opencode/skills/code").mkdir(parents=True)
        (tmp_path / ".opencode/skills/code/SKILL.md").write_text("x")
        (tmp_path / ".opencode/agents").mkdir(parents=True)
        (tmp_path / ".opencode/agents/git-ops.md").write_text("x")

        state = state_from_tree(tmp_path)

        assert state.components.skills == ["code"]
        assert state.components.agents == ["git-ops"]
        assert state.components.commands == []
        assert state.components.hooks == []

    def test_ignores_files_that_are_not_in_the_manifest(self, tmp_path: Path) -> None:
        (tmp_path / ".opencode/agents").mkdir(parents=True)
        (tmp_path / ".opencode/agents/hand-written.md").write_text("x")

        assert state_from_tree(tmp_path).components.agents == []

    def test_reads_mcp_servers_from_the_project_config(self, tmp_path: Path) -> None:
        (tmp_path / "opencode.json").write_text(json.dumps({"mcp": {"servers": {"b": {}, "a": {}}}}))

        assert state_from_tree(tmp_path).mcp.servers == ["a", "b"]

    def test_reads_mcp_servers_from_a_v1_config(self, tmp_path: Path) -> None:
        """Radio's project config still uses the V1 layout; detection must not win."""
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "demo"\n')
        (tmp_path / "opencode.json").write_text(
            json.dumps({"mcp": {"sequential-thinking": {"type": "local", "command": ["x"]}}})
        )

        assert state_from_tree(tmp_path).mcp.servers == ["sequential-thinking"]

    def test_declared_empty_server_list_is_not_overridden_by_detection(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "demo"\n')
        (tmp_path / "opencode.json").write_text(json.dumps({"mcp": {"servers": {}}}))

        assert state_from_tree(tmp_path).mcp.servers == []

    def test_falls_back_to_detection_without_a_config(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "demo"\n')

        state = state_from_tree(tmp_path)

        assert state.project.name == "demo"
        assert state.project.stack is Stack.PYTHON
        assert "sequential-thinking" in state.mcp.servers

    def test_declared_components_all_exist_in_the_manifest(self, tmp_path: Path) -> None:
        """A name in state must be a name the generator can install."""
        known = {
            "agents": {e[1].split("/")[-1][:-3] for e in FILE_MANIFEST if family_for(e[1]) == "agents"},
            "skills": {e[1].split("/")[-2] for e in FILE_MANIFEST if family_for(e[1]) == "skills"},
        }
        (tmp_path / ".opencode/skills/code").mkdir(parents=True)
        (tmp_path / ".opencode/skills/code/SKILL.md").write_text("x")

        state = state_from_tree(tmp_path)

        assert set(state.components.skills) <= known["skills"]
        assert set(state.components.agents) <= known["agents"]


class TestComponentsModel:
    def test_defaults_are_empty_not_missing(self) -> None:
        state = ProjectState(project=ProjectIdentity(name="x", stack=Stack.GENERIC))
        assert state.components == Components()
        assert state.mcp == McpState()
        assert state.board is None
