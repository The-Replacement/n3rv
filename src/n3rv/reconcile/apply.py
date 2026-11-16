"""Applying a plan (n3rv#9).

Effects live here and nowhere else: ``plan`` decides, this writes. Two rules
that are not negotiable:

* CONFLICT and INERT actions are never written. A plan that cannot decide must
  not act.
* A lockfile entry is recorded only for an action that landed, so the lockfile
  can never describe a write that did not happen.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from n3rv.init.lockfile import load_lockfile, record_entry, save_lockfile, save_managed_state
from n3rv.init.update import _write_rendered
from n3rv.reconcile.plan import ActionKind, Plan, resolve_actions


@dataclass(frozen=True)
class AppliedAction:
    kind: str
    target: str
    result: str


@dataclass
class ApplyResult:
    plan_hash: str = ""
    applied: list[AppliedAction] = field(default_factory=list)
    skipped: list[AppliedAction] = field(default_factory=list)
    error: str | None = None
    lockfile_entries: int = 0

    @property
    def ok(self) -> bool:
        return self.error is None


def apply_plan(
    root: Path,
    state: Any,
    *,
    templates_dir: Path,
    n3rv_version: str,
    n3rv_binary: str,
    force_project_files: bool = False,
    global_config: Path | None = None,
) -> ApplyResult:
    """Recompute the plan and apply it.

    The plan is recomputed rather than read from disk: the state is the
    authority, and a stale plan file could describe actions that no longer
    match reality. Reviewing before applying is the caller's job.
    """
    plan, resolutions = resolve_actions(
        root,
        state,
        templates_dir=templates_dir,
        n3rv_version=n3rv_version,
        n3rv_binary=n3rv_binary,
        force_project_files=force_project_files,
        global_config=global_config,
    )
    return apply_resolved_plan(root, state, plan, resolutions, templates_dir=templates_dir)


def apply_resolved_plan(
    root: Path,
    state: Any,
    plan: Plan,
    resolutions: dict[str, Any],
    templates_dir: Path | None = None,
) -> ApplyResult:
    """Execute an already-resolved plan. Separated so tests can drive it directly.

    ``templates_dir`` records which template revision each write came from in
    the lockfile entry; without it the entry is untraceable (n3rv#17).
    """
    result = ApplyResult(plan_hash=plan.plan_hash)

    # An invalid state cannot be applied, not even partially. Without this,
    # a typo in components turns into "delete everything that was declared
    # before" - which is exactly what an unknown component produces.
    blocking = [f for f in plan.findings if f.code in {"unknown_component", "template_missing"}]
    if blocking:
        result.error = "the declared state is invalid: " + "; ".join(f.message for f in blocking)
        return result

    entries: dict[str, Any] = dict(load_lockfile(root).get("entries") or {})

    for action in plan.actions:
        if action.kind in {ActionKind.CONFLICT, ActionKind.INERT}:
            result.skipped.append(AppliedAction(action.kind.value, action.target, "requires a decision"))
            continue

        target = root / action.target
        if action.kind == ActionKind.DELETE:
            entries.pop(action.target, None)
            try:
                target.unlink()
            except OSError as exc:
                result.error = f"could not delete {action.target}: {exc}"
                _persist(root, state, entries, _collect_owned_json_keys(root, result, resolutions))
                result.lockfile_entries = len(entries)
                return result
            result.applied.append(AppliedAction(action.kind.value, action.target, "deleted"))
            continue

        resolution = resolutions.get(action.target)
        if resolution is None or resolution.text is None:
            result.skipped.append(AppliedAction(action.kind.value, action.target, "nothing to write"))
            continue

        try:
            _write_rendered(target, resolution.text)
        except OSError as exc:
            result.error = f"could not write {action.target}: {exc}"
            _persist(root, state, entries, _collect_owned_json_keys(root, result, resolutions))
            result.lockfile_entries = len(entries)
            return result

        template_path: Path | None = None
        if templates_dir is not None and action.source:
            template_path = templates_dir / action.source
        entries[action.target] = record_entry(
            target,
            action.source or "",
            context_hash=state.sha256(),
            template_path=template_path,
        )
        result.applied.append(AppliedAction(action.kind.value, action.target, "written"))

    _persist(root, state, entries, _collect_owned_json_keys(root, result, resolutions))
    result.lockfile_entries = len(entries)
    return result


def _collect_owned_json_keys(
    root: Path, result: ApplyResult, resolutions: dict[str, Any]
) -> dict[str, dict[str, list[str]]]:
    """Accumulate json_keys for written JSON_MERGE outputs, preserving existing records."""
    from n3rv.init.lockfile import load_managed_state
    from n3rv.init.update import FILE_UPDATE_MANIFEST, UpdateStrategy

    json_outputs = {e.output_path for e in FILE_UPDATE_MANIFEST if e.strategy == UpdateStrategy.JSON_MERGE}
    owned = dict(load_managed_state(root).get("json_keys") or {})
    for applied in result.applied:
        if applied.target in json_outputs and applied.result == "written":
            resolution = resolutions.get(applied.target)
            if resolution is not None and getattr(resolution, "owned_keys", None) is not None:
                owned[applied.target] = resolution.owned_keys
    return owned


def _persist(
    root: Path,
    state: Any,
    entries: dict[str, Any],
    owned_json_keys: dict[str, dict[str, list[str]]] | None = None,
) -> None:
    """Save what actually landed. Never called with entries for failed writes."""
    save_lockfile(root, entries)
    managed: dict[str, Any] = {"mcp_servers": sorted(server.name for server in state.mcp.servers)}
    if owned_json_keys:
        managed["json_keys"] = owned_json_keys
    save_managed_state(root, managed)


def file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
