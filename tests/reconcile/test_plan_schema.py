"""Plan JSON schema fixture validation (n3rv#19, AC12)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import n3rv
from n3rv.init.binary import resolve_n3rv_binary
from n3rv.init.context import Stack
from n3rv.init.update import run_update
from n3rv.reconcile import state_from_tree
from n3rv.reconcile.plan import build_plan
from n3rv.reconcile.state import ProjectIdentity, ProjectState

TEMPLATES = Path(n3rv.__file__).parent / "init" / "templates"
BINARY = resolve_n3rv_binary() or "n3rv"
FIXTURE = Path(__file__).parent / "fixtures" / "plan_schema.json"


def _type_ok(value, spec: str) -> bool:
    if spec == "str":
        return isinstance(value, str)
    if spec == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    if spec == "bool":
        return isinstance(value, bool)
    if spec == "list":
        return isinstance(value, list)
    if spec == "str_or_null":
        return value is None or isinstance(value, str)
    raise AssertionError(f"unknown spec {spec}")


def validate_shape(data: dict) -> None:
    schema = json.loads(FIXTURE.read_text())
    top = schema["top_level"]
    assert set(data.keys()) == set(top.keys())
    for k, spec in top.items():
        assert _type_ok(data[k], spec), f"{k} type mismatch"
    assert re.fullmatch(schema["plan_hash_pattern"], data["plan_hash"])
    action_keys = schema["action_keys"]
    for action in data["actions"]:
        assert isinstance(action, dict)
        assert set(action.keys()) == set(action_keys)
        for k in ("kind", "target", "strategy", "reason"):
            assert isinstance(action[k], str)
        for k in ("source", "current_sha", "desired_sha"):
            assert action[k] is None or isinstance(action[k], str)
    finding_keys = schema["finding_keys"]
    for finding in data["findings"]:
        assert isinstance(finding, dict)
        assert set(finding.keys()) == set(finding_keys)
        for k in finding_keys:
            assert isinstance(finding[k], str)


def test_plan_json_and_output_validate_shape(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "demo"\nversion = "0.1.0"\n')
    run_update(tmp_path)
    plan = build_plan(tmp_path, state_from_tree(tmp_path), templates_dir=TEMPLATES, n3rv_version="dev", n3rv_binary=BINARY)
    data = plan.to_dict()
    validate_shape(data)
    out = tmp_path / "plan.json"
    out.write_text(json.dumps(data, indent=2) + "\n")
    validate_shape(json.loads(out.read_text()))
