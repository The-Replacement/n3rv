"""Pure planning for the reconciler (n3rv#9).

``build_plan`` renders the declared state, compares it with the tree and the
lockfile, and returns an ordered action list. It reads; it never writes and it
never touches the network - that is what makes "show me what you would do" a
real answer instead of a promise.
"""

from __future__ import annotations

import hashlib
import json
import re
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
    _locked_managed_hash,
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
    "rejected": 32,
    "usage": 64,
}

ENV_CONTEXT_KEYS = ("project_root", "n3rv_binary")
_ENV_PLACEHOLDER = "\x00ENV\x00"
# Equal-length pairs that differ at every byte so the full span is env-derived.
_SENTINEL_A = ("AAAAAAAAAAAAAAAAAAAAAAAA", "CCCCCCCCCCCCCCCCCCCCCCCC")
_SENTINEL_B = ("BBBBBBBBBBBBBBBBBBBBBBBB", "DDDDDDDDDDDDDDDDDDDDDDDD")


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
    force_project_files: bool = False
    global_config: str | None = None

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

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Plan:
        if not isinstance(data, dict):
            from n3rv.reconcile.planfile import PlanFileError

            raise PlanFileError("plan file: expected a JSON object")

        def _require(key: str, types: tuple[type, ...] | type, *, allow_none: bool = False) -> Any:
            from n3rv.reconcile.planfile import PlanFileError

            if key not in data:
                raise PlanFileError(f"missing required key: {key}")
            val = data[key]
            if allow_none and val is None:
                return val
            if not isinstance(val, types):
                raise PlanFileError(f"wrong type for key: {key}")
            return val

        state_sha = _require("state_sha", str)
        n3rv_version = _require("n3rv_version", str)
        templates_sha_v = _require("templates_sha", str)
        env_hash_v = _require("env_hash", str)
        _require("exit_code", int)
        _require("empty", bool)
        force_project_files = _require("force_project_files", bool)
        global_config = _require("global_config", str, allow_none=True)
        plan_hash_v = _require("plan_hash", str)
        actions_raw = _require("actions", list)
        findings_raw = _require("findings", list)

        from n3rv.reconcile.planfile import PlanFileError

        if not re.fullmatch(r"[0-9a-f]{64}", plan_hash_v):
            raise PlanFileError("wrong type or format for key: plan_hash")

        actions: list[Action] = []
        action_keys = {"kind", "target", "strategy", "reason", "source", "current_sha", "desired_sha"}
        for item in actions_raw:
            if not isinstance(item, dict):
                raise PlanFileError("wrong type for key: actions")
            missing = [k for k in action_keys if k not in item]
            if missing:
                raise PlanFileError(f"missing required key: {missing[0]}")
            kind_val = item["kind"]
            if not isinstance(kind_val, str):
                raise PlanFileError("wrong type for key: kind")
            try:
                kind = ActionKind(kind_val)
            except ValueError as exc:
                raise PlanFileError(f"bad action kind: {kind_val}") from exc
            for k in ("target", "strategy", "reason"):
                if not isinstance(item[k], str):
                    raise PlanFileError(f"wrong type for key: {k}")
            for k in ("source", "current_sha", "desired_sha"):
                v = item[k]
                if v is not None and not isinstance(v, str):
                    raise PlanFileError(f"wrong type for key: {k}")
            actions.append(
                Action(
                    kind=kind,
                    target=item["target"],
                    strategy=item["strategy"],
                    reason=item["reason"],
                    source=item["source"],
                    current_sha=item["current_sha"],
                    desired_sha=item["desired_sha"],
                )
            )

        findings: list[Finding] = []
        finding_keys = {"code", "level", "message"}
        for item in findings_raw:
            if not isinstance(item, dict):
                raise PlanFileError("wrong type for key: findings")
            missing = [k for k in finding_keys if k not in item]
            if missing:
                raise PlanFileError(f"missing required key: {missing[0]}")
            for k in finding_keys:
                if not isinstance(item[k], str):
                    raise PlanFileError(f"wrong type for key: {k}")
            findings.append(Finding(code=item["code"], level=item["level"], message=item["message"]))

        return cls(
            state_sha=state_sha,
            n3rv_version=n3rv_version,
            templates_sha=templates_sha_v,
            env_hash=env_hash_v,
            actions=tuple(actions),
            findings=tuple(findings),
            force_project_files=force_project_files,
            global_config=global_config,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_hash": self.plan_hash,
            "env_hash": self.env_hash,
            "state_sha": self.state_sha,
            "n3rv_version": self.n3rv_version,
            "templates_sha": self.templates_sha,
            "exit_code": self.exit_code,
            "empty": self.empty,
            "force_project_files": self.force_project_files,
            "global_config": self.global_config,
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


def file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def selected_entries(state: Any) -> tuple[list[UpdateEntry], list[Finding]]:
    """Entries the declared state asks for, plus any component it names in vain.

    Validation uses the union of both manifests (state may legitimately declare
    an init-only component); selection uses the update manifest, which is what
    a reconciler can actually converge.
    """
    from n3rv.reconcile.state import manifest_components

    findings: list[Finding] = []
    known = manifest_components()
    declared = declared_components(state)
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

    from n3rv.init import ALWAYS_ON_FAMILY, TIER_FAMILIES

    tier_family_to_attr = {v: k for k, v in TIER_FAMILIES.items()}
    selected: list[UpdateEntry] = []
    for entry in FILE_UPDATE_MANIFEST:
        family = family_for(entry.output_path)
        if family == ALWAYS_ON_FAMILY:
            selected.append(entry)
        elif family in DECLARABLE_FAMILIES:
            if entry.output_path in wanted:
                selected.append(entry)
        elif family in tier_family_to_attr:
            if getattr(state.tiers, tier_family_to_attr[family]):
                selected.append(entry)
        else:
            selected.append(entry)
    return selected, findings


def declared_components(state: Any) -> dict[str, set[str]]:
    """Component names the state declares, grouped by declarable family.

    Deletion decisions need this: a lockfile path that no manifest entry selects
    is only a *decline* if the state does not name it. ``init`` ships outputs
    (spec-kit) that ``update`` does not manage, so those paths are never
    selected and must not be mistaken for components the project dropped.
    """
    return {
        "agents": set(state.components.agents),
        "skills": set(state.components.skills),
        "commands": set(state.components.commands),
        "hooks": set(state.components.hooks),
    }


def _name(family: str, output_path: str) -> str:
    if family == "skills":
        parts = output_path.split("/")
        return parts[-2] if len(parts) >= 2 else output_path
    return Path(output_path).stem


def _singular(family: str) -> str:
    return family[:-1] if family.endswith("s") else family


def _env_static_segments(engine: TemplateEngine, entry: UpdateEntry, ctx: dict[str, Any]) -> list[str | None] | None:
    """Return static/placeholder segments for env-derived spans, or None if none."""

    def _render_with(pair: tuple[str, str]) -> str:
        c = dict(ctx)
        c["project_root"], c["n3rv_binary"] = pair
        return engine.render(entry.template_name, entry_render_context(entry, c))

    sa = _render_with(_SENTINEL_A)
    sb = _render_with(_SENTINEL_B)
    if sa == sb:
        return None
    # equal-length sentinels keep sa/sb aligned; diff marks env-derived bytes
    segments: list[str | None] = []
    start = 0
    in_env = sa[0] != sb[0]
    for i in range(len(sa)):
        now_env = sa[i] != sb[i]
        if now_env != in_env:
            if not in_env:
                segments.append(sa[start:i])
            else:
                segments.append(None)
            start = i
            in_env = now_env
    if not in_env:
        segments.append(sa[start:])
    else:
        segments.append(None)
    return segments


def _mask_env_text(text: str, segments: list[str | None]) -> str | None:
    """Replace env-derived spans with a constant placeholder. None if text doesn't match."""
    pos = 0
    out: list[str] = []
    for i, seg in enumerate(segments):
        if seg is None:
            out.append(_ENV_PLACEHOLDER)
            next_static = next((segments[j] for j in range(i + 1, len(segments)) if segments[j] is not None), None)
            if next_static is None:
                pos = len(text)
            else:
                idx = text.find(next_static, pos)
                if idx == -1:
                    return None
                pos = idx
        else:
            idx = text.find(seg, pos)
            if idx == -1 or idx != pos:
                return None
            out.append(seg)
            pos += len(seg)
    if pos != len(text):
        return None
    return "".join(out)


def build_plan(
    root: Path,
    state: Any,
    *,
    templates_dir: Path,
    n3rv_version: str,
    n3rv_binary: str,
    force_project_files: bool = False,
    global_config: Path | None = None,
) -> Plan:
    """Compute the action list. Pure with respect to the filesystem."""
    return resolve_actions(
        root,
        state,
        templates_dir=templates_dir,
        n3rv_version=n3rv_version,
        n3rv_binary=n3rv_binary,
        force_project_files=force_project_files,
        global_config=global_config,
    )[0]


def resolve_actions(
    root: Path,
    state: Any,
    *,
    templates_dir: Path,
    n3rv_version: str,
    n3rv_binary: str,
    force_project_files: bool = False,
    global_config: Path | None = None,
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

        # env-neutralized compare (defect #4): only env inputs differ -> advisory finding, no action.
        env_segments = _env_static_segments(engine, entry, ctx)
        if env_segments is not None:
            masked_current = _mask_env_text(current, env_segments)
            masked_desired = _mask_env_text(desired, env_segments)
            if masked_current is not None and masked_current == masked_desired:
                findings.append(
                    Finding(
                        "environment",
                        "warn",
                        f"{entry.output_path}: differs only in environment inputs (project_root / n3rv_binary)",
                    )
                )
                continue

        if entry.project_owned and not force_project_files and _project_owned_modified(entry, root, target):
            # update() silently skips these. A reconciler must not: it cannot
            # converge the file without overwriting a human edit, and a warning
            # that never becomes an action hides a file it will never converge.
            provenance = _locked_managed_hash(root, entry.output_path)
            reason = (
                "lockfile records no provenance for this project-owned file"
                if provenance is None
                else "managed region changed since generation"
            )
            actions.append(
                Action(
                    ActionKind.CONFLICT,
                    entry.output_path,
                    entry.strategy.value,
                    reason,
                    entry.template_name,
                    file_sha(target),
                    desired_sha,
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
                file_sha(target),
                desired_sha,
            )
        )

    actions.extend(_deletions(root, entries, lock_entries, state.tiers, declared_components(state)))
    findings.extend(_declined_tier_findings(root, entries, lock_entries, state.tiers))

    # Advisory: detection may report, never act.
    from n3rv.reconcile.divergence import detect_divergence

    for raw in detect_divergence(root, state):
        findings.append(Finding(raw["code"], raw["level"], raw["message"]))

    # Inert: configuration the policy would drop at startup.
    from n3rv.reconcile.policy import allowlisted, inert_targets

    allow, reason = allowlisted(global_config)
    if allow is None:
        findings.append(Finding("policy_unknown", "warn", reason or "policy unavailable"))
    else:
        blocked = inert_targets(
            allow,
            servers=[server.name for server in state.mcp.servers],
            skills=list(state.components.skills),
        )
        actions = _mark_inert(actions, blocked)
        for why in sorted(blocked.values()):
            findings.append(Finding("inert", "warn", why))

    return (
        Plan(
            state_sha=state.sha256(),
            n3rv_version=n3rv_version,
            templates_sha=templates_sha(templates_dir),
            env_hash=env_hash(root, n3rv_binary),
            actions=tuple(actions),
            findings=tuple(findings),
            force_project_files=force_project_files,
            global_config=str(global_config) if global_config is not None else None,
        ),
        resolutions,
    )


def _mark_inert(actions: list[Action], blocked: dict[str, str]) -> list[Action]:
    """Turn an action into INERT when its content is configuration policy drops.

    opencode.json carries the MCP server set, so one unlisted server makes the
    whole file inert: writing it would produce a config that looks applied and
    silently loses that server at startup.
    """
    marked: list[Action] = []
    for action in actions:
        reason = None
        if action.target == "opencode.json":
            reason = next((why for key, why in sorted(blocked.items()) if key.startswith("mcp:")), None)
        elif action.target.startswith(".opencode/skills/"):
            skill = action.target.split("/")[2]
            reason = blocked.get(f"skill:{skill}")
        if reason is None or action.kind in {ActionKind.CONFLICT, ActionKind.INERT}:
            marked.append(action)
        else:
            marked.append(
                Action(
                    ActionKind.INERT,
                    action.target,
                    action.strategy,
                    reason,
                    action.source,
                    action.current_sha,
                    action.desired_sha,
                )
            )
    return marked


def _deletions(
    root: Path,
    entries: list[UpdateEntry],
    lock_entries: dict[str, Any],
    tiers: Any = None,
    declared: dict[str, set[str]] | None = None,
) -> list[Action]:
    """Files this generator wrote before and no longer selects.

    Only touched when the file still matches what was written: anything a human
    edited becomes a conflict, never a deletion.

    "No longer selects" means the state stopped naming it, not merely that the
    update manifest has no entry for it: ``init`` ships outputs (spec-kit) that
    ``update`` never manages, and those are never selected by construction.
    """
    from n3rv.init import ALWAYS_ON_FAMILY, TIER_FAMILIES

    tier_family_to_attr = {v: k for k, v in TIER_FAMILIES.items()}
    selected = {entry.output_path for entry in entries}
    removed: list[Action] = []
    for output_path, record in sorted(lock_entries.items()):
        if output_path in selected:
            continue
        try:
            family = family_for(output_path)
        except Exception:
            continue
        if family == ALWAYS_ON_FAMILY:
            continue
        if family in DECLARABLE_FAMILIES:
            if declared is not None and _name(family, output_path) in declared.get(family, set()):
                continue  # still declared, just not selectable by the update manifest
        elif family in tier_family_to_attr and tiers is not None:
            if getattr(tiers, tier_family_to_attr[family]):
                continue  # tier enabled: absence not a decline
            # tier declined: fall through to sha-guarded deletion
        else:
            continue
        target = root / output_path
        if not target.exists():
            continue
        current_sha = file_sha(target)
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


def _declined_tier_findings(
    root: Path, entries: list[UpdateEntry], lock_entries: dict[str, Any], tiers: Any
) -> list[Finding]:
    """Warn about untracked files in declined tiers (not ours to delete)."""
    from n3rv.init import TIER_FAMILIES

    tier_family_to_attr = {v: k for k, v in TIER_FAMILIES.items()}
    selected = {entry.output_path for entry in entries}
    findings: list[Finding] = []
    for entry in FILE_UPDATE_MANIFEST:
        try:
            family = family_for(entry.output_path)
        except Exception:
            continue
        if family not in tier_family_to_attr:
            continue
        if getattr(tiers, tier_family_to_attr[family]):
            continue
        if entry.output_path in selected or entry.output_path in lock_entries:
            continue
        target = root / entry.output_path
        if target.exists():
            tier_name = tier_family_to_attr[family]
            findings.append(
                Finding(
                    "tier_declined_untracked",
                    "warn",
                    f"{entry.output_path}: tier {tier_name} is declined but the file exists and is untracked; "
                    "not deleting a file n3rv did not write",
                )
            )
    return findings
