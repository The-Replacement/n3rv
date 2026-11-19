"""Writer utilities for project scaffolding."""

from __future__ import annotations

import logging
from enum import StrEnum
from pathlib import Path

logger = logging.getLogger("n3rv.init.writer")

MARKER_START = "# >>> N3RV-MARKER-START"
MARKER_END = "# >>> N3RV-MARKER-END"


class WriteResult(StrEnum):
    """Result of a write operation."""

    CREATED = "created"
    UPDATED = "updated"
    OVERWRITTEN = "overwritten"
    SKIPPED = "skipped"


def write_file(
    target: Path,
    content: str,
    *,
    force: bool = False,
    use_markers: bool = False,
    make_executable: bool = False,
) -> WriteResult:
    """Write content to target file.

    Args:
        target: Path to write to
        content: File content
        force: Overwrite existing files without checking markers
        use_markers: Check for MARKER_START/END markers before overwriting
        make_executable: chmod +x the file

    Returns:
        WriteResult indicating what happened
    """
    from n3rv.init.update import MARKER_END, MARKER_START

    if target.exists() and not force:
        if use_markers:
            existing = target.read_text(encoding="utf-8")
            if MARKER_START in existing and MARKER_END in existing:
                start_idx = existing.find(MARKER_START)
                end_idx = existing.find(MARKER_END) + len(MARKER_END)
                before = existing[:start_idx]
                after = existing[end_idx:]
                new_content = f"{before}{MARKER_START}\n{content}\n{MARKER_END}{after}"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(new_content, encoding="utf-8")
                return WriteResult.UPDATED
            return WriteResult.SKIPPED
        return WriteResult.SKIPPED

    existed = target.exists()
    target.parent.mkdir(parents=True, exist_ok=True)

    if use_markers and (not existed or force):
        content = f"{MARKER_START}\n{content}\n{MARKER_END}\n"

    target.write_text(content, encoding="utf-8")
    if make_executable:
        target.chmod(0o755)
    return WriteResult.OVERWRITTEN if existed else WriteResult.CREATED


def configure_git_hooks(root: Path) -> bool:
    """Configure git hooks for the project.

    Args:
        root: Project root directory

    Returns:
        True if hooks were configured, False otherwise
    """
    import subprocess

    git_dir = root / ".git"
    if not git_dir.is_dir():
        return False

    hooks_dir = git_dir / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)

    # Create pre-push hook that delegates to .n3rv/githooks/
    pre_push = hooks_dir / "pre-push"
    pre_push.write_text(
        "#!/bin/sh\n# N3RV pre-push hook\nexec .n3rv/githooks/pre-push\n",
        encoding="utf-8",
    )
    pre_push.chmod(0o755)

    # Create .githooks/pre-push wrapper
    githooks_dir = root / ".githooks"
    githooks_dir.mkdir(parents=True, exist_ok=True)
    wrapper = githooks_dir / "pre-push"
    if not wrapper.exists():
        wrapper.write_text(
            "#!/usr/bin/env python3\n# N3RV pre-push hook\n# Add your checks here\n",
            encoding="utf-8",
        )
        wrapper.chmod(0o755)

    # Set core.hooksPath to .githooks
    subprocess.run(
        ["git", "config", "core.hooksPath", ".githooks"],
        cwd=root,
        capture_output=True,
    )
    return True


GITIGNORE_SECTION_HEADER = "# N3RV — NEVER COMMIT (template-controlled)"

GITIGNORE_PATTERNS: list[str] = [
    # Contents ignored, not the directory: git cannot re-include a file whose
    # parent directory is excluded, and the declared state is committed on
    # purpose — it is what a reviewer reads before the generator writes
    # anything. Every project's .gitignore already documents this split.
    ".n3rv/*",
    "!.n3rv/state.yaml",
    ".opencode/",
    ".githooks/",
    ".specify/",
    "AGENTS.md",
    # No longer scaffolded (see FILE_MANIFEST), but projects that ran init before
    # the removal still have a placeholder constitution.md lying around.
    "constitution.md",
    "opencode.json",
    "opencode.template.json",
]


def _gitignore_block() -> str:
    lines = [GITIGNORE_SECTION_HEADER] + GITIGNORE_PATTERNS + [""]
    return "\n".join(lines)


def _managed_block_bounds(lines: list[str]) -> tuple[int, int] | None:
    """Locate the generator-owned block: header line through the next blank line.

    ``configure_gitignore`` writes the block with a trailing blank line, so a
    blank line (or EOF) terminates it and preserves any project rules that
    follow. Returns ``(start, end)`` as a half-open slice, or ``None``.
    """
    for start, line in enumerate(lines):
        if line.strip() != GITIGNORE_SECTION_HEADER:
            continue
        end = start + 1
        while end < len(lines) and lines[end].strip():
            end += 1
        return start, end
    return None


def _write_gitignore(gitignore: Path, lines: list[str]) -> None:
    text = "\n".join(lines)
    if text and not text.endswith("\n"):
        text += "\n"
    gitignore.write_text(text, encoding="utf-8")


def configure_gitignore(root: Path) -> bool:
    """Ensure all n3rv-generated paths are in .gitignore, converging stale entries.

    Converges rather than blindly appending:

    - an existing n3rv block is rewritten in place when it is stale — e.g. the
      old ``.n3rv/`` directory entry that swallowed ``.n3rv/state.yaml``;
    - a project that already spells out the same rules by hand is left alone;
    - a bare ``.n3rv/`` line is rewritten to ``.n3rv/*`` before the negation is
      added, because git cannot re-include a file from an excluded directory.

    Idempotent: a file that already matches is untouched and returns ``False``.

    Args:
        root: Project root directory

    Returns:
        True if .gitignore was updated, False if skipped, already correct, or no
        git repo
    """
    git_dir = root / ".git"
    if not git_dir.is_dir():
        return False

    gitignore = root / ".gitignore"
    existing = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    lines = existing.splitlines()

    managed = [GITIGNORE_SECTION_HEADER] + GITIGNORE_PATTERNS
    bounds = _managed_block_bounds(lines)
    if bounds is not None:
        start, end = bounds
        if lines[start:end] == managed:
            return False
        lines[start:end] = managed
        _write_gitignore(gitignore, lines)
        return True

    # A project that already spells out the same rules by hand keeps its own
    # block: the file's convention wins over a second, possibly contradictory one.
    if set(GITIGNORE_PATTERNS) <= {line.strip() for line in lines}:
        return False

    # Rewrite a bare ``.n3rv/`` to ``.n3rv/*`` so the negation below actually
    # takes effect instead of being dead behind an excluded directory.
    lines = [".n3rv/*" if line.strip() == ".n3rv/" else line for line in lines]
    while lines and not lines[-1].strip():
        lines.pop()
    if lines:
        lines.append("")
    lines.extend(managed)
    _write_gitignore(gitignore, lines)
    return True


def managed_region(text: str) -> str | None:
    """Return the generator-owned marker block, if the file has one."""
    from n3rv.init.update import MARKER_END, MARKER_START

    start = text.find(MARKER_START)
    end = text.find(MARKER_END)
    if start == -1 or end == -1:
        return None
    return text[start : end + len(MARKER_END)]


def validate_markers(content: str) -> list[str]:
    """Check if content has valid marker pairs.

    Args:
        content: File content to check

    Returns:
        List of warning strings (empty if no issues)
    """
    from n3rv.init.update import MARKER_END, MARKER_START

    start_count = content.count(MARKER_START)
    end_count = content.count(MARKER_END)

    warnings: list[str] = []
    if start_count != end_count:
        warnings.append(f"Mismatched markers: {start_count} start-tags vs {end_count} end-tags")
    if start_count > 1:
        warnings.append(f"Multiple marker sections ({start_count} pairs) — only first pair guaranteed")
    if start_count == 0 and end_count == 0:
        return []

    return warnings
