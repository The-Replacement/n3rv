"""Tests for the board CLI adapter."""

from __future__ import annotations

import json
from unittest.mock import patch

from typer.testing import CliRunner

from n3rv.board import BoardError
from n3rv.cli import app

runner = CliRunner()


def test_board_sync_emits_machine_readable_json():
    payload = {
        "ok": True,
        "applied": True,
        "changed": ["status", "phase"],
        "item_id": "I_1",
    }
    with patch("n3rv.cli_board.sync_issue", return_value=payload) as mocked:
        result = runner.invoke(
            app,
            ["board", "sync", "--repo", "The-Replacement/n3rv", "--issue", "5", "--phase", "spec"],
        )

    assert result.exit_code == 0
    assert json.loads(result.output) == payload
    assert mocked.call_args.kwargs["phase"] == "spec"
    assert mocked.call_args.kwargs["issue"] == 5


def test_board_sync_propagates_distinct_exit_codes():
    with patch("n3rv.cli_board.sync_issue", side_effect=BoardError("issue #5 is not on project #1", code=3)):
        result = runner.invoke(
            app,
            ["board", "sync", "--repo", "The-Replacement/n3rv", "--issue", "5", "--phase", "spec"],
        )

    assert result.exit_code == 3
    body = json.loads(result.output)
    assert body["ok"] is False
    assert body["code"] == 3


def test_board_sync_rejects_unknown_phase():
    with patch(
        "n3rv.cli_board.sync_issue",
        side_effect=BoardError("unknown SDD phase 'deploy'", code=4),
    ):
        result = runner.invoke(
            app, ["board", "sync", "--repo", "The-Replacement/n3rv", "--issue", "5", "--phase", "deploy"]
        )

    assert result.exit_code == 4


def test_board_sync_requires_repo_outside_a_git_remote(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["board", "sync", "--issue", "5", "--phase", "spec"])

    assert result.exit_code == 4
    assert json.loads(result.output)["ok"] is False


def test_doctor_command_is_registered():
    result = runner.invoke(app, ["doctor", "--help"])

    assert result.exit_code == 0
    assert "convergence" in result.output.lower()


def test_doctor_json_and_exit_code_passthrough(tmp_path):
    with patch("n3rv.cli.run_doctor", return_value=2) as mocked:
        result = runner.invoke(app, ["doctor", "--json", "--root", str(tmp_path)])

    assert result.exit_code == 2
    assert mocked.call_args.args[0] == tmp_path
    assert mocked.call_args.kwargs["json_output"] is True
