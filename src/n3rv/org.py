"""Org-level configuration for The-Replacement multi-project workspaces."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from n3rv.models.a2a import AgentCapabilities, AgentSkill, NervAgentCard

logger = logging.getLogger("n3rv.org")

ORG_CONFIG_FILENAME = "org-config.yaml"


class OrgNotFoundError(Exception):
    """Raised when no .n3rv/org-config.yaml is found."""


class OrgProject(BaseModel):
    """A single project registered in the org."""

    model_config = ConfigDict(frozen=True)

    name: str
    path: Path
    description: str = ""
    type: Literal["hub", "satellite", "tool"] = "satellite"
    repo_url: str | None = None


class OrgConfig(BaseModel):
    """Org-level configuration for a multi-project workspace."""

    org_name: str = "The-Replacement"
    projects: list[OrgProject] = []
    config: dict[str, Any] = Field(
        default_factory=lambda: {
            "shared_skills_dir": ".opencode/shared/skills",
            "satellites_dir": ".",
        }
    )

    @field_validator("projects")
    @classmethod
    def _no_duplicate_names(cls, v: list[OrgProject]) -> list[OrgProject]:
        names = [p.name for p in v]
        if len(names) != len(set(names)):
            raise ValueError(f"Duplicate project names: {names}")
        return v

    @classmethod
    def from_yaml(cls, path: Path) -> OrgConfig:
        """Load org config from a YAML file."""
        if not path.exists():
            raise OrgNotFoundError(f"Org config not found: {path}")
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError(f"Invalid org config in {path}: expected a mapping")
        return cls.model_validate(raw)

    def to_yaml(self, path: Path) -> None:
        """Write org config to a YAML file."""
        path.parent.mkdir(parents=True, exist_ok=True)
        data = self.model_dump(mode="python")
        # Convert Path objects to strings for YAML serialization
        for p in data.get("projects", []):
            p["path"] = str(p["path"])
        path.write_text(yaml.safe_dump(data), encoding="utf-8")

    def discover_satellite_cards(self, config_dir: Path | None = None) -> list[NervAgentCard]:
        """Discover agent cards for all satellite projects."""
        cards: list[NervAgentCard] = []
        base = config_dir or Path.cwd()
        for project in self.projects:
            if project.type != "satellite":
                continue
            project_path = (base / project.path).resolve()
            a2a_config = project_path / ".n3rv" / "a2a-config.yaml"
            if not a2a_config.exists():
                logger.debug("No a2a-config.yaml for %s at %s", project.name, a2a_config)
                continue
            try:
                raw = yaml.safe_load(a2a_config.read_text(encoding="utf-8")) or {}
                hub = raw.get("hub", {})
                host = hub.get("host", "127.0.0.1")
                port = hub.get("port", 19820)
                card = NervAgentCard(
                    name=f"n3rv-{project.name}",
                    description=project.description or f"{project.name} satellite agent",
                    url=f"http://{host}:{port}",
                    version="0.1.0",
                    capabilities=AgentCapabilities(streaming=False),
                    skills=[
                        AgentSkill(
                            id=f"satellite-{project.name}",
                            name=project.name,
                            description=project.description or f"{project.name} operations",
                        ),
                    ],
                )
                cards.append(card)
            except Exception as exc:
                logger.warning("Failed to load agent card for %s: %s", project.name, exc)
                continue
        return cards


def protect_repo(repo_url: str, dry_run: bool = False) -> bool:
    """Apply branch protection to a GitHub repository's main branch.

    Uses ``gh api`` to detect CI workflow checks and apply:
    - Required status checks from CI jobs
    - Required PR reviews (1 approval, dismiss stale)
    - Admin enforcement

    Args:
        repo_url: Full GitHub repo URL (e.g. https://github.com/The-Replacement/hub)
        dry_run: If True, print what would be done without applying.

    Returns:
        True if protection was applied or would be applied, False on error.
    """
    import json
    import subprocess

    # Parse owner/name from URL
    # Supports: https://github.com/owner/name, git@github.com:owner/name.git
    repo_url_s = str(repo_url)
    if "github.com/" in repo_url_s:
        parts = repo_url_s.rstrip("/").split("github.com/")[-1]
    elif "github.com:" in repo_url_s:
        parts = repo_url_s.split("github.com:")[-1].rstrip(".git")
    else:
        print(f"  ✗ Cannot parse repo URL: {repo_url}")
        return False

    owner_repo = parts.rstrip(".git")
    api_base = f"repos/{owner_repo}"

    # Detect CI workflow checks
    checks: list[dict[str, object]] = []
    workflows_check = subprocess.run(
        ["gh", "api", f"{api_base}/actions/workflows"],
        capture_output=True,
        text=True,
    )
    if workflows_check.returncode == 0:
        try:
            workflows = json.loads(workflows_check.stdout).get("workflows", [])
            ci_workflows = [w for w in workflows if w.get("path", "").endswith("ci.yml")]
            for wf in ci_workflows:
                wf_name = wf.get("name", "CI")
                wf_content = subprocess.run(
                    ["gh", "api", f"{api_base}/contents/{wf['path']}"],
                    capture_output=True,
                    text=True,
                )
                if wf_content.returncode == 0:
                    try:
                        import base64

                        content = base64.b64decode(json.loads(wf_content.stdout).get("content", "")).decode()
                        import yaml as yaml_lib  # noqa: N812 — same yaml dep used elsewhere

                        parsed = yaml_lib.safe_load(content)
                        jobs = (parsed or {}).get("jobs", {})
                        for job_id, job_def in jobs.items():
                            if not isinstance(job_def, dict):
                                continue
                            check_name = job_def.get("name", job_id)
                            checks.append({"context": f"{wf_name} / {check_name}", "app_id": 15368})
                    except Exception:
                        logger.debug("Failed to parse workflow %s", wf_name, exc_info=True)
        except Exception:
            logger.debug("Failed to load workflows for %s", api_base, exc_info=True)

    if not checks:
        # No CI found — still protect with PR reviews
        print(f"  ~ {owner_repo}: no CI checks, PR-only protection")

    payload = {
        "required_status_checks": {
            "strict": True,
            "checks": checks,
        }
        if checks
        else None,
        "enforce_admins": True,
        "required_pull_request_reviews": {
            "required_approving_review_count": 0,
            "dismiss_stale_reviews": True,
            "require_code_owner_review": False,
        },
        "restrictions": None,
    }

    if dry_run:
        check_names = [c["context"] for c in checks] if checks else []
        print(f"  → {owner_repo}: would protect with checks={check_names}")
        return True

    result = subprocess.run(
        ["gh", "api", f"{api_base}/branches/main/protection", "--method", "PUT", "--input", "-"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        check_names = [c["context"] for c in checks] if checks else []
        print(f"  ✓ {owner_repo}: protected with checks={check_names}")
        return True
    else:
        print(f"  ✗ {owner_repo}: failed ({result.stderr.strip()})")
        return False


DEFAULT_RULESET_NAME = "protect-default"


class RulesetError(Exception):
    """Raised when a ruleset cannot be derived from observable repository state."""


def _parse_owner_repo(repo: object) -> str:
    """Return ``owner/name`` from a GitHub URL, an scp-style remote or a bare slug."""
    raw = str(repo).strip().rstrip("/")
    if "github.com/" in raw:
        raw = raw.split("github.com/")[-1]
    elif "github.com:" in raw:
        raw = raw.split("github.com:")[-1]
    raw = raw.removesuffix(".git")
    parts = raw.split("/")
    if len(parts) != 2 or not all(parts):
        raise RulesetError(f"Cannot parse repository {repo!r}; expected owner/name or a GitHub URL")
    return raw


def _gh_json(*args: str) -> object:
    """Run ``gh api <args>`` and decode stdout; any failure is a RulesetError."""
    import json
    import subprocess

    result = subprocess.run(["gh", "api", *args], capture_output=True, text=True)
    if result.returncode != 0:
        raise RulesetError(f"gh api {args[0]} failed: {result.stderr.strip() or f'exit {result.returncode}'}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RulesetError(f"gh api {args[0]} returned non-JSON output") from exc


def default_branch(owner_repo: str) -> str:
    """Return the repository's GitHub default branch."""
    data = _gh_json(f"repos/{owner_repo}")
    branch = data.get("default_branch") if isinstance(data, dict) else None
    if not isinstance(branch, str) or not branch:
        raise RulesetError(f"{owner_repo}: GitHub did not report a default branch")
    return branch


def ci_check_contexts(owner_repo: str, branch: str) -> list[str]:
    """Return the check names GitHub actually reports for the branch head.

    Read from check runs, never from the workflow YAML: a matrix job publishes one
    context per leg (``Test (Python 3.12)``) and a renamed job publishes its name,
    so parsing the file invents contexts that never report and the ruleset then
    requires nothing that exists.
    """
    data = _gh_json(f"repos/{owner_repo}/commits/{branch}/check-runs?per_page=100")
    runs = data.get("check_runs", []) if isinstance(data, dict) else []
    names = sorted(
        {run["name"] for run in runs if isinstance(run, dict) and isinstance(run.get("name"), str) and run["name"]}
    )
    if not names:
        raise RulesetError(
            f"{owner_repo}@{branch}: no check runs to require; a ruleset that requires nothing gates nothing"
        )
    return names


def build_ruleset_payload(name: str, contexts: list[str]) -> dict[str, object]:
    """Build the ruleset body: default-branch protection with the repo's own checks."""
    return {
        "name": name,
        "target": "branch",
        "enforcement": "active",
        "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
        "bypass_actors": [],
        "rules": [
            {"type": "deletion"},
            {"type": "non_fast_forward"},
            {
                "type": "pull_request",
                "parameters": {
                    "required_approving_review_count": 0,
                    "dismiss_stale_reviews_on_push": True,
                    "require_code_owner_review": False,
                    "require_last_push_approval": False,
                    "required_review_thread_resolution": False,
                },
            },
            {
                "type": "required_status_checks",
                "parameters": {
                    "strict_required_status_checks_policy": True,
                    "do_not_enforce_on_create": False,
                    "required_status_checks": [{"context": context} for context in contexts],
                },
            },
        ],
    }


def _ruleset_required_contexts(ruleset: dict[str, object]) -> list[str]:
    """Return the status-check contexts a ruleset requires, sorted."""
    rules = ruleset.get("rules")
    for rule in rules if isinstance(rules, list) else []:
        if not isinstance(rule, dict) or rule.get("type") != "required_status_checks":
            continue
        parameters = rule.get("parameters")
        checks = parameters.get("required_status_checks") if isinstance(parameters, dict) else None
        contexts = [c.get("context") for c in checks if isinstance(c, dict)] if isinstance(checks, list) else []
        return sorted(context for context in contexts if isinstance(context, str))
    return []


def _find_ruleset(owner_repo: str, name: str) -> dict[str, object] | None:
    """Return the named ruleset with its full detail, or None."""
    listed = _gh_json(f"repos/{owner_repo}/rulesets")
    for entry in listed if isinstance(listed, list) else []:
        if isinstance(entry, dict) and entry.get("name") == name:
            detail = _gh_json(f"repos/{owner_repo}/rulesets/{entry.get('id')}")
            return detail if isinstance(detail, dict) else None
    return None


def install_ruleset(repo: object, name: str = DEFAULT_RULESET_NAME, dry_run: bool = False) -> bool:
    """Install or update a default-branch ruleset that requires the repo's real CI checks.

    Idempotent by name: an existing ruleset already active with the same contexts
    is reported unchanged and not written. Repositories with no observable check
    runs are refused rather than protected by an empty requirement.

    Args:
        repo: ``owner/name``, a GitHub URL, or an scp-style remote.
        name: Ruleset name; the identity used for the idempotent lookup.
        dry_run: Print the resolved payload without calling the write API.

    Returns:
        True when the ruleset is (or already was) active, False otherwise.
    """
    import json
    import subprocess

    try:
        owner_repo = _parse_owner_repo(repo)
        branch = default_branch(owner_repo)
        contexts = ci_check_contexts(owner_repo, branch)
    except RulesetError as exc:
        print(f"  ✗ {exc}")
        return False

    payload = build_ruleset_payload(name=name, contexts=contexts)

    if dry_run:
        print(f"  → {owner_repo}: would install ruleset {name!r} on {branch} with checks={contexts}")
        print(json.dumps(payload, indent=2, sort_keys=True))
        return True

    try:
        existing = _find_ruleset(owner_repo, name)
    except RulesetError as exc:
        print(f"  ✗ {exc}")
        return False

    if existing is not None:
        have = _ruleset_required_contexts(existing)
        if existing.get("enforcement") == "active" and have == contexts:
            print(f"  = {owner_repo}: ruleset {name!r} already active with checks={have}")
            return True
        url = f"repos/{owner_repo}/rulesets/{existing.get('id')}"
        verb = "updated"
    else:
        url = f"repos/{owner_repo}/rulesets"
        verb = "installed"

    result = subprocess.run(
        ["gh", "api", url, "--method", "PUT" if existing is not None else "POST", "--input", "-"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"  ✗ {owner_repo}: ruleset {name!r} not {verb} ({result.stderr.strip()})")
        return False
    print(f"  ✓ {owner_repo}: ruleset {name!r} {verb} with checks={contexts}")
    return True


def resolve_org_root(start: Path | None = None) -> Path:
    """Walk up from *start* looking for .n3rv/org-config.yaml.

    Raises OrgNotFoundError if no config is found.
    """
    current = (start or Path.cwd()).resolve()
    for ancestor in [current] + list(current.parents):
        config_path = ancestor / ".n3rv" / ORG_CONFIG_FILENAME
        if config_path.exists():
            return ancestor
    raise OrgNotFoundError(
        f"No .n3rv/{ORG_CONFIG_FILENAME} found from {start or Path.cwd()}. Run 'n3rv org init' first."
    )
