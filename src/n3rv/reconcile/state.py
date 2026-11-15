"""Declared desired state for the reconciler (n3rv#9).

``.n3rv/state.yaml`` is the only hand-edited input to generation. It pins the
decisions that detection used to make at render time - stack, project name, the
component families, the MCP server set - so that "what will n3rv do" stops
depending on which branch happens to be checked out.

Schema v1 is deliberately explicit: there is no ``all`` sentinel. A skill or
server shipped by a later n3rv appears in a plan only after someone adds it
here.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from n3rv.init import ALWAYS_ON_FAMILY, DECLARABLE_FAMILIES, FILE_MANIFEST, family_for
from n3rv.init.context import Stack
from n3rv.init.detector import detect_stack

SCHEMA_VERSION = 1

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


class McpState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    servers: list[str] = Field(default_factory=list)


class BoardTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    owner: str = Field(min_length=1)
    project: int = Field(ge=1)


class ProjectState(BaseModel):
    """The declared state. Unknown keys are rejected, never ignored."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    schema_version: Literal[1] = Field(default=SCHEMA_VERSION, alias="schema")
    project: ProjectIdentity
    components: Components = Field(default_factory=Components)
    mcp: McpState = Field(default_factory=McpState)
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

    Raises:
        StateError: the file is missing, is not valid YAML, or violates the
            schema. A typo must never silently do nothing.
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
    try:
        return ProjectState.model_validate(raw)
    except ValidationError as exc:
        raise StateError(f"{path} does not match schema {SCHEMA_VERSION}:\n{exc}") from exc


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

    Names are the identifiers a user reads: ``code`` for a skill, ``git-ops``
    for an agent or command, ``pre-push`` for a hook.
    """
    grouped: dict[str, dict[str, str]] = {family: {} for family in DECLARABLE_FAMILIES}
    for entry in FILE_MANIFEST:
        output_path = entry[1]
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
    user-owned, not ours to declare). MCP servers come from the project's
    opencode.json, falling back to detection when there is no config yet.
    """
    stack_info = detect_stack(root)
    grouped = manifest_components()

    components = Components(
        agents=sorted(name for name, out in grouped["agents"].items() if (root / out).is_file()),
        skills=sorted(name for name, out in grouped["skills"].items() if (root / out).is_file()),
        commands=sorted(name for name, out in grouped["commands"].items() if (root / out).is_file()),
        hooks=sorted(name for name, out in grouped["hooks"].items() if (root / out).is_file()),
    )

    return ProjectState(
        project=ProjectIdentity(name=stack_info.project_name, stack=stack_info.stack),
        components=components,
        mcp=McpState(servers=_servers_from_tree(root)),
    )


def _servers_from_tree(root: Path) -> list[str]:
    config = root / "opencode.json"
    if config.is_file():
        try:
            data = json.loads(config.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = None
        servers = _servers_from_config(data)
        if servers is not None:
            return servers
    return _servers_from_detection(root)


def _servers_from_config(data: Any) -> list[str] | None:
    """Servers a project config actually declares, in either layout.

    V2 keeps them under ``mcp.servers``; V1 kept server definitions directly
    under ``mcp``. ``None`` means the config says nothing, which is different
    from declaring an empty set - only then does detection get a vote.
    """
    if not isinstance(data, dict):
        return None
    mcp = data.get("mcp")
    if not isinstance(mcp, dict):
        return None
    servers = mcp.get("servers")
    if isinstance(servers, dict):
        return sorted(servers)
    v1_servers = {
        name: value
        for name, value in mcp.items()
        if name not in {"servers", "timeout"} and isinstance(value, dict) and "type" in value
    }
    return sorted(v1_servers) if v1_servers else None


def _servers_from_detection(root: Path) -> list[str]:
    from n3rv.init.analyzer import analyze_project
    from n3rv.init.context import ProjectContext

    stack_info = detect_stack(root)
    try:
        context = ProjectContext.build(
            project_name=stack_info.project_name,
            stack=stack_info.stack,
            project_root=root,
        )
    except Exception:  # pragma: no cover - detection must not block state creation
        return []
    return sorted(server.name for server in analyze_project(root, context).mcp_servers)


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
        "mcp_servers": list(state.mcp.servers),
    }
