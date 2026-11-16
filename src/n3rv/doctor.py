"""Read-only convergence diagnostics for ``n3rv doctor``.

Every check answers one question about drift between what the generator wrote,
what the repository has, and what the outside world exposes. Nothing here
writes to the repository or to GitHub.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from n3rv import board

#: One exit-code bit per category. A single failing category yields its own
#: distinct code; several failing categories OR their bits together.
CATEGORY_CODES: dict[str, int] = {
    "drift": 2,
    "models": 4,
    "mcp": 8,
    "convergence": 16,
    "hooks": 32,
}

_DEFAULT_GLOBAL_CONFIG = Path.home() / ".config" / "opencode" / "opencode.json"
_TEMPLATES_DIR = Path(__file__).parent / "init" / "templates"

_MODEL_LINE_RE = re.compile(r"^model:\s*(\S+)\s*$", re.MULTILINE)
_PROJECT_NUMBER_RE = re.compile(r"projectV2\(number:\s*(\d+)")
_PROJECT_HASH_RE = re.compile(r"project\s*#(\d+)")
_BOARD_ID_RE = re.compile(r"\bPVT[A-Za-z0-9_]+\b")

_MCP_STATUS_RE = re.compile(r"^\s*([^\w\s])\s+(\S+)\s+(.*)$")

_HOOK_RISKS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bgit\s+fetch\b"), "fetches remote refs"),
    (re.compile(r"\bgit\s+pull\b"), "pulls remote refs"),
    (re.compile(r"\bgit\s+push\b"), "pushes from a hook"),
    (re.compile(r"\bgit\s+remote\s+(?:update|prune)\b"), "rewrites remote-tracking refs"),
    (re.compile(r"\bgit\s+update-ref\b.*refs/remotes/"), "rewrites remote-tracking refs"),
]


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


def default_runner(cmd: list[str], cwd: Path | None = None, timeout: int = 30) -> CommandResult:
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return CommandResult(127, "", f"{cmd[0]} not found")
    except subprocess.TimeoutExpired:
        return CommandResult(124, "", f"{cmd[0]} timed out after {timeout}s")
    return CommandResult(proc.returncode, proc.stdout, proc.stderr)


Runner = Callable[..., CommandResult]


@dataclass(frozen=True)
class Finding:
    level: str
    message: str
    context: dict[str, Any] = field(default_factory=dict)


def _ok(message: str, **context: Any) -> Finding:
    return Finding("ok", message, context)


def _fail(message: str, **context: Any) -> Finding:
    return Finding("fail", message, context)


def _warn(message: str, **context: Any) -> Finding:
    return Finding("warn", message, context)


def _skip(message: str, **context: Any) -> Finding:
    return Finding("skip", message, context)


@dataclass
class CategoryResult:
    name: str
    findings: list[Finding]

    @property
    def failed(self) -> bool:
        return any(finding.level == "fail" for finding in self.findings)

    def strict(self) -> CategoryResult:
        """Treat every unverified check as a failure.

        A skip means "this could not be checked", which is honest but not
        clean. A category that skipped the mainframe leg has proven nothing
        about the mainframe, so strict mode refuses to call it a pass.
        """
        if not any(finding.level == "skip" for finding in self.findings):
            return self
        return CategoryResult(
            self.name,
            [
                Finding("fail", f"unverified: {finding.message}", finding.context)
                if finding.level == "skip"
                else finding
                for finding in self.findings
            ],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": not self.failed,
            "code": CATEGORY_CODES[self.name],
            "findings": [
                {"level": finding.level, "message": finding.message, "context": finding.context}
                for finding in self.findings
            ],
        }


# ── drift ─────────────────────────────────────────────────────────────


def _git(runner: Runner, root: Path, *args: str) -> CommandResult:
    return runner(["git", *args], cwd=root, timeout=20)


def check_drift(
    root: Path,
    runner: Runner,
    *,
    repo: str | None = None,
    mainframe_remote: str = "mainframe",
    deployment_target: str = "mainframe",
) -> CategoryResult:
    """Workstation HEAD vs live GitHub default branch vs mainframe HEAD."""
    findings: list[Finding] = []
    if not (root / ".git").exists():
        return CategoryResult("drift", [_skip("not a git repository", root=str(root))])

    head = _git(runner, root, "rev-parse", "HEAD")
    if head.returncode != 0:
        return CategoryResult("drift", [_skip("repository has no HEAD commit", detail=head.stderr.strip())])
    workstation = head.stdout.strip()
    branch_result = _git(runner, root, "rev-parse", "--abbrev-ref", "HEAD")
    branch = branch_result.stdout.strip() if branch_result.returncode == 0 else ""
    findings.append(
        _ok(
            f"workstation HEAD {workstation[:12]}"
            + (f" on {branch}" if branch and branch != "HEAD" else " (detached HEAD)"),
            sha=workstation,
            branch=branch,
        )
    )

    resolved_repo = repo
    if resolved_repo is None:
        remote = _git(runner, root, "remote", "get-url", "origin")
        match = re.search(r"github\.com[:/]([^/]+/[^/\s]+?)(?:\.git)?$", remote.stdout.strip())
        if remote.returncode == 0 and match:
            resolved_repo = match.group(1)

    github_sha: str | None = None
    if not resolved_repo:
        findings.append(_skip("cannot resolve owner/repo for the GitHub check"))
    else:
        meta = runner(["gh", "api", f"repos/{resolved_repo}", "--jq", ".default_branch"], cwd=root, timeout=30)
        if meta.returncode != 0 or not meta.stdout.strip():
            findings.append(_skip(f"GitHub API unavailable for {resolved_repo}", detail=meta.stderr.strip()))
        else:
            default_branch = meta.stdout.strip()
            commits = runner(
                ["gh", "api", f"repos/{resolved_repo}/commits/{default_branch}", "--jq", ".sha"],
                cwd=root,
                timeout=30,
            )
            if commits.returncode != 0 or not commits.stdout.strip():
                findings.append(
                    _skip(
                        f"cannot read {resolved_repo}@{default_branch}",
                        detail=commits.stderr.strip(),
                    )
                )
            else:
                github_sha = commits.stdout.strip()
                on_default = bool(default_branch) and branch == default_branch
                if github_sha == workstation:
                    level = "ok"
                    message = f"GitHub {resolved_repo}@{default_branch} is {github_sha[:12]}"
                elif on_default:
                    level = "fail"
                    message = (
                        f"GitHub {resolved_repo}@{default_branch} is {github_sha[:12]} "
                        f"(workstation is {workstation[:12]})"
                    )
                else:
                    # A feature branch is expected to be ahead of the default
                    # branch; report it without failing convergence.
                    level = "warn"
                    message = (
                        f"workstation is on {branch or 'a detached HEAD'} ({workstation[:12]}); "
                        f"GitHub {resolved_repo}@{default_branch} is {github_sha[:12]}"
                    )
                findings.append(
                    Finding(
                        level,
                        message,
                        {"repo": resolved_repo, "branch": default_branch, "sha": github_sha},
                    )
                )

    mainframe = _git(runner, root, "remote", "get-url", mainframe_remote)
    if deployment_target == "none":
        findings.append(_ok("deployment.target is none; mainframe leg skipped by declaration"))
        if mainframe.returncode == 0:
            findings.append(_warn(f"{mainframe_remote} remote is configured but deployment.target is none"))
    elif mainframe.returncode != 0:
        findings.append(
            _skip(
                f"no {mainframe_remote} remote configured",
                hint="add the remote, pass --mainframe-remote, or use --strict to fail on unverified checks",
            )
        )
    else:
        ls_remote = _git(runner, root, "ls-remote", "--exit-code", mainframe_remote, "HEAD")
        if ls_remote.returncode != 0 or not ls_remote.stdout.strip():
            findings.append(
                _skip(
                    f"{mainframe_remote} remote is unreachable",
                    detail=ls_remote.stderr.strip(),
                )
            )
        else:
            mainframe_sha = ls_remote.stdout.split()[0]
            level = "ok" if github_sha is None or mainframe_sha == github_sha else "fail"
            findings.append(
                Finding(
                    level,
                    f"{mainframe_remote} HEAD is {mainframe_sha[:12]}"
                    + ("" if level == "ok" else f" (GitHub is {(github_sha or '')[:12]})"),
                    {"remote": mainframe_remote, "sha": mainframe_sha},
                )
            )

    return CategoryResult("drift", findings)


# ── models ────────────────────────────────────────────────────────────


def _template_model_ids(templates_dir: Path) -> dict[str, list[str]]:
    ids: dict[str, list[str]] = {}
    for path in sorted(templates_dir.rglob("*.j2")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for model_id in _MODEL_LINE_RE.findall(text):
            if "/" not in model_id:
                continue
            ids.setdefault(model_id, []).append(str(path.relative_to(templates_dir)))
    return ids


def _phase_map_model_ids() -> dict[str, list[str]]:
    from n3rv.init import SDD_PHASE_MAP

    ids: dict[str, list[str]] = {}
    for slug, data in SDD_PHASE_MAP.items():
        model_id = data.get("phase_model")
        if model_id:
            ids.setdefault(model_id, []).append(f"SDD_PHASE_MAP[{slug}]")
    return ids


def check_models(
    root: Path,
    runner: Runner,
    *,
    templates_dir: Path | None = None,
) -> CategoryResult:
    """Agent model ids in the templates vs the provider catalog."""
    templates = templates_dir or _TEMPLATES_DIR
    used: dict[str, list[str]] = {}
    for source in (_phase_map_model_ids(), _template_model_ids(templates)):
        for model_id, origins in source.items():
            used.setdefault(model_id, []).extend(origins)

    if not used:
        return CategoryResult("models", [_skip("no concrete model ids found in the templates")])

    catalog = runner(["opencode", "models"], cwd=root, timeout=90)
    available = {line.strip() for line in catalog.stdout.splitlines() if line.strip()}
    if catalog.returncode != 0 or not available:
        return CategoryResult(
            "models",
            [
                _skip(
                    "provider catalog unavailable (`opencode models` failed)",
                    detail=catalog.stderr.strip(),
                    used=sorted(used),
                )
            ],
        )

    findings: list[Finding] = []
    missing = {model_id: origins for model_id, origins in used.items() if model_id not in available}
    for model_id, origins in sorted(missing.items()):
        findings.append(_fail(f"model {model_id} is not exposed by the provider catalog", sources=origins))
    if not missing:
        findings.append(_ok(f"all {len(used)} template model ids exist in the catalog", models=sorted(used)))
    return CategoryResult("models", findings)


# ── mcp ───────────────────────────────────────────────────────────────


def _load_config_servers(path: Path) -> dict[str, dict[str, Any]]:
    """Return MCP servers from a project/global config, V1 or V2 layout."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    mcp = data.get("mcp")
    if not isinstance(mcp, dict):
        return {}
    servers: dict[str, dict[str, Any]] = {}
    nested = mcp.get("servers")
    if isinstance(nested, dict):
        servers.update({name: value for name, value in nested.items() if isinstance(value, dict)})
    for name, value in mcp.items():
        if name in ("servers", "timeout"):
            continue
        if isinstance(value, dict) and "type" in value:
            servers[name] = value
    return servers


def _unpinned_packages(server: dict[str, Any]) -> list[str]:
    command = server.get("command")
    if not isinstance(command, list):
        return []
    unpinned = []
    for arg in command:
        if isinstance(arg, str) and "@latest" in arg:
            unpinned.append(arg)
    return unpinned


def _mcp_statuses(runner: Runner, root: Path) -> dict[str, str] | None:
    result = runner(["opencode", "mcp", "list"], cwd=root, timeout=60)
    if result.returncode != 0:
        return None
    statuses: dict[str, str] = {}
    for line in result.stdout.splitlines():
        match = _MCP_STATUS_RE.match(line)
        if not match:
            continue
        statuses[match.group(2)] = match.group(3).strip()
    return statuses


def _connection_state(status: str) -> str:
    """Classify an ``opencode mcp list`` status string."""
    lowered = status.lower()
    if "connected" in lowered and "not connected" not in lowered:
        return "connected"
    if "pending" in lowered or "starting" in lowered or "checking" in lowered:
        return "pending"
    return "error"


def check_mcp(
    root: Path,
    runner: Runner,
    *,
    global_config: Path | None = None,
) -> CategoryResult:
    """Overrides, unpinned packages, and servers that are not connected."""
    findings: list[Finding] = []
    project_path = root / "opencode.json"
    project = _load_config_servers(project_path)
    global_path = global_config or _DEFAULT_GLOBAL_CONFIG
    global_servers = _load_config_servers(global_path) if global_path.is_file() else {}

    for name in sorted(set(project) & set(global_servers)):
        if project[name] == global_servers[name] and name not in {"github", "context7"}:
            continue
        message = f"project server {name} overrides the global definition"
        # github/context7 are secured globally: a project copy always shadows the
        # protected binary. Other names may be project-scoped recommendations.
        finding = _fail(message, server=name) if name in {"github", "context7"} else _warn(message, server=name)
        findings.append(finding)

    for name in sorted(project):
        for package in _unpinned_packages(project[name]):
            findings.append(_fail(f"server {name} uses an unpinned package: {package}", server=name))

    statuses = _mcp_statuses(runner, root)
    if statuses is None:
        findings.append(_skip("cannot read MCP connection status (`opencode mcp list` failed)"))
    else:
        pending: list[str] = []
        for name in sorted(project):
            status = statuses.get(name)
            if status is None:
                continue
            state = _connection_state(status)
            if state == "connected":
                continue
            if state == "pending":
                pending.append(name)
                continue
            findings.append(_fail(f"project server {name} is not connected: {status}", server=name))
        for name, status in sorted(statuses.items()):
            if name in project:
                continue
            if _connection_state(status) == "error":
                findings.append(_warn(f"global server {name} is not connected: {status}", server=name))
        if pending:
            findings.append(
                _skip(
                    "connection state is inconclusive for: " + ", ".join(pending),
                    servers=pending,
                )
            )

    if not findings:
        findings.append(_ok(f"{len(project)} project MCP servers are clean", servers=sorted(project)))
    return CategoryResult("mcp", findings)


# ── convergence ───────────────────────────────────────────────────────


def _generated_markdown(root: Path) -> list[Path]:
    paths: list[Path] = []
    for base, pattern in (
        (root / ".opencode" / "agents", "*.md"),
        (root / ".opencode" / "commands", "*.md"),
        (root / ".opencode" / "skills", "**/SKILL.md"),
    ):
        if base.is_dir():
            paths.extend(sorted(base.glob(pattern)))
    return paths


def _board_reference_findings(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for path in _generated_markdown(root):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        relative = str(path.relative_to(root))
        for number in _PROJECT_NUMBER_RE.findall(text) + _PROJECT_HASH_RE.findall(text):
            if int(number) != board.BOARD_NUMBER:
                findings.append(
                    _fail(
                        f"{relative} references project #{number}, canonical is #{board.BOARD_NUMBER}",
                        file=relative,
                    )
                )
        for identifier in set(_BOARD_ID_RE.findall(text)):
            if identifier not in board.CANONICAL_IDS:
                findings.append(_fail(f"{relative} references stale board id {identifier}", file=relative))
    return findings


def _template_revision_findings(root: Path, templates_dir: Path | None = None) -> list[Finding]:
    from n3rv.init.lockfile import load_lockfile

    lock = load_lockfile(root)
    entries = lock.get("entries") or {}
    if not entries:
        return [_warn("no template lockfile entries; run n3rv update")]

    templates = templates_dir or _TEMPLATES_DIR
    findings: list[Finding] = []
    untraced = 0
    traced = 0
    for output_path, entry in sorted(entries.items()):
        template_name = (entry or {}).get("template") or ""
        recorded = (entry or {}).get("template_sha256")
        if not template_name:
            untraced += 1
            continue
        template_path = templates / template_name
        if not template_path.is_file():
            findings.append(_fail(f"{output_path} is tracked against a missing template {template_name}"))
            continue
        current = hashlib.sha256(template_path.read_bytes()).hexdigest()
        if recorded and recorded != current:
            findings.append(
                _fail(
                    f"{output_path} was generated from an older revision of {template_name}",
                    file=output_path,
                    template=template_name,
                )
            )
        elif not recorded:
            # No recorded revision: the entry predates provenance recording or
            # was written by a path that skipped it. That is not a mismatch —
            # claiming it matched would be the lie the old ✓ line told — but
            # it is not traceable either (n3rv#17).
            untraced += 1
        else:
            traced += 1

    if not findings:
        if traced:
            findings.append(_ok(f"all {traced} traced file(s) match their template revision"))
        elif entries:
            findings.append(_warn("no tracked file records a template revision; run n3rv apply to trace them"))
    if untraced:
        findings.append(_warn(f"{untraced} tracked file(s) are untraced: no recorded template revision"))
    return findings


def check_convergence(root: Path, *, templates_dir: Path | None = None) -> CategoryResult:
    """Stale board references and template revisions behind generated output."""
    if not (root / ".opencode").is_dir() and not (root / ".n3rv").is_dir():
        return CategoryResult("convergence", [_skip("project has no generated .opencode/.n3rv tree")])

    findings = _board_reference_findings(root) + _template_revision_findings(root, templates_dir)
    if not findings:
        findings.append(_ok("generated files converge with the templates"))
    return CategoryResult("convergence", findings)


# ── hooks ─────────────────────────────────────────────────────────────


def _hook_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for directory in (root / ".githooks", root / ".git" / "hooks"):
        if not directory.is_dir():
            continue
        for path in sorted(directory.iterdir()):
            if path.is_file() and not path.name.endswith(".sample"):
                files.append(path)
    return files


def _hook_code_lines(text: str) -> list[tuple[int, str]]:
    lines = []
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        lines.append((number, line))
    return lines


def check_hooks(root: Path) -> CategoryResult:
    """Detect hooks that would rewrite remote-tracking refs behind our back."""
    files = _hook_files(root)
    if not files:
        return CategoryResult("hooks", [_skip("no receive/pre-push hooks installed")])

    findings: list[Finding] = []
    risky = 0
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        relative = str(path.relative_to(root))
        hits = []
        for number, line in _hook_code_lines(text):
            for pattern, reason in _HOOK_RISKS:
                if pattern.search(line):
                    hits.append({"line": number, "reason": reason, "code": line.strip()})
                    break
        if hits:
            risky += 1
            findings.append(
                _fail(
                    f"{relative} rewrites remote-tracking refs ({', '.join(sorted({hit['reason'] for hit in hits}))})",
                    file=relative,
                    hits=hits,
                )
            )
        else:
            findings.append(_ok(f"{relative} does not rewrite remote-tracking refs", file=relative))

    if risky == 0:
        findings.append(_ok(f"checked {len(files)} hook file(s)"))
    return CategoryResult("hooks", findings)


# ── orchestration ─────────────────────────────────────────────────────


def run_doctor(
    root: Path,
    *,
    repo: str | None = None,
    mainframe_remote: str = "mainframe",
    global_config: Path | None = None,
    templates_dir: Path | None = None,
    runner: Runner | None = None,
    json_output: bool = False,
    strict: bool = False,
) -> int:
    """Run every category and OR their exit bits.

    ``strict`` upgrades skipped checks to failures, so a run that could not
    reach a remote or the GitHub API cannot pass by omission.
    """
    run = runner or default_runner
    deployment_target = "mainframe"
    try:
        from n3rv.reconcile import load_state

        deployment_target = load_state(root).deployment.target
    except Exception:
        deployment_target = "mainframe"
    categories = [
        check_drift(root, run, repo=repo, mainframe_remote=mainframe_remote, deployment_target=deployment_target),
        check_models(root, run, templates_dir=templates_dir),
        check_mcp(root, run, global_config=global_config),
        check_convergence(root, templates_dir=templates_dir),
        check_hooks(root),
    ]
    if strict:
        categories = [category.strict() for category in categories]

    exit_code = 0
    for category in categories:
        if category.failed:
            exit_code |= CATEGORY_CODES[category.name]

    report = {
        "root": str(root),
        "ok": exit_code == 0,
        "exit_code": exit_code,
        "categories": {category.name: category.to_dict() for category in categories},
    }

    if json_output:
        print(json.dumps(report, indent=2))
    else:
        _print_report(report)
    return exit_code


_LEVEL_MARKERS = {"ok": "✓", "warn": "⚠", "fail": "✗", "skip": "⊘"}


def _print_report(report: dict[str, Any]) -> None:
    for name, category in report["categories"].items():
        state = "ok" if category["ok"] else f"FAIL (exit bit {category['code']})"
        print(f"\n{name}: {state}")
        for finding in category["findings"]:
            marker = _LEVEL_MARKERS.get(finding["level"], "?")
            print(f"  {marker} {finding['message']}")
    print()
    if report["ok"]:
        print("All checks passed.")
    else:
        print(f"Diagnostics failed. Exit code {report['exit_code']}.")
