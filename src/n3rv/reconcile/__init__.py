"""Reconciler package (n3rv#9): declared state -> plan -> apply.

The pieces are deliberately separate: ``state`` validates the only hand-edited
input, ``plan`` is pure, ``apply`` performs effects. See n3rv#9 and memory
``sdd-pull-based-reconciler-design``.
"""

from __future__ import annotations

from n3rv.reconcile.state import (
    SCHEMA_VERSION,
    BoardTarget,
    Components,
    Detection,
    FrameworkState,
    McpServerState,
    McpState,
    ProjectIdentity,
    ProjectState,
    StateError,
    StructureState,
    ToolState,
    load_state,
    render_context,
    save_state,
    state_from_tree,
    state_path,
    state_summary,
)

__all__ = [
    "SCHEMA_VERSION",
    "BoardTarget",
    "Components",
    "Detection",
    "FrameworkState",
    "McpServerState",
    "McpState",
    "ProjectIdentity",
    "ProjectState",
    "StateError",
    "StructureState",
    "ToolState",
    "load_state",
    "render_context",
    "save_state",
    "state_from_tree",
    "state_path",
    "state_summary",
]
