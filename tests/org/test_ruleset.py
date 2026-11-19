"""Tests for the default-branch ruleset installer (infra#3, T9)."""

from __future__ import annotations

import json
import re
import subprocess
from typing import Any
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from n3rv.cli import app
from n3rv.org import (
    DEFAULT_RULESET_NAME,
    RulesetError,
    _parse_owner_repo,
    _ruleset_required_contexts,
    build_ruleset_payload,
    ci_check_contexts,
    install_ruleset,
)

INFRA_CHECKS = ["health-self-test", "shellcheck", "sudoers", "units"]


def _done(stdout: str = "", returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def _repo_json(branch: str = "main") -> str:
    return json.dumps({"default_branch": branch})


def _checks_json(names: list[str]) -> str:
    return json.dumps({"check_runs": [{"name": name} for name in names]})


def _active_ruleset(contexts: list[str], enforcement: str = "active") -> dict[str, Any]:
    payload = build_ruleset_payload(DEFAULT_RULESET_NAME, contexts)
    payload["id"] = 42
    payload["enforcement"] = enforcement
    return payload


class TestParseOwnerRepo:
    def test_url(self) -> None:
        assert _parse_owner_repo("https://github.com/The-Replacement/infra") == "The-Replacement/infra"

    def test_url_with_git_suffix_and_trailing_slash(self) -> None:
        assert _parse_owner_repo("https://github.com/The-Replacement/infra.git/") == "The-Replacement/infra"

    def test_scp_style_remote(self) -> None:
        assert _parse_owner_repo("git@github.com:The-Replacement/n3rv.git") == "The-Replacement/n3rv"

    def test_bare_slug(self) -> None:
        assert _parse_owner_repo("The-Replacement/radio") == "The-Replacement/radio"

    @pytest.mark.parametrize("bad", ["", "infra", "a/b/c", "https://gitlab.com/a/b"])
    def test_rejects_unparseable(self, bad: str) -> None:
        with pytest.raises(RulesetError):
            _parse_owner_repo(bad)


class TestBuildPayload:
    def test_default_branch_conditions_and_no_bypass(self) -> None:
        payload = build_ruleset_payload(DEFAULT_RULESET_NAME, INFRA_CHECKS)
        assert payload["name"] == DEFAULT_RULESET_NAME
        assert payload["target"] == "branch"
        assert payload["enforcement"] == "active"
        assert payload["conditions"] == {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}}
        assert payload["bypass_actors"] == []

    def test_requires_exactly_the_given_contexts(self) -> None:
        payload = build_ruleset_payload(DEFAULT_RULESET_NAME, INFRA_CHECKS)
        rule = next(r for r in payload["rules"] if r["type"] == "required_status_checks")
        contexts = [c["context"] for c in rule["parameters"]["required_status_checks"]]
        assert contexts == INFRA_CHECKS
        assert rule["parameters"]["strict_required_status_checks_policy"] is True

    def test_blocks_deletion_and_force_push(self) -> None:
        types = {rule["type"] for rule in build_ruleset_payload(DEFAULT_RULESET_NAME, INFRA_CHECKS)["rules"]}
        assert {"deletion", "non_fast_forward", "pull_request", "required_status_checks"} <= types


class TestCiCheckContexts:
    def test_reads_reported_check_runs_not_workflow_yaml(self) -> None:
        calls: list[list[str]] = []

        def fake(cmd: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
            calls.append(cmd)
            if cmd[-1].endswith("/check-runs?per_page=100"):
                return _done(_checks_json(["units", "shellcheck", "units"]))
            return _done("{}", 1, "unexpected")

        with patch("subprocess.run", side_effect=fake):
            contexts = ci_check_contexts("The-Replacement/infra", "main")

        assert contexts == ["shellcheck", "units"]  # sorted, deduplicated
        assert calls[0] == [
            "gh",
            "api",
            "repos/The-Replacement/infra/commits/main/check-runs?per_page=100",
        ]

    def test_refuses_when_nothing_reports(self) -> None:
        with patch("subprocess.run", return_value=_done(_checks_json([]))):
            with pytest.raises(RulesetError, match="no check runs"):
                ci_check_contexts("The-Replacement/infra", "main")


class TestRulesetRequiredContexts:
    def test_extracts_contexts(self) -> None:
        assert _ruleset_required_contexts(_active_ruleset(INFRA_CHECKS)) == INFRA_CHECKS

    def test_missing_rule_returns_empty(self) -> None:
        assert _ruleset_required_contexts({"rules": [{"type": "deletion"}]}) == []


class TestInstallRuleset:
    def test_dry_run_writes_nothing(self) -> None:
        calls: list[list[str]] = []

        def fake(cmd: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
            calls.append(cmd)
            if cmd[-1] == "repos/The-Replacement/infra":
                return _done(_repo_json())
            return _done(_checks_json(INFRA_CHECKS))

        with patch("subprocess.run", side_effect=fake), patch("builtins.print") as printed:
            assert install_ruleset("The-Replacement/infra", dry_run=True) is True

        assert len(calls) == 2  # repo + check runs, no ruleset lookup and no write
        assert all("--method" not in cmd for cmd in calls)
        output = " ".join(str(call.args) for call in printed.call_args_list)
        assert "would install" in output

    def test_creates_when_absent(self) -> None:
        payloads: list[Any] = []

        def fake(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            if cmd[-1] == "repos/The-Replacement/infra":
                return _done(_repo_json())
            if cmd[-1].endswith("/check-runs?per_page=100"):
                return _done(_checks_json(INFRA_CHECKS))
            if cmd[2] == "repos/The-Replacement/infra/rulesets" and "--method" not in cmd:
                return _done("[]")
            if cmd[2] == "repos/The-Replacement/infra/rulesets" and "--method" in cmd:
                payloads.append(json.loads(kwargs["input"]))
                return _done('{"id": 7}')
            return _done("{}", 1, f"unexpected {cmd}")

        with patch("subprocess.run", side_effect=fake):
            assert install_ruleset("The-Replacement/infra") is True

        assert len(payloads) == 1
        assert payloads[0]["name"] == DEFAULT_RULESET_NAME

    def test_idempotent_when_already_active_with_same_checks(self) -> None:
        writes = 0

        def fake(cmd: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
            nonlocal writes
            if cmd[-1] == "repos/The-Replacement/infra":
                return _done(_repo_json())
            if cmd[-1].endswith("/check-runs?per_page=100"):
                return _done(_checks_json(INFRA_CHECKS))
            if cmd[-1] == "repos/The-Replacement/infra/rulesets":
                return _done(json.dumps([{"id": 42, "name": DEFAULT_RULESET_NAME}]))
            if cmd[-1] == "repos/The-Replacement/infra/rulesets/42" and "--method" not in cmd:
                return _done(json.dumps(_active_ruleset(INFRA_CHECKS)))
            if "--method" in cmd:
                writes += 1
                return _done("{}")
            return _done("{}", 1, f"unexpected {cmd}")

        with patch("subprocess.run", side_effect=fake), patch("builtins.print") as printed:
            assert install_ruleset("The-Replacement/infra") is True

        assert writes == 0
        assert "already active" in " ".join(str(call.args) for call in printed.call_args_list)

    def test_updates_when_contexts_differ(self) -> None:
        methods: list[str] = []

        def fake(cmd: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
            if cmd[-1] == "repos/The-Replacement/infra":
                return _done(_repo_json())
            if cmd[-1].endswith("/check-runs?per_page=100"):
                return _done(_checks_json(INFRA_CHECKS))
            if cmd[-1] == "repos/The-Replacement/infra/rulesets":
                return _done(json.dumps([{"id": 42, "name": DEFAULT_RULESET_NAME}]))
            if cmd[-1] == "repos/The-Replacement/infra/rulesets/42" and "--method" not in cmd:
                return _done(json.dumps(_active_ruleset(["shellcheck"])))
            if "--method" in cmd:
                methods.append(cmd[cmd.index("--method") + 1])
                return _done("{}")
            return _done("{}", 1, f"unexpected {cmd}")

        with patch("subprocess.run", side_effect=fake):
            assert install_ruleset("The-Replacement/infra") is True

        assert methods == ["PUT"]

    def test_fails_when_no_checks_and_writes_nothing(self) -> None:
        calls: list[list[str]] = []

        def fake(cmd: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
            calls.append(cmd)
            if cmd[-1] == "repos/The-Replacement/infra":
                return _done(_repo_json())
            return _done(_checks_json([]))

        with patch("subprocess.run", side_effect=fake), patch("builtins.print"):
            assert install_ruleset("The-Replacement/infra") is False

        assert all("--method" not in cmd for cmd in calls)

    def test_fails_on_api_error(self) -> None:
        with patch("subprocess.run", return_value=_done("", 1, "HTTP 403")), patch("builtins.print"):
            assert install_ruleset("The-Replacement/infra") is False


class TestCli:
    runner = CliRunner()

    @staticmethod
    def _plain(text: str) -> str:
        """Strip ANSI: rich splits flags as ``-\\x1b[0m\\x1b[1;36m-dry-run``."""
        return re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", text)

    def test_ruleset_in_org_help(self) -> None:
        result = self.runner.invoke(app, ["org", "--help"])
        assert result.exit_code == 0
        assert "ruleset" in self._plain(result.stdout)

    def test_ruleset_help_documents_options(self) -> None:
        result = self.runner.invoke(app, ["org", "ruleset", "--help"])
        assert result.exit_code == 0
        out = self._plain(result.stdout)
        assert "--dry-run" in out
        assert "--name" in out

    def test_exit_code_follows_install_result(self) -> None:
        with patch("n3rv.cli_org.install_ruleset", return_value=False), patch("builtins.print"):
            result = self.runner.invoke(app, ["org", "ruleset", "The-Replacement/infra"])
        assert result.exit_code == 1

        with patch("n3rv.cli_org.install_ruleset", return_value=True), patch("builtins.print"):
            result = self.runner.invoke(app, ["org", "ruleset", "The-Replacement/infra"])
        assert result.exit_code == 0
