"""Tests for update orchestrator and utilities.

Covers run_update() integration, deep_merge_json(), validate_markers(),
and update strategies.
"""

from pathlib import Path

from n3rv.init.update import (
    FILE_UPDATE_MANIFEST,
    UpdateEntry,
    UpdateResult,
    UpdateStrategy,
    UpdateSummary,
    _handle_create_if_missing,
    deep_merge_json,
)
from n3rv.init.writer import MARKER_END, MARKER_START, validate_markers


def test_validate_markers_clean_content_returns_empty():
    """Test clean balanced markers return no warnings."""
    content = f"before\n{MARKER_START}\nstuff\n{MARKER_END}\nafter"
    assert validate_markers(content) == []


def test_validate_markers_no_markers_returns_empty():
    """Test content with no markers returns no warnings."""
    assert validate_markers("no markers here") == []


def test_validate_markers_mismatched_returns_warning():
    """Test mismatched markers return warning."""
    content = f"{MARKER_START}\nno end marker"
    warnings = validate_markers(content)
    assert len(warnings) == 1
    assert "Mismatched" in warnings[0]


def test_validate_markers_multiple_pairs_returns_warning():
    """Test multiple marker pairs return warning."""
    content = f"{MARKER_START}\na\n{MARKER_END}\n{MARKER_START}\nb\n{MARKER_END}"
    warnings = validate_markers(content)
    assert len(warnings) == 1
    assert "Multiple" in warnings[0]


def test_deep_merge_preserves_user_only_keys():
    """Test merge preserves user keys not in overlay."""
    base = {"user_key": "user_value", "shared": "old"}
    overlay = {"shared": "new", "n3rv_key": "inv_value"}
    result = deep_merge_json(base, overlay)
    assert result["user_key"] == "user_value"
    assert result["shared"] == "new"
    assert result["n3rv_key"] == "inv_value"


def test_deep_merge_recursive_nested_dicts():
    """Test merge handles nested dicts recursively."""
    base = {"permissions": {"allow": ["user_tool"], "deny": []}}
    overlay = {"permissions": {"allow": ["inv_tool"], "extra": True}}
    result = deep_merge_json(base, overlay)
    assert result["permissions"]["allow"] == ["inv_tool"]
    assert result["permissions"]["deny"] == []
    assert result["permissions"]["extra"] is True


def test_deep_merge_empty_base():
    """Test merge with empty base returns overlay."""
    result = deep_merge_json({}, {"key": "value"})
    assert result == {"key": "value"}


def test_deep_merge_empty_overlay():
    """Test merge with empty overlay preserves base."""
    result = deep_merge_json({"key": "value"}, {})
    assert result == {"key": "value"}


def test_manifest_has_expected_entries():
    """Test manifest has expected number of entries."""
    assert len(FILE_UPDATE_MANIFEST) == 44


def test_manifest_marker_files():
    """Test marker merge files are correctly identified."""
    marker_files = [e for e in FILE_UPDATE_MANIFEST if e.strategy == UpdateStrategy.MARKER_MERGE]
    assert len(marker_files) == 1  # AGENTS.md is marker_merge


def test_manifest_json_merge_files():
    """Test JSON merge files are correctly identified."""
    json_files = [e for e in FILE_UPDATE_MANIFEST if e.strategy == UpdateStrategy.JSON_MERGE]
    paths = {e.output_path for e in json_files}
    assert "opencode.json" in paths
    assert "opencode.template.json" in paths
    assert ".opencode/package.json" in paths
    assert len(json_files) == 3


def test_manifest_overwrite_files():
    """Test overwrite files include infrastructure files."""
    ow_files = [e for e in FILE_UPDATE_MANIFEST if e.strategy == UpdateStrategy.OVERWRITE]
    paths = {e.output_path for e in ow_files}
    assert ".n3rv/a2a-config.yaml" in paths
    assert ".opencode/plugins/n3rv-lifecycle.js" in paths
    assert ".opencode/plugins/n3rv-shell-env.js" in paths
    assert ".opencode/tools/n3rv-stats.ts" in paths
    assert len(ow_files) == 6


def test_manifest_skip_default_entries_are_commands():
    """Only slash commands are skip-default; agents and skills are generated."""
    skip_files = [e for e in FILE_UPDATE_MANIFEST if e.strategy == UpdateStrategy.SKIP_DEFAULT]
    paths = {e.output_path for e in skip_files}
    assert paths == {
        ".opencode/commands/sdd-new.md",
        ".opencode/commands/judgment-day.md",
        ".opencode/commands/review.md",
        ".opencode/commands/handoff.md",
    }


def test_manifest_generated_covers_agents_and_skills():
    """Agents and skills are refreshed by every update (#5)."""
    generated = [e for e in FILE_UPDATE_MANIFEST if e.strategy == UpdateStrategy.GENERATED]
    paths = {e.output_path for e in generated}
    assert ".opencode/agents/n3rv.md" in paths
    assert ".opencode/agents/n3rv-scout.md" in paths
    assert ".opencode/agents/n3rv-sensor.md" in paths
    assert ".opencode/agents/sdd-explorer.md" in paths
    assert ".opencode/agents/project-ops.md" in paths
    assert ".opencode/skills/code/SKILL.md" in paths
    assert ".opencode/skills/sdd-explore/SKILL.md" in paths
    assert ".opencode/skills/project-board/SKILL.md" in paths


def test_manifest_project_owned_files_are_guarded():
    """AGENTS.md and .githooks/pre-push belong to the project (#5)."""
    owned = {e.output_path for e in FILE_UPDATE_MANIFEST if e.project_owned}
    assert owned == {"AGENTS.md", ".githooks/pre-push"}


def test_manifest_git_hooks_are_executable():
    """Test git hooks have executable flag."""
    hook_files = [e for e in FILE_UPDATE_MANIFEST if "githooks" in e.template_name]
    assert all(e.make_executable for e in hook_files)


def test_manifest_create_if_missing_files():
    """Test create-if-missing files — shared/rpc.js and new-satellite command."""
    cim_files = [e for e in FILE_UPDATE_MANIFEST if e.strategy == UpdateStrategy.CREATE_IF_MISSING]
    assert len(cim_files) == 2
    paths = {e.output_path for e in cim_files}
    assert paths == {".opencode/shared/rpc.js", ".opencode/commands/new-satellite.md"}


def test_update_summary_counts_correctly():
    """Test UpdateSummary counts results correctly."""
    summary = UpdateSummary(
        results=[
            UpdateResult("AGENTS.md", UpdateStrategy.MARKER_MERGE, "UPDATED"),
            UpdateResult("opencode.json", UpdateStrategy.JSON_MERGE, "SKIPPED"),
            UpdateResult(".githooks/pre-push", UpdateStrategy.OVERWRITE, "OVERWRITTEN"),
            UpdateResult(".opencode/skills/code/SKILL.md", UpdateStrategy.SKIP_DEFAULT, "SKIPPED"),
        ]
    )
    assert summary.updated_count == 2  # UPDATED + OVERWRITTEN
    assert summary.skipped_count == 2
    assert summary.error_count == 0


def test_create_if_missing_creates_when_absent(tmp_path: Path):
    """Test create-if-missing creates file when it does not exist."""
    target = tmp_path / "AGENTS.md"
    entry = UpdateEntry("opencode/AGENTS.md.j2", "AGENTS.md", UpdateStrategy.CREATE_IF_MISSING)

    result = _handle_create_if_missing(entry, target, "# content", dry_run=False)

    assert result.result == "CREATED"
    assert target.exists()
    assert target.read_text() == "# content"


def test_create_if_missing_skips_when_present(tmp_path: Path):
    """Test create-if-missing skips file when it already exists."""
    target = tmp_path / "AGENTS.md"
    target.write_text("# user customized content")
    entry = UpdateEntry("opencode/AGENTS.md.j2", "AGENTS.md", UpdateStrategy.CREATE_IF_MISSING)

    result = _handle_create_if_missing(entry, target, "# new content", dry_run=False)

    assert result.result == "SKIPPED"
    assert target.read_text() == "# user customized content"


def test_create_if_missing_dry_run_reports_created(tmp_path: Path):
    """Test create-if-missing dry run reports CREATED without writing."""
    target = tmp_path / "AGENTS.md"
    entry = UpdateEntry("opencode/AGENTS.md.j2", "AGENTS.md", UpdateStrategy.CREATE_IF_MISSING)

    result = _handle_create_if_missing(entry, target, "# content", dry_run=True)

    assert result.result == "CREATED"
    assert not target.exists()


# ── convergence safety (#5) ───────────────────────────────────────────


def test_update_refreshes_generated_agents_and_skills(initialized_project: Path):
    """`n3rv update` refreshes .opencode/agents and .opencode/skills (#5)."""
    from n3rv.init.update import run_update

    agent = initialized_project / ".opencode" / "agents" / "n3rv.md"
    skill = initialized_project / ".opencode" / "skills" / "code" / "SKILL.md"
    agent.write_text("tampered agent\n", encoding="utf-8")
    skill.write_text("tampered skill\n", encoding="utf-8")

    run_update(initialized_project)

    assert "N3RV orchestration agent" in agent.read_text(encoding="utf-8")
    assert "tampered" not in agent.read_text(encoding="utf-8")
    assert skill.read_text(encoding="utf-8") != "tampered skill\n"


def test_update_reports_every_file_it_did_not_touch(initialized_project: Path, capsys):
    """Skipped and unchanged files are named, not silently dropped (#5)."""
    from n3rv.init.update import run_update

    run_update(initialized_project)
    capsys.readouterr()

    run_update(initialized_project)

    output = capsys.readouterr().out
    assert "Not touched" in output
    assert ".opencode/commands/sdd-new.md" in output
    assert "use --force-commands to update" in output


def test_update_removes_retired_generated_mcp_servers(initialized_project: Path):
    """A server the generator no longer recommends is pruned (#5)."""
    import json

    from n3rv.init.lockfile import save_managed_state
    from n3rv.init.update import run_update

    config_path = initialized_project / "opencode.json"
    data = json.loads(config_path.read_text(encoding="utf-8"))
    data["mcp"]["servers"]["retired"] = {"type": "local", "command": ["retired-server"]}
    data["mcp"]["servers"]["user-server"] = {"type": "local", "command": ["my-own-server"]}
    config_path.write_text(json.dumps(data), encoding="utf-8")
    save_managed_state(initialized_project, {"mcp_servers": ["sequential-thinking", "retired"]})

    run_update(initialized_project)

    servers = json.loads(config_path.read_text(encoding="utf-8"))["mcp"]["servers"]
    assert "retired" not in servers
    assert "user-server" in servers
    assert "sequential-thinking" in servers


def test_update_migrates_flat_v1_mcp_servers(initialized_project: Path):
    """V1 flat mcp.<name> entries move under mcp.servers (#6)."""
    import json

    from n3rv.init.lockfile import save_managed_state
    from n3rv.init.update import run_update

    config_path = initialized_project / "opencode.json"
    data = json.loads(config_path.read_text(encoding="utf-8"))
    data["mcp"].pop("servers", None)
    data["mcp"]["sequential-thinking"] = {"type": "local", "command": ["flat-v1"]}
    config_path.write_text(json.dumps(data), encoding="utf-8")
    save_managed_state(initialized_project, {"mcp_servers": ["sequential-thinking"]})

    run_update(initialized_project)

    mcp = json.loads(config_path.read_text(encoding="utf-8"))["mcp"]
    assert "sequential-thinking" not in mcp
    assert mcp["servers"]["sequential-thinking"]["type"] == "local"


def _tamper_inside_markers(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    start = text.find(MARKER_START) + len(MARKER_START)
    path.write_text(f"{text[:start]}\nUSER HACKED THIS BLOCK\n{text[start:]}", encoding="utf-8")


def test_update_skips_agents_md_when_generated_block_was_edited(initialized_project: Path):
    """AGENTS.md is project-owned once its generated block is edited (#5)."""
    from n3rv.init.update import run_update

    agents_md = initialized_project / "AGENTS.md"
    _tamper_inside_markers(agents_md)

    run_update(initialized_project)

    assert "USER HACKED THIS BLOCK" in agents_md.read_text(encoding="utf-8")


def test_update_force_project_files_restores_agents_md(initialized_project: Path):
    """--force-project-files is the explicit opt-in."""
    from n3rv.init.update import run_update

    agents_md = initialized_project / "AGENTS.md"
    _tamper_inside_markers(agents_md)

    run_update(initialized_project, force_project_files=True)

    content = agents_md.read_text(encoding="utf-8")
    assert "USER HACKED THIS BLOCK" not in content
    assert MARKER_START in content


def test_update_preserves_user_prose_outside_agents_md_markers(initialized_project: Path):
    """Editing outside the marker block is expected and never blocks updates."""
    from n3rv.init.update import run_update

    agents_md = initialized_project / "AGENTS.md"
    agents_md.write_text(
        agents_md.read_text(encoding="utf-8") + "\n## My Notes\nkeep me around\n",
        encoding="utf-8",
    )

    run_update(initialized_project)

    content = agents_md.read_text(encoding="utf-8")
    assert "## My Notes" in content
    assert "keep me around" in content
    assert "Language: English everywhere" in content


class TestJsonKeyOwnership:
    """managed.json_keys: per-key ownership for JSON_MERGE outputs (n3rv#14/#16)."""

    def _entry(self, output_path: str = "opencode.json") -> UpdateEntry:
        return UpdateEntry("opencode.json.j2", output_path, UpdateStrategy.JSON_MERGE)

    def test_rename_converges_when_key_is_owned(self, tmp_path: Path) -> None:
        """AC-1.1: owned key absent from render is deleted; new key present; no duplicate."""
        import json

        from n3rv.init.lockfile import save_managed_state
        from n3rv.init.update import resolve_text

        target = tmp_path / "opencode.json"
        target.write_text(json.dumps({"command": "old", "user_key": "keep"}), encoding="utf-8")
        save_managed_state(tmp_path, {"json_keys": {"opencode.json": {".": ["command"]}}})
        rendered = json.dumps({"commands": {"sdd-new": {}}, "user_key": "keep"})

        res = resolve_text(self._entry(), tmp_path, target, rendered)

        assert res.error is None
        data = json.loads(res.text or "")
        assert "commands" in data
        assert "command" not in data
        assert data["user_key"] == "keep"
        assert res.owned_keys == {".": ["commands", "user_key"]}
        assert any("removed JSON key 'command'" in n for n in res.notes)

    def test_user_key_survives_byte_identical(self, tmp_path: Path) -> None:
        """AC-1.2: unowned on-disk key is preserved byte-identically."""
        import json

        from n3rv.init.lockfile import save_managed_state
        from n3rv.init.update import resolve_text

        target = tmp_path / "opencode.json"
        target.write_text(
            json.dumps({"default_agent": "n3rv", "user_extra": {"nested": [1, 2]}}),
            encoding="utf-8",
        )
        save_managed_state(tmp_path, {"json_keys": {"opencode.json": {".": ["default_agent"]}}})
        rendered = json.dumps({"default_agent": "n3rv"})

        res = resolve_text(self._entry(), tmp_path, target, rendered)

        data = json.loads(res.text or "")
        assert data["user_extra"] == {"nested": [1, 2]}
        assert data["default_agent"] == "n3rv"

    def test_first_apply_deferral_preserves_stale_key_and_reports(self, tmp_path: Path) -> None:
        """AC-1.3 / deferral: no prior record -> nothing deleted, unowned keys reported."""
        import json

        from n3rv.init.update import resolve_text

        target = tmp_path / "opencode.json"
        target.write_text(json.dumps({"command": "stale", "commands": {"x": 1}}), encoding="utf-8")
        rendered = json.dumps({"commands": {"sdd-new": {}}})

        res = resolve_text(self._entry(), tmp_path, target, rendered)

        data = json.loads(res.text or "")
        # deferral: stale "command" preserved (not in render, but no ownership record to delete it)
        assert "command" in data
        assert "commands" in data
        assert any("preserved unowned JSON key 'command'" in n for n in res.notes)
        assert res.owned_keys == {".": ["commands"]}
