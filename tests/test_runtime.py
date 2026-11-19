"""Tests for the runtime verdict contract (``n3rv.runtime``)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from n3rv import runtime
from n3rv.doctor import CommandResult


def _verdict(**overrides: object) -> str:
    payload: dict[str, object] = {
        "schema": 1,
        "verdict": "OK",
        "source_sha": "aaa111",
        "checks": [{"name": "checkout", "status": "PASS", "detail": "HEAD is aaa111"}],
    }
    payload.update(overrides)
    return json.dumps(payload)


class FakeRunner:
    def __init__(self, result: CommandResult) -> None:
        self.result = result
        self.calls: list[tuple[list[str], Path | None, int]] = []

    def __call__(self, cmd: list[str], cwd: Path | None = None, timeout: int = 30) -> CommandResult:
        self.calls.append((list(cmd), cwd, timeout))
        return self.result


# ── parse ─────────────────────────────────────────────────────────────


def test_parses_a_conforming_verdict():
    verdict = runtime.parse_verdict(_verdict())

    assert verdict.verdict == "OK"
    assert verdict.source_sha == "aaa111"
    assert verdict.schema == runtime.VERDICT_SCHEMA
    assert verdict.checks[0].name == "checkout"
    assert verdict.checks[0].status == "PASS"


def test_parses_mismatch_and_exposes_failing_checks():
    parsed = runtime.parse_verdict(
        _verdict(
            verdict="MISMATCH",
            checks=[
                {"name": "image:mario", "status": "PASS", "detail": "id matches"},
                {"name": "label:mario", "status": "FAIL", "detail": "revision is 9f9f9f"},
                {"name": "container:cam", "status": "NOTE", "detail": "nothing running"},
            ],
        )
    )

    assert parsed.verdict == "MISMATCH"
    assert [check.name for check in parsed.failing] == ["label:mario"]


def test_parses_unverified_without_checks():
    parsed = runtime.parse_verdict(_verdict(verdict="UNVERIFIED", source_sha=None, checks=[]))

    assert parsed.verdict == "UNVERIFIED"
    assert parsed.checks == ()
    assert parsed.source_sha is None


def test_source_sha_is_optional():
    payload = {"schema": 1, "verdict": "OK", "checks": []}

    assert runtime.parse_verdict(json.dumps(payload)).source_sha is None


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [
        ("", "no output"),
        ("   \n", "no output"),
        ("not json at all", "not JSON"),
        ('["a"]', "not an object"),
        (json.dumps({"schema": 2, "verdict": "OK"}), "schema 2"),
        (json.dumps({"verdict": "OK"}), "schema None"),
        (json.dumps({"schema": 1, "verdict": "MAYBE"}), "verdict 'MAYBE'"),
        (json.dumps({"schema": 1, "verdict": "OK", "checks": "nope"}), "checks that are not a list"),
        (json.dumps({"schema": 1, "verdict": "OK", "checks": [1]}), "check that is not an object"),
        (json.dumps({"schema": 1, "verdict": "OK", "checks": [{"status": "PASS"}]}), "without a name"),
        (
            json.dumps({"schema": 1, "verdict": "OK", "checks": [{"name": "x", "status": "MAYBE"}]}),
            "status 'MAYBE'",
        ),
        (json.dumps({"schema": 1, "verdict": "OK", "source_sha": 5}), "not a string"),
    ],
)
def test_rejects_anything_outside_the_contract(stdout: str, expected: str):
    with pytest.raises(runtime.VerdictError, match=expected.replace("'", "'")):
        runtime.parse_verdict(stdout)


def test_rejects_oversized_output_rather_than_trusting_a_prefix():
    payload = _verdict(checks=[{"name": "x", "status": "PASS", "detail": "y" * runtime.MAX_OUTPUT_BYTES}])

    with pytest.raises(runtime.VerdictError, match="more than the"):
        runtime.parse_verdict(payload)


# ── run ───────────────────────────────────────────────────────────────


def test_runs_the_declared_argv_unchanged_with_its_timeout(tmp_path: Path):
    command = ["ssh", "-o", "BatchMode=yes", "box", "cd /srv/app && python3 scripts/verify.py --json"]
    runner = FakeRunner(CommandResult(0, _verdict()))

    verdict = runtime.run_verdict(command, runner, cwd=tmp_path, timeout_s=45)

    assert verdict.verdict == "OK"
    assert runner.calls == [(command, tmp_path, 45)]


def test_timeout_is_a_verdict_error_not_an_answer():
    runner = FakeRunner(CommandResult(124, "", "ssh timed out after 45s"))

    with pytest.raises(runtime.VerdictError, match="timed out after 45s"):
        runtime.run_verdict(["ssh", "box", "verify"], runner, timeout_s=45)


def test_missing_executable_names_it():
    runner = FakeRunner(CommandResult(127, "", "ssh not found"))

    with pytest.raises(runtime.VerdictError, match="cannot execute ssh"):
        runtime.run_verdict(["ssh", "box", "verify"], runner)


def test_exit_code_is_advisory_when_the_verdict_parses():
    """A verifier that exits 2 because Docker was unavailable still prints UNVERIFIED."""
    runner = FakeRunner(CommandResult(2, _verdict(verdict="UNVERIFIED", checks=[]), "docker unavailable"))

    assert runtime.run_verdict(["verify"], runner).verdict == "UNVERIFIED"


def test_nonzero_exit_with_unparseable_output_reports_both():
    runner = FakeRunner(CommandResult(255, "", "ssh: Could not resolve hostname box"))

    with pytest.raises(runtime.VerdictError, match="no output.*exit 255.*Could not resolve"):
        runtime.run_verdict(["ssh", "box", "verify"], runner)


def test_empty_command_is_rejected():
    with pytest.raises(runtime.VerdictError, match="empty"):
        runtime.run_verdict([], FakeRunner(CommandResult(0, _verdict())))


@pytest.mark.parametrize(
    "command",
    [
        ["sh", "-c", "docker compose -f app.yml up -d"],
        ["docker", "compose", "build", "mario"],
        ["git", "push", "origin", "main"],
        ["git", "reset", "--hard"],
        ["systemctl", "restart", "n3rv-hub"],
        ["rm", "-rf", "/tmp/x"],
        ["n3rv", "apply"],
    ],
)
def test_a_verifier_that_could_mutate_the_host_is_refused(command: list[str]):
    runner = FakeRunner(CommandResult(0, _verdict()))

    with pytest.raises(runtime.VerdictError, match="mutating verify_command"):
        runtime.run_verdict(command, runner)

    assert runner.calls == []  # refused before anything ran


def test_read_only_verifier_shapes_are_allowed():
    for command in (
        ["ssh", "-o", "BatchMode=yes", "box", "cd /srv && python3 scripts/deploy_manifest.py verify --json"],
        ["git", "ls-remote", "--exit-code", "box", "HEAD"],
        ["docker", "inspect", "--format", "{{.Image}}", "radio-mario"],
        ["sysctl", "-n", "vm.swappiness"],
    ):
        assert runtime.run_verdict(command, FakeRunner(CommandResult(0, _verdict()))).verdict == "OK"


def test_context_shape_is_json_ready():
    context = runtime.to_context(runtime.parse_verdict(_verdict()))

    assert context == {
        "verdict": "OK",
        "source_sha": "aaa111",
        "checks": [{"name": "checkout", "status": "PASS", "detail": "HEAD is aaa111"}],
    }
    json.dumps(context)
