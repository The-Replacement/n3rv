"""Pure planning for the reconciler (n3rv#9).

``build_plan`` renders the declared state, compares it with the tree and the
lockfile, and returns an ordered action list. It reads; it never writes and it
never touches the network - that is what makes "show me what you would do" a
real answer instead of a promise.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from n3rv.init import DECLARABLE_FAMILIES, family_for
from n3rv.init.lockfile import load_lockfile
from n3rv.init.renderer import TemplateEngine
from n3rv.init.update import (
    FILE_UPDATE_MANIFEST,
    UpdateEntry,
    _project_owned_modified,
    entry_render_context,
    resolve_text,
)

#: Exit bits for ``n3rv plan``. Deliberately independent from doctor's category
#: codes: a plan that finds drift and a plan that cannot decide are different
#: failures and must not share a number.
PLAN_CODES: dict[str, int] = {
    "drift": 1,
    "conflict": 2,
    "divergence": 4,
    "inert": 8,
    "usage": 64,
}


class ActionKind(StrEnum):
    CREATE = "create"
    UPDATE = "update"
    REGENERATE = "regenerate"
    DELETE = "delete"
    CONFLICT = "conflict"
    INERT = "inert"


@dataclass(frozen=True)
class Action:
    """One step. ``desired_sha`` is display-only; it never enters plan_hash."""

    kind: ActionKind
    target: str
    strategy: str
    reason: str = ""
    source: str | None = None
    current_sha: str | None = None
    desired_sha: str | None = None


@dataclass(frozen=True)
class Finding:
    code: str
    level: str
    message: str


@dataclass(frozen=True)
class Plan:
    state_sha: str
    n3rv_version: str
    templates_sha: str
    env_hash: str
    actions: tuple[Action, ...] = ()
    findings: tuple[Finding, ...] = ()

    @property
    def empty(self) -> bool:
        return not self.actions

    @property
    def plan_hash(self) -> str:
        """Identity of the decisions, not of the bytes.

        Absolute paths and resolved binaries are environment, so two machines
        checking out the same state reach the same plan_hash while rendering
        different bytes. ``env_hash`` carries that difference.
        """
        payload = {
            "state": self.state_sha,
            "n3rv": self.n3rv_version,
            "templates": self.templates_sha,
            "actions": [
                {"kind": a.kind.value, "target": a.target, "strategy": a.strategy, "reason": a.reason}
                for a in sorted(self.actions, key=lambda a: (a.target, a.kind.value))
            ],
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()

    @property
    def exit_code(self) -> int:
        code = 0
        if any(
            a.kind in {ActionKind.CREATE, ActionKind.UPDATE, ActionKind.REGENERATE, ActionKind.DELETE}
            for a in self.actions
        ):
            code |= PLAN_CODES["drift"]
        if any(a.kind == ActionKind.CONFLICT for a in self.actions):
            code |= PLAN_CODES["conflict"]
        if any(a.kind == ActionKind.INERT for a in self.actions):
            code |= PLAN_CODES["inert"]
        for finding in self.findings:
            if finding.code == "divergence" and finding.level != "ok":
                code |= PLAN_CODES["divergence"]
            if finding.code in {"unknown_component", "template_missing"}:
                code |= PLAN_CODES["usage"]
        return code

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_hash": self.plan_hash,
            "env_hash": self.env_hash,
            "state_sha": self.state_sha,
            "n3rv_version": self.n3rv_version,
            "templates_sha": self.templates_sha,
            "exit_code": self.exit_code,
            "empty": self.empty,
            "actions": [
                {
                    "kind": a.kind.value,
                    "target": a.target,
                    "strategy": a.strategy,
                    "reason": a.reason,
                    "source": a.source,
                    "current_sha": a.current_sha,
                    "desired_sha": a.desired_sha,
                }
                for a in self.actions
            ],
            "findings": [{"code": f.code, "level": f.level, "message": f.message} for f in self.findings],
        }


def templates_sha(templates_dir: Path) -> str:
    """Stable digest of the template tree, so a template edit changes the plan."""
    digest = hashlib.sha256()
    for path in sorted(templates_dir.rglob("*.j2")):
        digest.update(str(path.relative_to(templates_dir)).encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def env_hash(project_root: Path, n3rv_binary: str) -> str:
    payload = {"root": str(project_root), "binary": n3rv_binary}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def selected_entries(state: Any) -> tuple[list[UpdateEntry], list[Finding]]:
    """Entries the declared state asks for, plus any component it names in vain.

    Validation uses the union of both manifests (state may legitimately declare
    an init-only component); selection uses the update manifest, which is what
    a reconciler can actually converge.
    """
    from n3rv.reconcile.state import manifest_components

    findings: list[Finding] = []
    known = manifest_components()
    declared = {
        "agents": state.components.agents,
        "skills": state.components.skills,
        "commands": state.components.commands,
        "hooks": state.components.hooks,
    }
    wanted: set[str] = set()
    for family, names in declared.items():
        for name in names:
            output = known[family].get(name)
            if output is None:
                findings.append(
                    Finding(
                        "unknown_component",
                        "fail",
                        f"state declares {_singular(family)} {name!r}, which the templates do not ship",
                    )
                )
                continue
            wanted.add(output)

    selected: list[UpdateEntry] = []
    for entry in FILE_UPDATE_MANIFEST:
        family = family_for(entry.output_path)
        if family in DECLARABLE_FAMILIES:
            if entry.output_path in wanted:
                selected.append(entry)
        else:
            selected.append(entry)
    return selected, findings


def _name(family: str, output_path: str) -> str:
    if family == "skills":
        parts = output_path.split("/")
        return parts[-2] if len(parts) >= 2 else output_path
    return Path(output_path).stem


def _singular(family: str) -> str:
    return family[:-1] if family.endswith("s") else family


def build_plan(
    root: Path,
    state: Any,
    *,
    templates_dir: Path,
    n3rv_version: str,
    n3rv_binary: str,
    force_project_files: bool = False,
) -> Plan:
    """Compute the action list. Pure with respect to the filesystem."""
    return resolve_actions(
        root,
        state,
        templates_dir=templates_dir,
        n3rv_version=n3rv_version,
        n3rv_binary=n3rv_binary,
        force_project_files=force_project_files,
    )[0]


def resolve_actions(
    root: Path,
    state: Any,
    *,
    templates_dir: Path,
    n3rv_version: str,
    n3rv_binary: str,
    force_project_files: bool = False,
) -> tuple[Plan, dict[str, Any]]:
    """Build the plan and keep the resolved text for every target.

    ``apply`` needs the exact bytes it is about to write; keeping them here
    means the plan and the write share one resolution instead of rendering
    twice and hoping the two passes agree.
    """
    from n3rv.reconcile.state import render_context

    ctx = render_context(state, project_root=root, n3rv_binary=n3rv_binary, n3rv_version=n3rv_version)
    engine = TemplateEngine(templates_dir)
    lock_entries = load_lockfile(root).get("entries") or {}
    managed_mcp = {server.name for server in state.mcp.servers}

    entries, findings = selected_entries(state)
    actions: list[Action] = []
    resolutions: dict[str, Any] = {}

    for entry in entries:
        target = root / entry.output_path
        try:
            rendered = engine.render(entry.template_name, entry_render_context(entry, ctx))
        except Exception as exc:  # a broken template is a generator bug, not drift
            findings.append(Finding("template_missing", "fail", f"{entry.template_name}: {exc}"))
            continue

        resolution = resolve_text(entry, root, target, rendered, recommended_mcp=managed_mcp)
        if resolution.error is not None:
            findings.append(Finding("template_missing", "fail", f"{entry.output_path}: {resolution.error}"))
            continue
        if resolution.text is None:
            if resolution.reason and resolution.reason != "already exists":
                findings.append(Finding("skipped", "warn", f"{entry.output_path}: {resolution.reason}"))
            continue

        desired = resolution.text
        desired_sha = hashlib.sha256(desired.encode("utf-8")).hexdigest()
        resolutions[entry.output_path] = resolution
        if not target.exists():
            actions.append(
                Action(
                    ActionKind.CREATE,
                    entry.output_path,
                    entry.strategy.value,
                    "not on disk",
                    entry.template_name,
                    None,
                    desired_sha,
                )
            )
            continue

        current = target.read_text(encoding="utf-8")
        if current == desired:
            continue

        if entry.project_owned and not force_project_files and _project_owned_modified(entry, root, target):
            # update() leaves a modified project-owned file alone. Reporting it
            # as a conflict would make every converged tree non-empty and would
            # claim the generator has an opinion it deliberately does not have.
            findings.append(
                Finding(
                    "project_owned",
                    "warn",
                    f"{entry.output_path}: modified since last generation (use --force-project-files)",
                )
            )
            continue

        tracked = entry.output_path in lock_entries
        actions.append(
            Action(
                ActionKind.UPDATE if tracked else ActionKind.REGENERATE,
                entry.output_path,
                entry.strategy.value,
                "content differs from the declared state" if tracked else "untracked by the lockfile",
                entry.template_name,
                hashlib.sha256(current.encode("utf-8")).hexdigest(),
                desired_sha,
            )
        )

    actions.extend(_deletions(root, entries, lock_entries))

    return (
        Plan(
            state_sha=state.sha256(),
            n3rv_version=n3rv_version,
            templates_sha=templates_sha(templates_dir),
            env_hash=env_hash(root, n3rv_binary),
            actions=tuple(actions),
            findings=tuple(findings),
        ),
        resolutions,
    )


def _deletions(root: Path, entries: list[UpdateEntry], lock_entries: dict[str, Any]) -> list[Action]:
    """Files this generator wrote before and no longer selects.

    Only touched when the file still matches what was written: anything a human
    edited becomes a conflict, never a deletion.
    """
    selected = {entry.output_path for entry in entries}
    removed: list[Action] = []
    for output_path, record in sorted(lock_entries.items()):
        if output_path in selected:
            continue
        try:
            family = family_for(output_path)
        except Exception:
            continue
        if family not in DECLARABLE_FAMILIES:
            continue
        target = root / output_path
        if not target.exists():
            continue
        current_sha = hashlib.sha256(target.read_bytes()).hexdigest()
        recorded = str((record or {}).get("sha256") or "")
        if recorded and recorded == current_sha:
            removed.append(
                Action(ActionKind.DELETE, output_path, "generated", "no longer declared", None, current_sha, None)
            )
        else:
            removed.append(
                Action(
                    ActionKind.CONFLICT,
                    output_path,
                    "generated",
                    "declared removed but edited by hand",
                    None,
                    current_sha,
                    None,
                )
            )
    return removed
