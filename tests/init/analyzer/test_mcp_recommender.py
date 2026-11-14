"""Tests for MCPRecommender — per-project MCP server recommendations."""

from __future__ import annotations

from pathlib import Path

from n3rv.init.analyzer.mcp_recommender import MCPRecommender
from n3rv.init.analyzer.profile import StructureInfo
from n3rv.init.context import Stack


class TestMCPRecommender:
    def test_secured_global_servers_are_inherited(self, tmp_path: Path) -> None:
        """Project recommendations never override global GitHub or Context7 servers."""
        structure = StructureInfo()
        recommender = MCPRecommender()

        for stack in Stack:
            servers = recommender.detect(tmp_path, stack, structure)
            names = {s.name for s in servers}
            assert "github" not in names, f"GitHub override generated for stack={stack}"
            assert "context7" not in names, f"Context7 override generated for stack={stack}"

    def test_web_project_gets_chrome_devtools(self, tmp_path: Path) -> None:
        """Projects with web files get chrome-devtools MCP server."""
        structure = StructureInfo(has_web_files=True)
        recommender = MCPRecommender()

        servers = recommender.detect(tmp_path, Stack.GENERIC, structure)
        names = {s.name for s in servers}
        assert "chrome-devtools" in names

    def test_node_stack_gets_chrome_devtools_even_without_web_files(self, tmp_path: Path) -> None:
        """Node projects get chrome-devtools even if web files not explicitly detected."""
        structure = StructureInfo(has_web_files=False)
        recommender = MCPRecommender()

        servers = recommender.detect(tmp_path, Stack.NODE, structure)
        names = {s.name for s in servers}
        assert "chrome-devtools" in names

    def test_non_web_project_no_chrome_devtools(self, tmp_path: Path) -> None:
        """Non-web Python projects don't get chrome-devtools."""
        structure = StructureInfo(has_web_files=False)
        recommender = MCPRecommender()

        servers = recommender.detect(tmp_path, Stack.PYTHON, structure)
        names = {s.name for s in servers}
        assert "chrome-devtools" not in names

    def test_python_gets_sequential_thinking(self, tmp_path: Path) -> None:
        """Python projects get sequential-thinking for complex reasoning."""
        structure = StructureInfo()
        recommender = MCPRecommender()

        servers = recommender.detect(tmp_path, Stack.PYTHON, structure)
        names = {s.name for s in servers}
        assert "sequential-thinking" in names

    def test_generic_no_sequential_thinking(self, tmp_path: Path) -> None:
        """Non-Python projects don't get sequential-thinking."""
        structure = StructureInfo()
        recommender = MCPRecommender()

        for stack in (Stack.GENERIC, Stack.NODE, Stack.GO):
            servers = recommender.detect(tmp_path, stack, structure)
            names = {s.name for s in servers}
            assert "sequential-thinking" not in names, f"sequential-thinking present for stack={stack}"

    def test_no_duplicate_servers(self, tmp_path: Path) -> None:
        """Even when multiple conditions match, servers are deduplicated."""
        structure = StructureInfo(has_web_files=True)
        recommender = MCPRecommender()

        # Python + web should have sequential-thinking + chrome-devtools.
        servers = recommender.detect(tmp_path, Stack.PYTHON, structure)
        names = [s.name for s in servers]

        assert len(names) == len(set(names)), f"Duplicates found: {names}"
        assert len(servers) == 2

    def test_servers_have_required_fields(self, tmp_path: Path) -> None:
        """All recommended servers have type and command (for local) or url (for remote)."""
        structure = StructureInfo(has_web_files=True)
        recommender = MCPRecommender()

        servers = recommender.detect(tmp_path, Stack.PYTHON, structure)
        for srv in servers:
            assert srv.name, "Server name is required"
            assert srv.type in ("local", "remote"), f"Invalid type for {srv.name}: {srv.type}"
            if srv.type == "local":
                assert srv.command, f"Local server {srv.name} missing command"
            else:
                assert srv.url, f"Remote server {srv.name} missing url"

    def test_no_unpinned_global_server_commands(self, tmp_path: Path) -> None:
        """Recommendations cannot reintroduce the removed overrides under another name."""
        structure = StructureInfo(has_web_files=True)
        recommender = MCPRecommender()
        forbidden_packages = ("@modelcontextprotocol/server-github", "@upstash/context7-mcp")

        for stack in Stack:
            servers = recommender.detect(tmp_path, stack, structure)
            commands = [" ".join(s.command or []) for s in servers]
            assert all(package not in command for package in forbidden_packages for command in commands)

    def test_no_n3rv_servers_recommended(self, tmp_path: Path) -> None:
        """n3rv's own MCP servers are never written into project config.

        They are inherited from the user's global opencode config. Emitting
        them with bare commands (["n3rv-exec"]) makes project config override a
        working global entry with one that only resolves when n3rv happens to
        be on PATH, which mise shims break.
        """
        structure = StructureInfo(has_web_files=True)
        recommender = MCPRecommender()

        for stack in Stack:
            servers = recommender.detect(tmp_path, stack, structure)
            leaked = [s.name for s in servers if s.name.startswith("n3rv-")]
            assert not leaked, f"n3rv servers leaked into scaffold for {stack}: {leaked}"

    def test_python_server_order(self, tmp_path: Path) -> None:
        """Python projects retain their stack-specific server after global inheritance."""
        structure = StructureInfo()
        recommender = MCPRecommender()

        servers = recommender.detect(tmp_path, Stack.PYTHON, structure)
        names = [s.name for s in servers]
        assert names == ["sequential-thinking"]
