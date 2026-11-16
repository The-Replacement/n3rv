"""Tests for read-only convergence diagnostics (``n3rv doctor``)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from n3rv import doctor
from n3rv.init import SDD_PHASE_MAP

CommandResult = doctor.CommandResult


class FakeRunner:
    """Match commands by prefix and return canned results."""

    def __init__(self, responses: list[tuple[tuple[str, ...], CommandResult]]) -> None:
        self.responses = responses
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str], cwd: Path | None = None, timeout: int = 30) -> CommandResult:
        self.calls.append(cmd)
        for prefix, result in self.responses:
            if tuple(cmd[: len(prefix)]) == prefix:
                return result
        return CommandResult(1, "", "no fake response")


def _git_root(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    return tmp_path


# ── drift ─────────────────────────────────────────────────────────────


def test_drift_flags_workstation_behind_github(tmp_path: Path):
    root = _git_root(tmp_path)
    runner = FakeRunner(
        [
            (("git", "rev-parse", "HEAD"), CommandResult(0, "aaa111\n")),
            (("git", "rev-parse", "--abbrev-ref", "HEAD"), CommandResult(0, "main\n")),
            (("git", "remote", "get-url", "origin"), CommandResult(0, "https://github.com/o/r.git\n")),
            (("gh", "api", "repos/o/r", "--jq"), CommandResult(0, "main\n")),
            (("gh", "api", "repos/o/r/commits/main", "--jq"), CommandResult(0, "bbb222\n")),
            (("git", "remote", "get-url", "mainframe"), CommandResult(1, "", "missing")),
        ]
    )

    result = doctor.check_drift(root, runner)

    assert result.failed
    assert any("GitHub" in finding.message and finding.level == "fail" for finding in result.findings)


def test_drift_clean_three_environments(tmp_path: Path):
    root = _git_root(tmp_path)
    runner = FakeRunner(
        [
            (("git", "rev-parse", "HEAD"), CommandResult(0, "aaa111\n")),
            (("git", "rev-parse", "--abbrev-ref", "HEAD"), CommandResult(0, "main\n")),
            (("git", "remote", "get-url", "origin"), CommandResult(0, "git@github.com:o/r.git\n")),
            (("gh", "api", "repos/o/r", "--jq"), CommandResult(0, "main\n")),
            (("gh", "api", "repos/o/r/commits/main", "--jq"), CommandResult(0, "aaa111\n")),
            (("git", "remote", "get-url", "mainframe"), CommandResult(0, "ssh://box/repo\n")),
            (("git", "ls-remote", "--exit-code", "mainframe", "HEAD"), CommandResult(0, "aaa111\tHEAD\n")),
        ]
    )

    result = doctor.check_drift(root, runner)

    assert not result.failed


def test_drift_reports_unreachable_mainframe_as_skip(tmp_path: Path):
    root = _git_root(tmp_path)
    runner = FakeRunner(
        [
            (("git", "rev-parse", "HEAD"), CommandResult(0, "aaa111\n")),
            (("git", "rev-parse", "--abbrev-ref", "HEAD"), CommandResult(0, "main\n")),
            (("git", "remote", "get-url", "origin"), CommandResult(0, "https://github.com/o/r.git\n")),
            (("gh", "api", "repos/o/r", "--jq"), CommandResult(0, "main\n")),
            (("gh", "api", "repos/o/r/commits/main", "--jq"), CommandResult(0, "aaa111\n")),
            (("git", "remote", "get-url", "mainframe"), CommandResult(0, "ssh://box/repo\n")),
            (("git", "ls-remote", "--exit-code", "mainframe", "HEAD"), CommandResult(255, "", "unreachable")),
        ]
    )

    result = doctor.check_drift(root, runner)

    assert not result.failed
    assert any(finding.level == "skip" and "unreachable" in finding.message for finding in result.findings)


def test_drift_skips_outside_a_git_repository(tmp_path: Path):
    result = doctor.check_drift(tmp_path, FakeRunner([]))

    assert not result.failed
    assert result.findings[0].level == "skip"


# ── models ────────────────────────────────────────────────────────────


def _catalog_with(*model_ids: str) -> CommandResult:
    return CommandResult(0, "\n".join(sorted(model_ids)) + "\n")


def test_models_flags_ids_the_catalog_does_not_expose(tmp_path: Path):
    templates = tmp_path / "templates"
    templates.mkdir()
    (templates / "agent.md.j2").write_text("---\nmodel: opencode-go/ghost-model\n---\n")
    catalog = _catalog_with(*(data["phase_model"] for data in SDD_PHASE_MAP.values()))

    result = doctor.check_models(tmp_path, FakeRunner([(("opencode", "models"), catalog)]), templates_dir=templates)

    assert result.failed
    assert any("ghost-model" in finding.message for finding in result.findings)


def test_models_accepts_verified_ids(tmp_path: Path):
    templates = tmp_path / "templates"
    templates.mkdir()
    (templates / "agent.md.j2").write_text("---\nmodel: opencode-go/deepseek-v4-pro\n---\n")
    catalog = _catalog_with("opencode-go/deepseek-v4-pro", *(data["phase_model"] for data in SDD_PHASE_MAP.values()))

    result = doctor.check_models(tmp_path, FakeRunner([(("opencode", "models"), catalog)]), templates_dir=templates)

    assert not result.failed


def test_models_degrades_when_catalog_is_unavailable(tmp_path: Path):
    templates = tmp_path / "templates"
    templates.mkdir()
    (templates / "agent.md.j2").write_text("---\nmodel: opencode-go/deepseek-v4-pro\n---\n")

    result = doctor.check_models(
        tmp_path, FakeRunner([(("opencode", "models"), CommandResult(127, "", "not found"))]), templates_dir=templates
    )

    assert not result.failed
    assert result.findings[0].level == "skip"


# ── mcp ───────────────────────────────────────────────────────────────


def _write_config(path: Path, servers: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcp": {"servers": servers}}), encoding="utf-8")


def test_mcp_flags_project_override_of_global_server(tmp_path: Path):
    _write_config(
        tmp_path / "opencode.json",
        {"github": {"type": "local", "command": ["npx", "-y", "github-mcp@1.0.0"]}},
    )
    global_config = tmp_path / "global.json"
    _write_config(global_config, {"github": {"type": "local", "command": ["/usr/bin/github-mcp"]}})

    result = doctor.check_mcp(
        tmp_path,
        FakeRunner([(("opencode", "mcp", "list"), CommandResult(0, "✓ github connected\n"))]),
        global_config=global_config,
    )

    assert result.failed
    assert any("overrides the global" in finding.message for finding in result.findings)


def test_mcp_flags_unpinned_latest_packages(tmp_path: Path):
    _write_config(
        tmp_path / "opencode.json",
        {"sequential-thinking": {"type": "local", "command": ["npx", "-y", "@modelcontextprotocol/server@latest"]}},
    )

    result = doctor.check_mcp(
        tmp_path,
        FakeRunner([(("opencode", "mcp", "list"), CommandResult(0, "✓ sequential-thinking connected\n"))]),
        global_config=tmp_path / "missing.json",
    )

    assert result.failed
    assert any("@latest" in finding.message for finding in result.findings)


def test_mcp_flags_project_servers_that_are_not_connected(tmp_path: Path):
    _write_config(tmp_path / "opencode.json", {"github": {"type": "local", "command": ["/usr/bin/gh-mcp"]}})

    result = doctor.check_mcp(
        tmp_path,
        FakeRunner([(("opencode", "mcp", "list"), CommandResult(0, "⚠ github needs authentication\n"))]),
        global_config=tmp_path / "missing.json",
    )

    assert result.failed
    assert any("not connected" in finding.message for finding in result.findings)


def test_mcp_clean_inventory_passes(tmp_path: Path):
    _write_config(
        tmp_path / "opencode.json",
        {"sequential-thinking": {"type": "local", "command": ["npx", "-y", "server@2.1.1"]}},
    )

    result = doctor.check_mcp(
        tmp_path,
        FakeRunner([(("opencode", "mcp", "list"), CommandResult(0, "✓ sequential-thinking connected\n"))]),
        global_config=tmp_path / "missing.json",
    )

    assert not result.failed


# ── convergence ───────────────────────────────────────────────────────


def test_convergence_flags_stale_board_project_reference(tmp_path: Path):
    agents = tmp_path / ".opencode" / "agents"
    agents.mkdir(parents=True)
    (agents / "n3rv.md").write_text("board project #2 and projectV2(number:2)\n", encoding="utf-8")

    result = doctor.check_convergence(tmp_path, templates_dir=tmp_path / "templates")

    assert result.failed
    assert any("project #2" in finding.message for finding in result.findings)


def test_convergence_flags_unknown_board_ids(tmp_path: Path):
    agents = tmp_path / ".opencode" / "agents"
    agents.mkdir(parents=True)
    (agents / "n3rv.md").write_text("field PVTSSF_staleId\n", encoding="utf-8")

    result = doctor.check_convergence(tmp_path, templates_dir=tmp_path / "templates")

    assert result.failed
    assert any("stale board id" in finding.message for finding in result.findings)


def test_convergence_flags_template_revision_behind_output(tmp_path: Path):
    templates = tmp_path / "templates"
    templates.mkdir()
    template = templates / "agent.md.j2"
    template.write_text("new revision\n", encoding="utf-8")

    (tmp_path / ".opencode").mkdir()
    lock = tmp_path / ".n3rv" / "template-lock.json"
    lock.parent.mkdir()
    lock.write_text(
        json.dumps(
            {
                "entries": {
                    ".opencode/agents/n3rv.md": {
                        "sha256": "x",
                        "template": "agent.md.j2",
                        "template_sha256": "recorded-with-an-older-template",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    result = doctor.check_convergence(tmp_path, templates_dir=templates)

    assert result.failed
    assert any("older revision" in finding.message for finding in result.findings)


def test_convergence_clean_when_template_revision_matches(tmp_path: Path):
    templates = tmp_path / "templates"
    templates.mkdir()
    template = templates / "agent.md.j2"
    template.write_text("revision\n", encoding="utf-8")

    agents = tmp_path / ".opencode" / "agents"
    agents.mkdir(parents=True)
    (agents / "n3rv.md").write_text("The-Replacement Roadmap (project #1), id PVT_kwDOE1iio84BrhYz\n", encoding="utf-8")

    lock = tmp_path / ".n3rv" / "template-lock.json"
    lock.parent.mkdir()
    lock.write_text(
        json.dumps(
            {
                "entries": {
                    ".opencode/agents/n3rv.md": {
                        "sha256": "x",
                        "template": "agent.md.j2",
                        "template_sha256": hashlib.sha256(template.read_bytes()).hexdigest(),
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    result = doctor.check_convergence(tmp_path, templates_dir=templates)

    assert not result.failed


# ── hooks ─────────────────────────────────────────────────────────────


def test_hooks_flags_a_receive_hook_that_fetches(tmp_path: Path):
    hooks = tmp_path / ".githooks"
    hooks.mkdir()
    (hooks / "post-receive").write_text("#!/bin/sh\ngit fetch origin --prune\n", encoding="utf-8")

    result = doctor.check_hooks(tmp_path)

    assert result.failed
    assert any("remote-tracking refs" in finding.message for finding in result.findings)


def test_hooks_accepts_radio_style_receive_hook(tmp_path: Path):
    """radio's post-receive mentions fetch/push in comments but never runs them."""
    hooks = tmp_path / ".githooks"
    hooks.mkdir()
    (hooks / "post-receive").write_text(
        "#!/bin/sh\n"
        "# Remote-tracking refs must come from GitHub fetches, never from a deploy push.\n"
        "OLD=$1\n"
        "git rev-parse --git-dir >/dev/null\n"
        'git diff --name-only "$OLD" HEAD\n',
        encoding="utf-8",
    )

    result = doctor.check_hooks(tmp_path)

    assert not result.failed
    assert any(finding.level == "ok" for finding in result.findings)


def test_hooks_skips_when_none_installed(tmp_path: Path):
    result = doctor.check_hooks(tmp_path)

    assert not result.failed
    assert result.findings[0].level == "skip"


# ── orchestration ─────────────────────────────────────────────────────


def test_run_doctor_ors_category_exit_codes(tmp_path: Path, capsys):
    root = _git_root(tmp_path)
    templates = tmp_path / "templates"
    templates.mkdir()
    (templates / "agent.md.j2").write_text("---\nmodel: opencode-go/ghost\n---\n", encoding="utf-8")
    catalog = _catalog_with(*(data["phase_model"] for data in SDD_PHASE_MAP.values()))
    runner = FakeRunner(
        [
            (("git", "rev-parse", "HEAD"), CommandResult(0, "aaa\n")),
            (("git", "rev-parse", "--abbrev-ref", "HEAD"), CommandResult(0, "main\n")),
            (("git", "remote", "get-url", "origin"), CommandResult(0, "https://github.com/o/r.git\n")),
            (("gh", "api", "repos/o/r", "--jq"), CommandResult(0, "main\n")),
            (("gh", "api", "repos/o/r/commits/main", "--jq"), CommandResult(0, "bbb\n")),
            (("git", "remote", "get-url", "mainframe"), CommandResult(1, "", "")),
            (("opencode", "models"), catalog),
            (("opencode", "mcp", "list"), CommandResult(0, "")),
        ]
    )

    exit_code = doctor.run_doctor(
        root,
        templates_dir=templates,
        global_config=tmp_path / "missing.json",
        runner=runner,
        json_output=True,
    )

    assert exit_code == doctor.CATEGORY_CODES["drift"] | doctor.CATEGORY_CODES["models"]
    report = json.loads(capsys.readouterr().out)
    assert report["exit_code"] == exit_code
    assert report["ok"] is False


def test_run_doctor_clean_tree_exits_zero(tmp_path: Path, capsys):
    root = _git_root(tmp_path)
    templates = tmp_path / "templates"
    templates.mkdir()
    catalog = _catalog_with(*(data["phase_model"] for data in SDD_PHASE_MAP.values()))
    runner = FakeRunner(
        [
            (("git", "rev-parse", "HEAD"), CommandResult(0, "aaa\n")),
            (("git", "rev-parse", "--abbrev-ref", "HEAD"), CommandResult(0, "main\n")),
            (("git", "remote", "get-url", "origin"), CommandResult(0, "https://github.com/o/r.git\n")),
            (("gh", "api", "repos/o/r", "--jq"), CommandResult(0, "main\n")),
            (("gh", "api", "repos/o/r/commits/main", "--jq"), CommandResult(0, "aaa\n")),
            (("git", "remote", "get-url", "mainframe"), CommandResult(1, "", "")),
            (("opencode", "models"), catalog),
            (("opencode", "mcp", "list"), CommandResult(0, "")),
        ]
    )

    exit_code = doctor.run_doctor(
        root,
        templates_dir=templates,
        global_config=tmp_path / "missing.json",
        runner=runner,
        json_output=True,
    )

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_run_doctor_strict_fails_when_mainframe_is_unverified(tmp_path: Path, capsys):
    """A missing mainframe remote must not look clean when strict is requested."""
    root = _git_root(tmp_path)
    templates = tmp_path / "templates"
    templates.mkdir()
    catalog = _catalog_with(*(data["phase_model"] for data in SDD_PHASE_MAP.values()))
    runner = FakeRunner(
        [
            (("git", "rev-parse", "HEAD"), CommandResult(0, "aaa\n")),
            (("git", "rev-parse", "--abbrev-ref", "HEAD"), CommandResult(0, "main\n")),
            (("git", "remote", "get-url", "origin"), CommandResult(0, "https://github.com/o/r.git\n")),
            (("gh", "api", "repos/o/r", "--jq"), CommandResult(0, "main\n")),
            (("gh", "api", "repos/o/r/commits/main", "--jq"), CommandResult(0, "aaa\n")),
            (("git", "remote", "get-url", "mainframe"), CommandResult(1, "", "")),
            (("opencode", "models"), catalog),
            (("opencode", "mcp", "list"), CommandResult(0, "")),
        ]
    )

    exit_code = doctor.run_doctor(
        root,
        templates_dir=templates,
        global_config=tmp_path / "missing.json",
        runner=runner,
        json_output=True,
        strict=True,
    )

    assert exit_code & doctor.CATEGORY_CODES["drift"]
    drift = json.loads(capsys.readouterr().out)["categories"]["drift"]
    assert drift["ok"] is False
    unverified = [finding for finding in drift["findings"] if finding["level"] == "fail"]
    assert any("no mainframe remote configured" in finding["message"] for finding in unverified)


def test_run_doctor_strict_leaves_verified_checks_alone(tmp_path: Path, capsys):
    """Strict mode only rewrites skips; an all-verified drift category still passes."""
    root = _git_root(tmp_path)
    runner = FakeRunner(
        [
            (("git", "rev-parse", "HEAD"), CommandResult(0, "aaa\n")),
            (("git", "rev-parse", "--abbrev-ref", "HEAD"), CommandResult(0, "main\n")),
            (("git", "remote", "get-url", "origin"), CommandResult(0, "https://github.com/o/r.git\n")),
            (("gh", "api", "repos/o/r", "--jq"), CommandResult(0, "main\n")),
            (("gh", "api", "repos/o/r/commits/main", "--jq"), CommandResult(0, "aaa\n")),
            (("git", "remote", "get-url", "mainframe"), CommandResult(0, "ssh://box/repo\n")),
            (("git", "ls-remote", "--exit-code", "mainframe", "HEAD"), CommandResult(0, "aaa\tHEAD\n")),
        ]
    )

    result = doctor.check_drift(root, runner).strict()

    assert not result.failed
    assert all(finding.level != "skip" for finding in result.findings)


def test_mcp_ignores_pending_connection_state(tmp_path: Path):
    """`pending` means the CLI has not finished checking, not that a server is down."""
    _write_config(tmp_path / "opencode.json", {"github": {"type": "local", "command": ["/usr/bin/gh-mcp"]}})

    result = doctor.check_mcp(
        tmp_path,
        FakeRunner([(("opencode", "mcp", "list"), CommandResult(0, "✓ github pending\n"))]),
        global_config=tmp_path / "missing.json",
    )

    assert not result.failed
    assert any(finding.level == "skip" for finding in result.findings)


def test_mcp_identical_project_copy_is_not_an_override(tmp_path: Path):
    server = {"type": "local", "command": ["/usr/bin/server"]}
    _write_config(tmp_path / "opencode.json", {"foo": server})
    global_config = tmp_path / "global.json"
    _write_config(global_config, {"foo": server})

    result = doctor.check_mcp(
        tmp_path,
        FakeRunner([(("opencode", "mcp", "list"), CommandResult(0, "✓ foo connected\n"))]),
        global_config=global_config,
    )

    assert not result.failed


def test_mcp_github_always_fails_project_override(tmp_path: Path):
    server = {"type": "local", "command": ["/usr/bin/server"]}
    _write_config(tmp_path / "opencode.json", {"github": server})
    global_config = tmp_path / "global.json"
    _write_config(global_config, {"github": server})

    result = doctor.check_mcp(
        tmp_path,
        FakeRunner([(("opencode", "mcp", "list"), CommandResult(0, "✓ github connected\n"))]),
        global_config=global_config,
    )

    assert result.failed


def test_mcp_non_secured_override_is_a_warning(tmp_path: Path):
    """A generated project copy of a recommended server is not a hard failure."""
    _write_config(tmp_path / "opencode.json", {"sequential-thinking": {"type": "local", "command": ["a"]}})
    global_config = tmp_path / "global.json"
    _write_config(global_config, {"sequential-thinking": {"type": "local", "command": ["b"]}})

    result = doctor.check_mcp(
        tmp_path,
        FakeRunner([(("opencode", "mcp", "list"), CommandResult(0, "✓ sequential-thinking connected\n"))]),
        global_config=global_config,
    )

    assert not result.failed
    assert any(finding.level == "warn" and "overrides" in finding.message for finding in result.findings)


def test_drift_feature_branch_is_a_warning_not_a_failure(tmp_path: Path):
    """A developer's feature branch is not a convergence failure."""
    root = _git_root(tmp_path)
    runner = FakeRunner(
        [
            (("git", "rev-parse", "HEAD"), CommandResult(0, "feature1\n")),
            (("git", "rev-parse", "--abbrev-ref", "HEAD"), CommandResult(0, "feat/x\n")),
            (("git", "remote", "get-url", "origin"), CommandResult(0, "https://github.com/o/r.git\n")),
            (("gh", "api", "repos/o/r", "--jq"), CommandResult(0, "main\n")),
            (("gh", "api", "repos/o/r/commits/main", "--jq"), CommandResult(0, "main123\n")),
            (("git", "remote", "get-url", "mainframe"), CommandResult(1, "", "")),
        ]
    )

    result = doctor.check_drift(root, runner)

    assert not result.failed
    assert any(finding.level == "warn" and "feat/x" in finding.message for finding in result.findings)


def test_run_doctor_strict_passes_when_deployment_target_is_none(tmp_path: Path, capsys):
    """Declared deployment.target: none makes the mainframe leg a declared ok, not an unverified skip."""
    root = _git_root(tmp_path)
    (root / ".n3rv").mkdir()
    (root / ".n3rv/state.yaml").write_text(
        "schema: 2\nproject:\n  name: demo\n  stack: generic\ndeployment:\n  target: none\n"
    )
    templates = tmp_path / "templates"
    templates.mkdir()
    catalog = _catalog_with(*(data["phase_model"] for data in SDD_PHASE_MAP.values()))
    runner = FakeRunner(
        [
            (("git", "rev-parse", "HEAD"), CommandResult(0, "aaa\n")),
            (("git", "rev-parse", "--abbrev-ref", "HEAD"), CommandResult(0, "main\n")),
            (("git", "remote", "get-url", "origin"), CommandResult(0, "https://github.com/o/r.git\n")),
            (("gh", "api", "repos/o/r", "--jq"), CommandResult(0, "main\n")),
            (("gh", "api", "repos/o/r/commits/main", "--jq"), CommandResult(0, "aaa\n")),
            (("git", "remote", "get-url", "mainframe"), CommandResult(1, "", "")),
            (("opencode", "models"), catalog),
            (("opencode", "mcp", "list"), CommandResult(0, "")),
        ]
    )

    exit_code = doctor.run_doctor(
        root,
        templates_dir=templates,
        global_config=tmp_path / "missing.json",
        runner=runner,
        json_output=True,
        strict=True,
    )

    drift = json.loads(capsys.readouterr().out)["categories"]["drift"]
    assert not (exit_code & doctor.CATEGORY_CODES["drift"])
    messages = [finding["message"] for finding in drift["findings"]]
    assert any("deployment.target is none" in m for m in messages)
    assert not any(f["level"] == "fail" and "mainframe" in f["message"] for f in drift["findings"])
