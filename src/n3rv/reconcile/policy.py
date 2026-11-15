"""Policy awareness for the reconciler (n3rv#9).

Since 2026-11-15 the global OpenCode config denies MCP servers, skills and
plugins by default and re-allows them by name. A reconciler that writes a
server or skill the policy drops gives a repository configuration that is
applied and inert - with no error anywhere. That is the failure this module
exists to name.

It reads the policy. It never edits it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

DEFAULT_GLOBAL_CONFIG = Path.home() / ".config" / "opencode" / "opencode.json"


@dataclass(frozen=True)
class Allowlist:
    servers: frozenset[str]
    skills: frozenset[str]


def allowlisted(global_config: Path | None = None) -> tuple[Allowlist | None, str | None]:
    """Return the allowed resource names, or None when the policy says nothing.

    ``None`` is not "nothing is allowed": it means there is no policy
    information, which must not block a repository that has no global config.
    The second element is a human-readable reason when information is missing.
    """
    path = global_config if global_config is not None else DEFAULT_GLOBAL_CONFIG
    if not path.is_file():
        return None, f"no policy file at {path}"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return None, f"{path} is not valid JSON: {exc}"
    policies = data.get("policies")
    if not isinstance(policies, list) or not policies:
        return None, f"{path} declares no policies"

    servers: set[str] = set()
    skills: set[str] = set()
    seen: set[str] = set()
    for statement in policies:
        if not isinstance(statement, dict):
            continue
        action = statement.get("action")
        resource = statement.get("resource")
        effect = statement.get("effect")
        if not isinstance(action, str) or not isinstance(resource, str):
            continue
        if effect == "deny" and resource == "*":
            seen.add(action)
        elif effect == "allow":
            if action == "mcp.use":
                servers.add(resource)
            elif action == "skill.use":
                skills.add(resource)
    if not seen:
        return None, f"{path} declares no default-deny policies"
    return Allowlist(frozenset(servers), frozenset(skills)), None


def inert_targets(
    allowlist: Allowlist,
    *,
    servers: list[str],
    skills: list[str],
) -> dict[str, str]:
    """Map each resource the policy would drop to the reason it would drop."""
    blocked: dict[str, str] = {}
    for server in servers:
        if server not in allowlist.servers:
            blocked[f"mcp:{server}"] = f"MCP server {server!r} is not in the global policy allowlist"
    for skill in skills:
        if skill not in allowlist.skills:
            blocked[f"skill:{skill}"] = f"skill {skill!r} is not in the global policy allowlist"
    return blocked
