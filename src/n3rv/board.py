"""Project board adapter — the single place that writes roadmap state.

Every SDD phase used to carry its own copy of a GraphQL snippet to find an item
and two ``gh project item-edit`` calls. The copies drifted, ignored pagination,
and skipped silently when an issue was not on the board. This module is the one
adapter: resolve the item with complete pagination, assert exactly one match,
update Status and SDD Phase together, and never skip without saying so.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any, Protocol

BOARD_OWNER = "The-Replacement"
BOARD_NUMBER = 1
BOARD_PROJECT_ID = "PVT_kwDOE1iio84BrhYz"
STATUS_FIELD_ID = "PVTSSF_lADOE1iio84BrhYzzhpzwA4"
PHASE_FIELD_ID = "PVTSSF_lADOE1iio84BrhYzzhpzwK4"

STATUS_OPTIONS: dict[str, str] = {
    "todo": "f75ad846",
    "in progress": "47fc9ee4",
    "done": "98236657",
}

PHASE_OPTIONS: dict[str, str] = {
    "todo": "66fa4f7d",
    "explore": "751f2d04",
    "propose": "33e3a016",
    "spec": "ceb6e6d2",
    "design": "c0dcb7b5",
    "tasks": "d2705ad5",
    "apply": "704d0a09",
    "verify": "ca959d10",
    "archive": "f94cb7b6",
}

#: SDD phase → (Status option name, SDD Phase option name).
PHASE_TRANSITIONS: dict[str, tuple[str, str]] = {
    "todo": ("todo", "todo"),
    "explore": ("in progress", "explore"),
    "propose": ("in progress", "propose"),
    "spec": ("in progress", "spec"),
    "design": ("in progress", "design"),
    "tasks": ("in progress", "tasks"),
    "apply": ("in progress", "apply"),
    "verify": ("in progress", "verify"),
    "archive": ("done", "archive"),
}

#: Every field/option id the generator owns. Anything else is stale.
CANONICAL_IDS: frozenset[str] = frozenset(
    {BOARD_PROJECT_ID, STATUS_FIELD_ID, PHASE_FIELD_ID, *STATUS_OPTIONS.values(), *PHASE_OPTIONS.values()}
)

_GITHUB_REMOTE_RE = re.compile(r"github\.com[:/](?P<owner>[^/]+)/(?P<repo>[^/\s]+?)(?:\.git)?$")
_GH_OVER_LIMIT = 100

_ITEMS_QUERY = """
query($owner: String!, $number: Int!, $cursor: String) {
  organization(login: $owner) {
    projectV2(number: $number) {
      id
      items(first: __PAGE_SIZE__, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          content {
            __typename
            ... on Issue {
              id
              number
              title
              repository { nameWithOwner }
            }
          }
          fieldValues(first: 20) {
            nodes {
              ... on ProjectV2ItemFieldSingleSelectValue {
                name
                optionId
                field { ... on ProjectV2FieldCommon { id name } }
              }
            }
          }
        }
      }
    }
  }
}
""".replace("__PAGE_SIZE__", str(_GH_OVER_LIMIT))

_UPDATE_MUTATION = """
mutation(
  $project: ID!
  $item: ID!
  $statusField: ID!
  $statusOption: String!
  $phaseField: ID!
  $phaseOption: String!
) {
  status: updateProjectV2ItemFieldValue(
    input: {projectId: $project, itemId: $item, fieldId: $statusField, value: {singleSelectOptionId: $statusOption}}
  ) { projectV2Item { id } }
  phase: updateProjectV2ItemFieldValue(
    input: {projectId: $project, itemId: $item, fieldId: $phaseField, value: {singleSelectOptionId: $phaseOption}}
  ) { projectV2Item { id } }
}
"""


class BoardError(Exception):
    """Board operation failed. *code* is the process exit code to use."""

    def __init__(self, message: str, code: int = 1, payload: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.payload = payload or {}


class GraphQLTransport(Protocol):
    """Minimal transport used by the board adapter (implemented by gh)."""

    def execute(self, query: str, variables: dict[str, Any]) -> dict[str, Any]: ...


class GhTransport:
    """Execute GraphQL through the authenticated ``gh`` CLI."""

    def __init__(self, gh: str = "gh", timeout: int = 60) -> None:
        self.gh = gh
        self.timeout = timeout

    def execute(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        cmd = [self.gh, "api", "graphql", "-f", f"query={query}"]
        for key, value in variables.items():
            if value is None:
                continue
            flag = "-F" if isinstance(value, int) and not isinstance(value, bool) else "-f"
            cmd.extend([flag, f"{key}={value}"])

        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout)
        except FileNotFoundError as exc:
            raise BoardError(f"{self.gh} is not installed or not on PATH", code=1) from exc
        except subprocess.TimeoutExpired as exc:
            raise BoardError(f"{self.gh} api graphql timed out after {self.timeout}s", code=1) from exc

        if proc.returncode != 0:
            raise BoardError(
                f"{self.gh} api graphql failed (exit {proc.returncode}): {proc.stderr.strip()}",
                code=1,
            )
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise BoardError(f"gh returned invalid JSON: {proc.stdout[:200]}", code=1) from exc

        if data.get("errors"):
            messages = "; ".join(str(err.get("message", err)) for err in data["errors"])
            raise BoardError(f"GraphQL error: {messages}", code=1)
        return data.get("data", {})


def render_context() -> dict[str, Any]:
    """Board constants for Jinja templates, so generated docs cannot drift."""
    return {
        "board_owner": BOARD_OWNER,
        "board_number": BOARD_NUMBER,
        "board_project_id": BOARD_PROJECT_ID,
        "board_status_field_id": STATUS_FIELD_ID,
        "board_phase_field_id": PHASE_FIELD_ID,
        "board_status_options": dict(STATUS_OPTIONS),
        "board_phase_options": dict(PHASE_OPTIONS),
        "board_phase_transitions": {
            phase: {"status": STATUS_OPTIONS[status], "phase": PHASE_OPTIONS[sdd_phase]}
            for phase, (status, sdd_phase) in PHASE_TRANSITIONS.items()
        },
    }


def parse_repo(value: str) -> str:
    """Validate and normalize an ``owner/repo`` string."""
    if value.count("/") != 1 or not all(part.strip() for part in value.split("/")):
        raise BoardError(f"invalid --repo value {value!r}; expected owner/repo", code=4)
    owner, repo = value.split("/")
    return f"{owner}/{repo}"


def detect_repo(root: Path) -> str | None:
    """Resolve owner/repo from the repository's origin remote."""
    try:
        proc = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    match = _GITHUB_REMOTE_RE.search(proc.stdout.strip())
    if not match:
        return None
    return f"{match.group('owner')}/{match.group('repo')}"


def _option(option_id: str) -> dict[str, str]:
    names = {value: key for key, value in STATUS_OPTIONS.items()}
    names.update({value: key for key, value in PHASE_OPTIONS.items()})
    return {"id": option_id, "name": names.get(option_id, "unknown")}


def _field_values(node: dict[str, Any]) -> dict[str, dict[str, str]]:
    """Map canonical field id → current single-select value."""
    current: dict[str, dict[str, str]] = {}
    nodes = (node.get("fieldValues") or {}).get("nodes") or []
    for value in nodes:
        if not value:
            continue
        field = value.get("field") or {}
        field_id = field.get("id")
        if field_id in (STATUS_FIELD_ID, PHASE_FIELD_ID) and value.get("optionId"):
            current[field_id] = {
                "id": value["optionId"],
                "name": value.get("name") or _option(value["optionId"])["name"],
            }
    return current


def _collect_items(transport: GraphQLTransport, owner: str, project_number: int) -> tuple[str, list[dict[str, Any]]]:
    """Page through every item. Returns (project id, items)."""
    items: list[dict[str, Any]] = []
    cursor: str | None = None
    project_id: str | None = None

    while True:
        data = transport.execute(_ITEMS_QUERY, {"owner": owner, "number": project_number, "cursor": cursor})
        project = (data.get("organization") or {}).get("projectV2")
        if not project:
            raise BoardError(
                f"project #{project_number} not found for owner {owner}",
                code=4,
            )
        project_id = project.get("id") or project_id
        page = project.get("items") or {}
        items.extend(node for node in (page.get("nodes") or []) if node)
        page_info = page.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        cursor = page_info.get("endCursor")
        if not cursor:
            raise BoardError("pagination reported another page without an end cursor", code=1)

    if not project_id:
        raise BoardError(f"project #{project_number} has no id", code=4)
    return project_id, items


def resolve_item(
    transport: GraphQLTransport,
    *,
    owner: str = BOARD_OWNER,
    project_number: int = BOARD_NUMBER,
    repo: str | None = None,
    issue: int | None = None,
    issue_node_id: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Resolve exactly one board item for an issue.

    Fails loudly with a distinct code when the issue is absent (3) or when more
    than one item matches (2). A silent skip is never a valid outcome.
    """
    if not issue_node_id and not (repo and issue is not None):
        raise BoardError("either --issue-id or both --repo and --issue are required", code=4)

    project_id, items = _collect_items(transport, owner, project_number)

    matches: list[dict[str, Any]] = []
    for node in items:
        content = node.get("content") or {}
        if content.get("__typename") not in (None, "Issue"):
            continue
        if issue_node_id:
            if content.get("id") == issue_node_id:
                matches.append(node)
            continue
        repository = (content.get("repository") or {}).get("nameWithOwner", "")
        if content.get("number") == issue and repository.lower() == (repo or "").lower():
            matches.append(node)

    if not matches:
        target = issue_node_id or f"{repo}#{issue}"
        raise BoardError(f"issue {target} is not on project #{project_number}", code=3)
    if len(matches) > 1:
        ids = ", ".join(node.get("id", "?") for node in matches)
        target = issue_node_id or f"{repo}#{issue}"
        raise BoardError(
            f"ambiguous board item for issue {target}: {len(matches)} matches ({ids})",
            code=2,
        )
    return project_id, matches[0]


def sync_issue(
    *,
    phase: str,
    repo: str | None = None,
    issue: int | None = None,
    issue_node_id: str | None = None,
    dry_run: bool = False,
    transport: GraphQLTransport | None = None,
    owner: str = BOARD_OWNER,
    project_number: int = BOARD_NUMBER,
) -> dict[str, Any]:
    """Update Status and SDD Phase together. Idempotent and explicit."""
    key = phase.strip().lower()
    if key not in PHASE_TRANSITIONS:
        allowed = ", ".join(sorted(PHASE_TRANSITIONS))
        raise BoardError(f"unknown SDD phase {phase!r}; allowed: {allowed}", code=4)

    if repo:
        repo = parse_repo(repo)
    client = transport or GhTransport()
    project_id, item = resolve_item(
        client,
        owner=owner,
        project_number=project_number,
        repo=repo,
        issue=issue,
        issue_node_id=issue_node_id,
    )

    status_name, phase_name = PHASE_TRANSITIONS[key]
    status_option = STATUS_OPTIONS[status_name]
    phase_option = PHASE_OPTIONS[phase_name]

    content = item.get("content") or {}
    current = _field_values(item)
    current_status = current.get(STATUS_FIELD_ID)
    current_phase = current.get(PHASE_FIELD_ID)

    changed: list[str] = []
    if not current_status or current_status.get("id") != status_option:
        changed.append("status")
    if not current_phase or current_phase.get("id") != phase_option:
        changed.append("phase")

    applied = False
    if changed and not dry_run:
        client.execute(
            _UPDATE_MUTATION,
            {
                "project": project_id,
                "item": item["id"],
                "statusField": STATUS_FIELD_ID,
                "statusOption": status_option,
                "phaseField": PHASE_FIELD_ID,
                "phaseOption": phase_option,
            },
        )
        applied = True

    return {
        "ok": True,
        "dry_run": dry_run,
        "applied": applied,
        "changed": changed,
        "project": {"owner": owner, "number": project_number, "id": project_id},
        "issue": {
            "repo": (content.get("repository") or {}).get("nameWithOwner") or repo,
            "number": content.get("number") if content.get("number") is not None else issue,
            "node_id": content.get("id") or issue_node_id,
            "title": content.get("title"),
        },
        "item_id": item.get("id"),
        "target": {
            "phase": key,
            "status": {"id": status_option, "name": status_name},
            "sdd_phase": {"id": phase_option, "name": phase_name},
        },
        "current": {
            "status": current_status,
            "sdd_phase": current_phase,
        },
    }
