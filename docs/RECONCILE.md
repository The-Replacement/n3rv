# Reconciling a project to its declared state

`n3rv init` writes a tree. `n3rv update` rewrites it. Both decide *what* to
write by running detection against the working tree at that moment - so the same
n3rv version on a different branch produces different configuration, and nobody
reviews it.

The reconciler removes the guess: `.n3rv/state.yaml` declares the decisions,
and rendering becomes a pure function of that file plus the templates plus the
n3rv version.

## The contract

```
n3rv state init     # once: freeze what the tree already expresses
n3rv plan           # reads only; exits with a bitmask
n3rv apply          # writes only what plan decided
n3rv doctor --strict # verifies independently
```

Three commands, three jobs. `plan` never writes, `apply` never verifies, and
`doctor` never edits. A failure at one step cannot masquerade as a success at
another.

## `.n3rv/state.yaml` (schema 2)

```yaml
schema: 2
project:
  name: radio
  stack: generic
components:            # explicit lists. There is no "all"
  agents: [git-ops, github-ops, n3rv, ...]
  skills: [code, commits, git-ops, ...]
  commands: [handoff, review, ...]
  hooks: [pre-push]
detection:             # pinned, because templates interpolate it into file bodies
  structure:
    has_src_dir: true
    has_tests_dir: true
    has_web_files: true
    entry_points: []
    key_dirs: [caddy, cam, config, web]
  frameworks: []
  tools: []
  requires_python: ">=3.15"   # pinned from pyproject.toml when present
mcp:
  servers:             # full specs, not names
    - name: sequential-thinking
      type: local
      command: [npx, -y, "@modelcontextprotocol/server-sequential-thinking@2.1.1"]
deployment:            # declared topology (operator decision)
  target: mainframe    # or "none" for repos that are not deployed
tiers:                 # declinable always-on tiers (floor is AGENTS.md + opencode.json)
  runtime: true
  spec_kit: true
  tooling: true
board:
  owner: The-Replacement
  project: 1
```

Schema v2 is additive over schema 1. A schema 1 file loads under v2 code with
defaults filled in memory (`requires_python: null`, `deployment.target:
"mainframe"`, all tiers `true`). Persist the upgrade without re-running
detection:

```
n3rv state migrate
```

`managed.json_keys` in `.n3rv/template-lock.json` records which JSON keys each
generator-owned output claimed, so a template rename/delete converges instead of
leaving stale keys behind (user-added keys are preserved and reported).

Three decisions worth understanding:

- **Components are explicit.** A skill shipped by a later n3rv does not appear
  in your project because you upgraded; it appears because you added it here.
- **Detection is pinned.** `AGENTS.md` and the `testing` skill embed structure
  flags and tool lists. If those came from the live tree, a directory appearing
  overnight would change generated file bodies with no state change.
- **MCP servers carry full specs.** A name-only reference would mean a catalog
  bump silently rewrote your config. Pinned specs make that a divergence report
  instead.

`state init` writes what the tree *already expresses*: manifest-known components
that exist on disk, MCP servers from `opencode.json` (V2 `mcp.servers` or the V1
layout), detection performed once and frozen. Detection only gets a vote when
the config says nothing.

## Verdicts

| Verdict | Meaning | Written? |
|---|---|---|
| `create` / `update` / `regenerate` | the file differs from the declared state | yes |
| `delete` | no longer declared, and the lockfile proves the generator wrote it and nobody edited it | yes |
| `conflict` | the plan cannot decide without a human (see below) | **no** |
| `inert` | the content would be dropped by the global policy at startup | **no** |
| `divergence` | a finding, never an action: detection disagrees with the state | no |

**Conflicts** are raised when applying would destroy something the generator
cannot regenerate:

- a project-owned file whose lockfile entry records no provenance (a `null`
  entry from an older generator is enough)
- a project-owned file whose managed region changed since generation
- a file no longer declared whose lockfile cannot prove ownership

`--force-project-files` is the one explicit way through, per invocation.
`update` silently skips these files; a reconciler refuses to pretend they are
converged.

**Inert** exists because of the global policy layer: an MCP server or skill the
allowlist does not cover is removed at startup. Writing it anyway produces a
config that reports success and silently loses the entry. The plan marks it and
`apply` will not write it. The policy file is read, never edited.

**Divergence** keeps detection useful without letting it decide. A `web/`
directory appearing produces "detection recommends chrome-devtools that the
state does not declare" - an invitation, not a change.

## Exit bits

Independent of `doctor`'s category codes, and OR-able:

| Bit | Value | Meaning |
|---|---|---|
| drift | 1 | the tree differs from the declared state |
| conflict | 2 | at least one action needs a human decision |
| divergence | 4 | detection reports something the state does not declare |
| inert | 8 | at least one action would be dropped by policy |
| usage | 64 | no state file, or the state names a component the templates do not ship |

`0` means clean. `--json` emits the same information plus `plan_hash`.

## Two hashes

`plan_hash` covers the decisions - `(kind, target, strategy, reason)` - never
the bytes. `systemd/n3rv-hub.service` embeds the resolved binary path, so two
machines legitimately render different bytes for the same intent. `env_hash`
carries that difference (project root, n3rv binary). Same state, same version,
same templates → same `plan_hash` anywhere.

## The interlock

`n3rv update` refuses to run in a repository that has `.n3rv/state.yaml`, and
points at `plan`/`apply`. Two writers on one tree is how drift starts. Projects
that have not declared a state keep using `update` unchanged.

## Freeze semantics

This is a gate, not a convenience. After migration:

- a new skill or agent from an upgraded n3rv → add its name to `components`
- a new MCP server → add its spec to `mcp.servers` **and** its name to the
  global policy allowlist, or the plan will report it inert
- a template change → the plan shows the diff before anything is written

## Migration recipe

```bash
n3rv state init --root <repo>        # 1. freeze what is there
n3rv plan --root <repo>              # 2. read: expect drift, maybe conflicts
n3rv apply --root <repo> --force-project-files   # 3. resolve once, deliberately
n3rv plan --root <repo>              # 4. expect empty
n3rv doctor --root <repo> --strict   # 5. independent verification
git add .n3rv/state.yaml .gitignore   # 6. commit the state, not the tree
```

Commit the state file. A state file nobody can review is a state file nobody
declared: if `.n3rv/` is gitignored, keep the directory ignored but re-allow
the file (`!.n3rv/state.yaml`).

**Read a marker-merge file before forcing it.** Everything inside
`N3RV-MARKER-START`/`END` is the generator's, including that paragraph someone
added there last month. Project prose belongs outside the markers, where
marker merge preserves it.

## What the reconciler never does

- No runtime convergence: no docker, no compose, no service restarts.
- No GitHub writes: `board.sync_issue` stays an explicit adapter call, so a
  network failure cannot look like local drift.
- No policy edits: inert is reported, never resolved by widening the allowlist.
- No plan file: `apply` recomputes. A saved plan could describe actions that no
  longer match reality; the state is the authority.

## Worked example

`radio` migrated in The-Replacement/radio#114. First plan against the real tree:
29 actions - 25 files the lockfile had never recorded, 2 updates, 2 conflicts
(a `null` lockfile entry for `AGENTS.md`, an empty `template` field for
`pre-push`). After one resolved apply: `plan` empty with exit 4 (divergence only,
`chrome-devtools`), `doctor --strict` exit 0, `update` refusing.
