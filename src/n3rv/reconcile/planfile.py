"""Plan file loading and precondition validation (n3rv#19)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class PlanFileError(ValueError):
    """Malformed, unreadable, or missing plan file (usage)."""


class PlanRejectedError(ValueError):
    """Precondition mismatch; carries the first divergence."""

    def __init__(self, field: str, detail: str) -> None:
        self.field = field
        self.detail = detail
        super().__init__(f"{field}: {detail}")


def load_plan_file(path: Path) -> tuple[Any, str]:
    """Read and parse a plan file. Returns (plan, recorded_plan_hash)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PlanFileError(f"could not read plan file {path}: {exc}") from exc
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PlanFileError(f"could not parse plan file {path}: {exc}") from exc
    if not isinstance(obj, dict):
        raise PlanFileError(f"plan file {path}: expected a JSON object")
    from n3rv.reconcile.plan import Plan

    plan = Plan.from_dict(obj)
    return plan, str(obj.get("plan_hash", ""))


def validate_plan_file(
    saved: Any,
    *,
    root: Path,
    state: Any,
    templates_dir: Path,
    n3rv_version: str,
) -> None:
    """Sequential precondition gate; raises PlanRejectedError on first divergence."""
    from n3rv.reconcile.plan import file_sha, templates_sha

    if state.sha256() != saved.state_sha:
        raise PlanRejectedError("state_sha", f"expected {saved.state_sha}, got {state.sha256()}")
    current_templates_sha = templates_sha(templates_dir)
    if current_templates_sha != saved.templates_sha:
        raise PlanRejectedError("templates_sha", f"expected {saved.templates_sha}, got {current_templates_sha}")
    if n3rv_version != saved.n3rv_version:
        raise PlanRejectedError("n3rv_version", f"expected {saved.n3rv_version}, got {n3rv_version}")
    for action in saved.actions:
        if action.current_sha is None:
            continue
        target = root / action.target
        if not target.exists():
            raise PlanRejectedError(action.target, "file is missing")
        current = file_sha(target)
        if current != action.current_sha:
            raise PlanRejectedError(action.target, f"expected {action.current_sha}, got {current}")
