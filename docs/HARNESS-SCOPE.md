# Harness scope: which repositories get a generated tree

Answers n3rv#2. Every claim below was checked against the repositories on
2026-11-15; the evidence is in the table, not in this paragraph.

## The rule

**A repository gets a generated tree when agents are expected to write code or
run gates in it. Otherwise it does not.**

That is the whole policy. It is deliberately shallow: a deeper rule ("get a tree
if the repo has an AGENTS.md", "if it is a satellite") produces arguments instead
of answers, and the tree is cheap to add later — `n3rv state init` on a repo
that grew code is one command, and the first `plan` shows exactly what it adds.

## The six repositories

| Repository | Tree | Declared components | Evidence for the decision |
|---|---|---|---|
| `radio` | yes | 13 agents · 18 skills · 6 commands · 1 hook | Docker stack, shell gates (`scripts/`), Python (`src/mario`) |
| `indiegenerator` | yes | 13 agents · 18 skills · 6 commands · 1 hook | Python package, CI gate + seed sweep |
| `infra` | yes, minimal | 0 · 0 · 0 · 0 | Shell deploy scripts and systemd units that agents edit; nothing else applies |
| `n3rv` | yes, **undeclared** | — | Already carries `.opencode/`, `.githooks/`, `.specify/`, `AGENTS.md`; it dogfoods in practice, it just never declared it |
| `docs` | no | — | 19 files, all prose |
| `.github` | no | — | 4 files, all markdown (`README`, `SECURITY`, `profile/README`); **no workflows at all** |

The last two are covered by the workspace-level `AGENTS.md` at
`~/Repositories/AGENTS.md`, which is generated once and applies to every repo
under it. A per-repo tree there would be configuration larger than the content.

## The count that makes the rest of the issue easy

`radio` and `indiegenerator` declare **identical** component sets: 13 agents, 18
skills, 6 commands, 1 hook. There is no per-repository divergence today, so
there is nothing to fork.

Consequence, and it is a rule rather than an observation: **a skill or agent is
shared until two repositories genuinely disagree about it.** When they do, the
disagreement is resolved by detection feeding the template (as
`profile_has_frameworks` already does in `AGENTS.md`), not by copying the skill
into a repo where it can drift. Forking a template is how the archived fork
ended up with duplicate skill definitions in the first place.

## Stack-specific skills: not yet, and not like that

The issue asked for "stack-specific operational skills for radio and
constrained-host operations, without copying generic templates". No skill or
agent template is stack-conditional today; the only stack parameterization is
detection → framework guidance → `AGENTS.md`.

That is the right shape and it should stay. A "radio host operations" skill
becomes justified the day a constrained-host task is repeated often enough to
name — and then it is a *shared* skill that reads its specifics from detection
or from `.n3rv/state.yaml`, not a `radio-only` copy. Writing it in advance
produces instructions nobody has executed, which is the thing this project's
runbooks already warn about.

## Gate contracts: shared obligation, per-repo gate

No shared runner. Each repository's gate is its own (radio: `scripts/` gates and
`smoke.sh`; indiegenerator: `gate.yml` + the seed `sweep` job; infra:
`shellcheck`, `systemd-analyze verify`, `visudo -cf`). What is shared is the
contract a gate must satisfy:

1. It exits non-zero on a regression and zero otherwise — no reporting-only
   checks wearing a gate's name.
2. It runs on every pull request, not on a path filter that a state-only PR can
   miss. (`radio` paid for this: a `.n3rv/**` change did not match the filters
   and the PR sat `BLOCKED` with zero checks.)
3. It is named in the repository's docs, so "green" has a definition.
4. `n3rv doctor` verifies convergence, never the gate. Two verifiers with one
   job each.

## Versioned harness changes

Already true, and worth stating so it is not re-invented:

- `state.yaml` carries `schema: 2`; a shape change is a schema change with a
  migration, not a new key. (Schema v2 landed in n3rv#16: `requires_python`,
  `deployment.target`, `tiers`, and JSON key ownership via `managed.json_keys`.)
- `template-lock.json` records the template revision each output was generated
  from, so "which harness wrote this file" is answerable per file.
- A migration records its round-trip in the PR body: `state init` → `plan`
  (action count and exit bits) → `apply` → `plan` (empty, exit 0). All three
  migrations done so far used that form; it is the required one.

## The archived fork

`n3rv-thereplacement` **does not exist**. Checked on 2026-11-15: 404 in the
`The-Replacement` org, 404 under the maintainer's account, and `gh search repos`
returns nothing. The "archived-fork migration path" item is therefore closed as
not applicable rather than migrated. If the repository ever returns, this
paragraph is wrong and the item reopens.

## Schema v2 (n3rv#16)

Schema v2 makes the always-on tier and deployment topology declarable. A
repository can decline `tiers.runtime`, `tiers.spec_kit`, and `tiers.tooling`
(leaving only the floor: `AGENTS.md` + `opencode.json`), declare
`deployment.target: none`, and pin `detection.requires_python` from
`pyproject.toml`. JSON key ownership (`managed.json_keys`) lets template
renames converge instead of leaving stale keys. A repo that declines a tier it
already has on disk gets a sha-guarded delete/conflict, never a silent removal.
