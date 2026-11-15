"""Stack detection from project manifest files.

Detection answers one question: what is *this repository*? A manifest that
describes a package nested inside the repository (radio's ``src/mario``) must
not rename the repository, and a repository held together by compose files,
shell scripts and a web tree is ``generic`` even when one nested package has a
pyproject.
"""

from __future__ import annotations

import json
import logging
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from n3rv.init.analyzer.structure import has_web_tree
from n3rv.init.context import Stack

logger = logging.getLogger("n3rv.init.detector")

_COMPOSE_FILES = ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml")


@dataclass
class StackInfo:
    """Stack detection result."""

    stack: Stack
    project_name: str
    multi_stack: bool = False
    nested_package: str | None = None
    evidence: list[str] = field(default_factory=list)


def detect_stack(root: Path, stack_override: str | None = None) -> StackInfo:
    # Identity is derived from ``root.name`` below, and ``Path(".").name`` is
    # the empty string: an unresolved relative root ("--root .", or "..") would
    # yield an empty project name that only fails much later, inside pydantic
    # validation, as a traceback. Resolve once, here, where identity is born.
    root = root.resolve()
    manifest_stack = _detect_stack_from_manifests(root)
    declared_name = _extract_project_name(root)
    nested_package = _nested_package(root, declared_name)
    multi_stack = _is_multi_stack(root)

    if stack_override:
        stack = Stack(stack_override)
    elif nested_package and multi_stack:
        # The root manifest belongs to a package inside the repository; the
        # repository itself is the compose/shell/web project.
        stack = Stack.GENERIC
    else:
        stack = manifest_stack

    project_name = root.name if nested_package else (declared_name or root.name)
    evidence: list[str] = []
    if nested_package:
        evidence.append(f"nested package {nested_package!r} does not name the repository")
    if multi_stack:
        evidence.append("multi-stack layout (compose/shell/web)")

    return StackInfo(
        stack=stack,
        project_name=project_name,
        multi_stack=multi_stack,
        nested_package=nested_package,
        evidence=evidence,
    )


def _detect_stack_from_manifests(root: Path) -> Stack:
    if (root / "pyproject.toml").exists():
        return Stack.PYTHON
    if (root / "package.json").exists():
        return Stack.NODE
    if (root / "go.mod").exists():
        return Stack.GO
    return Stack.GENERIC


def _extract_project_name(root: Path) -> str | None:
    pyproject = root / "pyproject.toml"
    if pyproject.exists():
        name = _extract_from_pyproject(pyproject)
        if name:
            return name

    package_json = root / "package.json"
    if package_json.exists():
        name = _extract_from_package_json(package_json)
        if name:
            return name

    go_mod = root / "go.mod"
    if go_mod.exists():
        name = _extract_from_go_mod(go_mod)
        if name:
            return name

    return None


def _nested_package(root: Path, declared_name: str | None) -> str | None:
    """Return the nested package name when the manifest describes a subpackage.

    A manifest describes a nested package when its declared name differs from
    the repository directory and the code it packages lives under ``src/``.
    """
    if not declared_name or declared_name == root.name:
        return None

    candidates = {declared_name, declared_name.replace("-", "_")}
    if any((root / "src" / candidate).is_dir() for candidate in candidates):
        return declared_name

    declared_packages = _declared_package_paths(root)
    if any(path.replace("-", "_") in {f"src/{candidate}" for candidate in candidates} for path in declared_packages):
        return declared_name

    return None


def _declared_package_paths(root: Path) -> list[str]:
    pyproject = root / "pyproject.toml"
    if not pyproject.exists():
        return []
    try:
        with pyproject.open("rb") as handle:
            data = tomllib.load(handle)
    except Exception:
        logger.debug("Failed to parse pyproject.toml: %s", pyproject, exc_info=True)
        return []

    paths: list[str] = []
    hatch = data.get("tool", {}).get("hatch", {}).get("build", {}).get("targets", {}).get("wheel", {})
    if isinstance(hatch, dict) and isinstance(hatch.get("packages"), list):
        paths.extend(str(entry) for entry in hatch["packages"])

    setuptools = data.get("tool", {}).get("setuptools", {})
    if isinstance(setuptools, dict) and isinstance(setuptools.get("package-dir"), dict):
        paths.extend(str(value) for value in setuptools["package-dir"].values())
    return paths


def _is_multi_stack(root: Path) -> bool:
    """Compose + shell + web signals: the repository is bigger than one stack."""
    signals = 0
    if any((root / name).is_file() for name in _COMPOSE_FILES):
        signals += 1
    if any(root.glob("*.sh")) or ((root / "scripts").is_dir() and any((root / "scripts").glob("*.sh"))):
        signals += 1
    if has_web_tree(root):
        signals += 1
    return signals >= 2


def _extract_from_pyproject(path: Path) -> str | None:
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
        if "project" in data and "name" in data["project"]:
            return data["project"]["name"]
        if "tool" in data and "poetry" in data["tool"] and "name" in data["tool"]["poetry"]:
            return data["tool"]["poetry"]["name"]
    except Exception:
        logger.debug("Failed to parse pyproject.toml: %s", path, exc_info=True)
    return None


def _extract_from_package_json(path: Path) -> str | None:
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        name = data.get("name")
        if name:
            if name.startswith("@") and "/" in name:
                name = name.split("/", 1)[1]
            return name
    except Exception:
        logger.debug("Failed to parse package.json: %s", path, exc_info=True)
    return None


def _extract_from_go_mod(path: Path) -> str | None:
    try:
        content = path.read_text(encoding="utf-8")
        match = re.match(r"^\s*module\s+(.+)", content, re.MULTILINE)
        if match:
            module_path = match.group(1).strip()
            return module_path.split("/")[-1]
    except Exception:
        logger.debug("Failed to parse go.mod: %s", path, exc_info=True)
    return None
