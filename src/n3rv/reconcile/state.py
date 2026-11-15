"""Declared desired state for the reconciler (n3rv#9).

``.n3rv/state.yaml`` is the only hand-edited input to generation. It pins the
decisions that detection used to make at render time - stack, project name, the
component families, the MCP server set - so that "what will n3rv do" stops
depending on which branch happens to be checked out.

Schema v2 adds three declarations on top of v1: ``detection.requires_python``
(a detected pin), ``deployment.target`` and ``tiers`` (operator decisions,
defaulting to v1 behavior). Schema 1 files load under v2 code with defaults
filled in memory; ``n3rv state migrate`` rewrites them on disk.

Schema v1 was already deliberately explicit: there is no ``all`` sentinel. A
skill or server shipped by a later n3rv appears in a plan only after someone
adds it here.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from n3rv.init import ALWAYS_ON_FAMILY, DECLARABLE_FAMILIES, FILE_MANIFEST, family_for
from n3rv.init.context import Stack
from n3rv.init.detector import detect_stack

SCHEMA_VERSION = 2

STATE_DIR = ".n3rv"
STATE_FILENAME = "state.yaml"


class StateError(Exception):
    """Raised when the declared state is missing or cannot be trusted."""


class ProjectIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    stack: Stack


class Components(BaseModel):
    """Families a project may choose. Always-on outputs are not listed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    agents: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    commands: list[str] = Field(default_factory=list)
    hooks: list[str] = Field(default_factory=list)


class McpServerState(BaseModel):
    """A full server specification.

    Specs are pinned, not resolved by name from a catalog: a catalog bump must
    show up as divergence, never as a silent rewrite of a repository's config.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    type: str = "local"
    command: list[str] | None = None
    url: str | None = None
    environment: dict[str, str] | None = None
    description: str = ""
    disabled: bool = False


class McpState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    servers: list[McpServerState] = Field(default_factory=list)


class StructureState(BaseModel):
    """Detected layout, pinned. Templates interpolate these into file bodies."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    has_src_dir: bool = False
    has_tests_dir: bool = False
    has_docs_dir: bool = False
    has_scripts_dir: bool = False
    has_web_files: bool = False
    entry_points: list[str] = Field(default_factory=list)
    key_dirs: list[str] = Field(default_factory=list)


class FrameworkState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    category: str = ""
    guidance: str = ""
    version_hint: str | None = None


class ToolState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    command: str
    category: str = ""


class Detection(BaseModel):
    """What the analyzers decided when this state was written.

    Pinned rather than re-detected at render time, so the same state produces
    the same bytes regardless of which branch is checked out.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    structure: StructureState = Field(default_factory=StructureState)
    frameworks: list[FrameworkState] = Field(default_factory=list)
    tools: list[ToolState] = Field(default_factory=list)
    requires_python: str | None = None


class Deployment(BaseModel):
    """Declared deployment topology (an operator decision, not a detection).

    ``mainframe`` keeps the conservative default: a repo that deploys but did
    not declare must still fail loud in ``doctor --strict``. ``none`` is the
    declaration "this repository is not deployed"; it wins over a stale remote.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    target: Literal["mainframe", "none"] = "mainframe"


class Tiers(BaseModel):
    """Always-on tiers a project may decline (an operator decision).

    All-True reproduces schema v1 behavior: every always-on output is
    generated. The floor (AGENTS.md + opencode.json) is not represented here -
    it is never declinable.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    runtime: bool = True
    spec_kit: bool = True
    tooling: bool = True


class BoardTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    owner: str = Field(min_length=1)
    project: int = Field(ge=1)


class ProjectState(BaseModel):
    """The declared state. Unknown keys are rejected, never ignored."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    schema_version: Literal[1, 2] = Field(default=SCHEMA_VERSION, alias="schema")
    project: ProjectIdentity
    components: Components = Field(default_factory=Components)
    detection: Detection = Field(default_factory=Detection)
    mcp: McpState = Field(default_factory=McpState)
    deployment: Deployment = Field(default_factory=Deployment)
    tiers: Tiers = Field(default_factory=Tiers)
    board: BoardTarget | None = None

    def to_yaml(self) -> str:
        return yaml.safe_dump(self.model_dump(by_alias=True, mode="json"), sort_keys=False)

    def canonical(self) -> str:
        """Stable text used for hashing. Sorted, alias-keyed, no whitespace noise."""
        return json.dumps(self.model_dump(by_alias=True, mode="json"), sort_keys=True, separators=(",", ":"))

    def sha256(self) -> str:
        return hashlib.sha256(self.canonical().encode("utf-8")).hexdigest()


def state_path(root: Path) -> Path:
    return root / STATE_DIR / STATE_FILENAME


def load_state(root: Path) -> ProjectState:
    """Load and validate the declared state.

    Schema 1 files are upgraded to schema 2 in memory (new fields get their
    v1-behavior defaults); the on-disk file is only rewritten by
    ``n3rv state migrate``.

    Raises:
        StateError: the file is missing, is not valid YAML, violates the
            schema, or declares an unsupported schema version. A typo must
            never silently do nothing.
    """
    path = state_path(root)
    if not path.is_file():
        raise StateError(f"no {STATE_DIR}/{STATE_FILENAME} in {root}; run 'n3rv state init' first")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise StateError(f"{path} is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise StateError(f"{path} must contain a mapping at the top level")
    schema = raw.get("schema", 1)
    if schema in (1, 2):
        try:
            return _upgrade_v1(raw) if schema == 1 else ProjectState.model_validate(raw)
        except ValidationError as exc:
            raise StateError(f"{path} does not match schema {SCHEMA_VERSION}:\n{exc}") from exc
    raise StateError(f"{path} uses schema {schema}; this n3rv supports 1 and 2")


def _upgrade_v1(raw: dict[str, Any]) -> ProjectState:
    """Fill schema v2 fields at their v1-behavior defaults, in memory only.

    v2 is purely additive, so model_validate fills ``requires_python``,
    ``deployment`` and ``tiers`` from their defaults; the only explicit
    mutation is the schema marker bump. No disk write happens here.
    """
    data = dict(raw)
    data["schema"] = SCHEMA_VERSION
    return ProjectState.model_validate(data)


def save_state(root: Path, state: ProjectState, *, force: bool = False) -> Path:
    """Write the declared state. Refuses to overwrite unless ``force``."""
    path = state_path(root)
    if path.exists() and not force:
        raise StateError(f"{path} already exists; pass --force to overwrite it")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(state.to_yaml(), encoding="utf-8")
    return path


def manifest_components() -> dict[str, dict[str, str]]:
    """Manifest-declared components, grouped by family: name -> output path.

    Covers both manifests: ``init`` ships spec-kit templates that ``update``
    does not manage, and ``update`` manages AGENTS.md which ``init`` does not.
    A name is valid if either list knows it.

    Names are the identifiers a user reads: ``code`` for a skill, ``git-ops``
    for an agent or command, ``pre-push`` for a hook.
    """
    from n3rv.init.update import FILE_UPDATE_MANIFEST

    grouped: dict[str, dict[str, str]] = {family: {} for family in DECLARABLE_FAMILIES}
    outputs = [entry[1] for entry in FILE_MANIFEST] + [entry.output_path for entry in FILE_UPDATE_MANIFEST]
    for output_path in outputs:
        family = family_for(output_path)
        if family == ALWAYS_ON_FAMILY:
            continue
        name = _component_name(family, output_path)
        if name is not None:
            grouped[family][name] = output_path
    return grouped


def _component_name(family: str, output_path: str) -> str | None:
    if family == "skills":
        # .opencode/skills/<name>/SKILL.md
        parts = output_path.split("/")
        return parts[-2] if len(parts) >= 2 else None
    if family in {"agents", "commands", "hooks"}:
        return Path(output_path).stem
    return None


def state_from_tree(root: Path) -> ProjectState:
    """Freeze the state a converged tree already expresses.

    Components are the manifest entries actually present on disk - not what
    detection would offer, and not files someone installed by hand (those are
    user-owned, not ours to declare). Detection and MCP specs are pinned from
    the analyzers, because templates interpolate them into file bodies.
    """
    stack_info = detect_stack(root)
    grouped = manifest_components()

    components = Components(
        agents=sorted(name for name, out in grouped["agents"].items() if (root / out).is_file()),
        skills=sorted(name for name, out in grouped["skills"].items() if (root / out).is_file()),
        commands=sorted(name for name, out in grouped["commands"].items() if (root / out).is_file()),
        hooks=sorted(name for name, out in grouped["hooks"].items() if (root / out).is_file()),
    )

    profile = _profile(root, stack_info)
    served = _servers_from_config(_read_project_config(root))

    return ProjectState(
        project=ProjectIdentity(name=stack_info.project_name, stack=stack_info.stack),
        components=components,
        detection=Detection(
            structure=StructureState(
                has_src_dir=profile.structure.has_src_dir,
                has_tests_dir=profile.structure.has_tests_dir,
                has_docs_dir=profile.structure.has_docs_dir,
                has_scripts_dir=profile.structure.has_scripts_dir,
                has_web_files=profile.structure.has_web_files,
                entry_points=list(profile.structure.entry_points),
                key_dirs=list(profile.structure.key_dirs),
            ),
            frameworks=[FrameworkState(**asdict(fw)) for fw in profile.frameworks],
            tools=[ToolState(**asdict(tool)) for tool in profile.tools],
            requires_python=profile.requires_python,
        ),
        mcp=McpState(servers=served if served is not None else _servers_from_profile(profile)),
    )


def _read_project_config(root: Path) -> Any:
    config = root / "opencode.json"
    if not config.is_file():
        return None
    try:
        return json.loads(config.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _servers_from_config(data: Any) -> list[McpServerState] | None:
    """Servers a project config actually declares, in either layout.

    V2 keeps them under ``mcp.servers``; V1 kept server definitions directly
    under ``mcp``. ``None`` means the config says nothing, which is different
    from declaring an empty set - only then does the catalog get a vote.
    """
    if not isinstance(data, dict):
        return None
    mcp = data.get("mcp")
    if not isinstance(mcp, dict):
        return None
    servers = mcp.get("servers")
    if isinstance(servers, dict):
        raw = servers
    else:
        raw = {
            name: value
            for name, value in mcp.items()
            if name not in {"servers", "timeout"} and isinstance(value, dict) and "type" in value
        }
    if not raw:
        return None if not isinstance(mcp.get("servers"), dict) else []
    return [_server_state(name, value) for name, value in sorted(raw.items())]


def _server_state(name: str, value: Any) -> McpServerState:
    data = dict(value) if isinstance(value, dict) else {}
    command = data.get("command")
    environment = data.get("environment")
    return McpServerState(
        name=name,
        type=str(data.get("type") or "local"),
        command=[str(part) for part in command] if isinstance(command, list) else None,
        url=data.get("url") if isinstance(data.get("url"), str) else None,
        environment={str(k): str(v) for k, v in environment.items()} if isinstance(environment, dict) else None,
        description=str(data.get("description") or ""),
        disabled=bool(data.get("disabled") or data.get("enabled") is False),
    )


def _servers_from_profile(profile: Any) -> list[McpServerState]:
    return [
        McpServerState(
            name=server.name,
            type=server.type,
            command=list(server.command) if server.command else None,
            url=server.url,
            environment=dict(server.environment) if server.environment else None,
            description=server.description,
            disabled=server.disabled,
        )
        for server in profile.mcp_servers
    ]


def _profile(root: Path, stack_info: Any) -> Any:
    from n3rv.init.analyzer import analyze_project
    from n3rv.init.context import ProjectContext

    context = ProjectContext.build(
        project_name=stack_info.project_name,
        stack=stack_info.stack,
        project_root=root,
    )
    return analyze_project(root, context)


def render_context(state: ProjectState, *, project_root: Path, n3rv_binary: str, n3rv_version: str) -> dict[str, Any]:
    """Jinja context built from the declared state instead of live detection.

    Only two inputs are environment-derived: ``project_root`` and
    ``n3rv_binary``. They are rendered (some outputs embed absolute paths) but
    never enter a plan's identity - see plan_hash/env_hash.
    """
    from n3rv.board import render_context as board_context

    return {
        "project_name": state.project.name,
        "stack": state.project.stack.value,
        "project_root": str(project_root),
        "n3rv_version": n3rv_version,
        # Templates do not render a timestamp; it is provided empty so it can
        # never leak non-determinism into output.
        "timestamp": "",
        "n3rv_binary": n3rv_binary,
        "profile_structure": state.detection.structure.model_dump(),
        "profile_frameworks": [fw.model_dump() for fw in state.detection.frameworks],
        "profile_has_frameworks": bool(state.detection.frameworks),
        "profile_tools": [tool.model_dump() for tool in state.detection.tools],
        "profile_has_tools": bool(state.detection.tools),
        "profile_mcp_servers": [server.model_dump() for server in state.mcp.servers],
        "profile_has_mcp_servers": bool(state.mcp.servers),
        "requires_python": state.detection.requires_python,
        **board_context(),
    }


def state_summary(state: ProjectState) -> dict[str, Any]:
    """Small, stable shape for ``state show --json`` and for tests."""
    return {
        "schema": state.schema_version,
        "path": f"{STATE_DIR}/{STATE_FILENAME}",
        "sha256": state.sha256(),
        "project": {"name": state.project.name, "stack": state.project.stack.value},
        "components": {
            "agents": len(state.components.agents),
            "skills": len(state.components.skills),
            "commands": len(state.components.commands),
            "hooks": len(state.components.hooks),
        },
        "detection": {
            "frameworks": len(state.detection.frameworks),
            "tools": len(state.detection.tools),
        },
        "mcp_servers": [server.name for server in state.mcp.servers],
    }
