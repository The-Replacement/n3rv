"""Divergence advisor (n3rv#9).

Detection no longer decides anything: rendering is a pure function of the
declared state. But a tree that has outgrown its state is worth knowing about -
a web/ directory appearing overnight, a framework arriving in pyproject - so
the analyzers keep running and report the difference.

This module produces findings only. It has no action kind, no write path and no
influence on what gets rendered.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from n3rv.init.analyzer import analyze_project
from n3rv.init.context import ProjectContext


def detect_divergence(root: Path, state: Any) -> list[dict[str, str]]:
    """Compare the declared state with what the tree currently looks like.

    Returns findings shaped like ``plan.Finding`` on purpose: divergence is a
    report, not a change.
    """
    try:
        live = analyze_project(
            root,
            ProjectContext.build(
                project_name=state.project.name,
                stack=state.project.stack,
                project_root=root,
            ),
        )
    except Exception as exc:  # detection failing must never fail a plan
        return [{"code": "divergence_unknown", "level": "warn", "message": f"detection unavailable: {exc}"}]

    findings: list[dict[str, str]] = []
    findings.extend(_structure(root, state, live.structure))
    findings.extend(_names("framework", state.detection.frameworks, live.frameworks))
    findings.extend(_names("tool", state.detection.tools, live.tools))
    findings.extend(_servers(state, live.mcp_servers))
    return findings


def _structure(root: Path, state: Any, live: Any) -> list[dict[str, str]]:
    declared = state.detection.structure
    changes: list[str] = []
    for field in ("has_src_dir", "has_tests_dir", "has_docs_dir", "has_scripts_dir", "has_web_files"):
        if getattr(declared, field) != getattr(live, field):
            changes.append(f"{field.removeprefix('has_')} {'appeared' if getattr(live, field) else 'disappeared'}")
    if not changes:
        return []
    return [
        {
            "code": "divergence",
            "level": "warn",
            "message": f"{root.name}: {', '.join(changes)} since the state was written "
            "(state still decides; update it to adopt the change)",
        }
    ]


def _names(noun: str, declared: list[Any], live: list[Any]) -> list[dict[str, str]]:
    before = {item.name for item in declared}
    after = {item.name for item in live}
    added = sorted(after - before)
    removed = sorted(before - after)
    findings: list[dict[str, str]] = []
    if added:
        findings.append(
            {
                "code": "divergence",
                "level": "warn",
                "message": f"detection now sees {noun}(s) {', '.join(added)} that the state does not declare",
            }
        )
    if removed:
        findings.append(
            {
                "code": "divergence",
                "level": "warn",
                "message": f"the state declares {noun}(s) {', '.join(removed)} that detection no longer sees",
            }
        )
    return findings


def _servers(state: Any, live: list[Any]) -> list[dict[str, str]]:
    declared = {server.name for server in state.mcp.servers}
    recommended = {server.name for server in live}
    offered = sorted(recommended - declared)
    if not offered:
        return []
    return [
        {
            "code": "divergence",
            "level": "warn",
            "message": f"detection recommends MCP server(s) {', '.join(offered)} that the state does not declare "
            "(add them to state.yaml to adopt)",
        }
    ]
