"""Read-only consumer of a deployment runtime verdict.

A deployed repository declares one argv in ``.n3rv/state.yaml``::

    deployment:
      target: mainframe
      runtime:
        verify_command: ["ssh", "-o", "BatchMode=yes", "box",
                         "cd /srv/app && python3 scripts/deploy_manifest.py verify --json"]
        timeout_s: 45

``doctor`` runs it and parses the answer. The contract is deliberately tiny and
frozen, so the producer (a repository's own verifier) can evolve its checks
without n3rv learning anything about that repository:

    {"schema": 1,
     "verdict": "OK" | "MISMATCH" | "UNVERIFIED",
     "source_sha": "<the revision the host believes it runs>",   # optional
     "checks": [{"name": str, "status": "PASS|FAIL|UNKNOWN|NOTE", "detail": str}]}

The exit code is advisory: a verifier that exits 2 because it could not inspect
Docker still prints ``UNVERIFIED``, and that is the answer that matters. An
unreadable answer is never ``OK`` — it is a skip, which ``--strict`` fails.

Nothing here writes, and nothing here accepts a mutating command.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

VERDICT_SCHEMA = 1
"""Contract version this module understands. Bump only for an incompatible change."""

MAX_OUTPUT_BYTES = 65_536
"""Output larger than this is treated as unparseable, not partially trusted."""

VERDICTS = frozenset({"OK", "MISMATCH", "UNVERIFIED"})
CHECK_STATUSES = frozenset({"PASS", "FAIL", "UNKNOWN", "NOTE"})

#: A verifier answers questions. If its argv can mutate the host, it is not a
#: verifier and doctor refuses to run it: a diagnostic that deploys is worse
#: than no diagnostic.
_MUTATING = (
    re.compile(r"\bdocker\s+compose\b.*\b(up|down|build|restart|run|rm)\b"),
    re.compile(r"\bdocker\s+(run|build|restart|rm|stop)\b"),
    re.compile(r"\bgit\s+(push|reset|clean|checkout|rebase)\b"),
    re.compile(r"\bsystemctl\s+(start|stop|restart|reload|enable|disable)\b"),
    re.compile(r"\brm\s+-"),
    re.compile(r"\bn3rv\s+(apply|init|update)\b"),
)


class VerdictError(Exception):
    """The verifier produced no answer that can be trusted."""


@dataclass(frozen=True)
class Check:
    """One row of a verifier's own report, carried through verbatim."""

    name: str
    status: str
    detail: str = ""


@dataclass(frozen=True)
class Verdict:
    """A parsed, validated answer."""

    verdict: str
    checks: tuple[Check, ...] = ()
    source_sha: str | None = None
    schema: int = VERDICT_SCHEMA

    @property
    def failing(self) -> tuple[Check, ...]:
        """Checks that failed, or the whole set when the verifier gave no detail."""
        return tuple(check for check in self.checks if check.status == "FAIL")


def _excerpt(text: str, limit: int = 200) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


def parse_verdict(stdout: str) -> Verdict:
    """Parse and validate the verdict contract.

    Raises:
        VerdictError: when the text is empty, oversized, not JSON, not the
            contract's shape, or names a verdict/status this version does not
            know. A partially understood verdict is not a verdict.
    """
    text = stdout.strip()
    if not text:
        raise VerdictError("returned no output")
    if len(stdout.encode("utf-8")) > MAX_OUTPUT_BYTES:
        raise VerdictError(f"returned {len(stdout)} bytes, more than the {MAX_OUTPUT_BYTES} this check trusts")

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        raise VerdictError(f"returned output that is not JSON: {_excerpt(text)}") from None
    if not isinstance(payload, dict):
        raise VerdictError("returned JSON that is not an object")

    schema = payload.get("schema")
    if schema != VERDICT_SCHEMA:
        raise VerdictError(f"reported schema {schema!r}; this n3rv understands schema {VERDICT_SCHEMA}")

    verdict = payload.get("verdict")
    if verdict not in VERDICTS:
        raise VerdictError(f"reported verdict {verdict!r}; expected one of {sorted(VERDICTS)}")

    source_sha = payload.get("source_sha")
    if source_sha is not None and not isinstance(source_sha, str):
        raise VerdictError("reported a source_sha that is not a string")

    raw_checks = payload.get("checks", [])
    if not isinstance(raw_checks, list):
        raise VerdictError("reported checks that are not a list")
    checks: list[Check] = []
    for raw in raw_checks:
        if not isinstance(raw, dict):
            raise VerdictError("reported a check that is not an object")
        name, status = raw.get("name"), raw.get("status")
        if not isinstance(name, str) or not name:
            raise VerdictError("reported a check without a name")
        if status not in CHECK_STATUSES:
            raise VerdictError(f"check {name!r} reported status {status!r}; expected one of {sorted(CHECK_STATUSES)}")
        detail = raw.get("detail", "")
        checks.append(Check(name, status, detail if isinstance(detail, str) else ""))

    return Verdict(verdict=verdict, checks=tuple(checks), source_sha=source_sha, schema=VERDICT_SCHEMA)


def _reject_mutating(command: list[str]) -> None:
    joined = " ".join(command)
    for pattern in _MUTATING:
        match = pattern.search(joined)
        if match:
            raise VerdictError(f"refuses to run a mutating verify_command ({match.group(0)!r}): doctor only reports")


def run_verdict(
    command: list[str],
    runner: Callable[..., Any],
    *,
    cwd: Path | None = None,
    timeout_s: int = 30,
) -> Verdict:
    """Run a declared verifier and return its verdict.

    The argv is executed as given — no shell, no rewriting — after refusing any
    command that could change the host.

    Raises:
        VerdictError: the command could not run, timed out, or answered with
            something outside the contract.
    """
    if not command:
        raise VerdictError("verify_command is empty")
    _reject_mutating(command)

    result = runner(command, cwd=cwd, timeout=timeout_s)
    if result.returncode == 124:
        raise VerdictError(f"timed out after {timeout_s}s")
    if result.returncode == 127:
        raise VerdictError(f"cannot execute {command[0]}: {_excerpt(result.stderr) or 'not found'}")

    try:
        return parse_verdict(result.stdout)
    except VerdictError as exc:
        if result.returncode != 0:
            detail = _excerpt(result.stderr)
            raise VerdictError(f"{exc} (exit {result.returncode}{f': {detail}' if detail else ''})") from None
        raise


def to_context(verdict: Verdict) -> dict[str, Any]:
    """The shape ``doctor --json`` carries for a runtime finding."""
    return {
        "verdict": verdict.verdict,
        "source_sha": verdict.source_sha,
        "checks": [{"name": c.name, "status": c.status, "detail": c.detail} for c in verdict.checks],
    }


__all__ = [
    "CHECK_STATUSES",
    "MAX_OUTPUT_BYTES",
    "VERDICTS",
    "VERDICT_SCHEMA",
    "Check",
    "Verdict",
    "VerdictError",
    "parse_verdict",
    "run_verdict",
    "to_context",
]
