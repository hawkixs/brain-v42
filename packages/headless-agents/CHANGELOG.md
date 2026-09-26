# Changelog — headless-agents

The versioned contract a consumer pins (red-rail ADR-0003, Brain ticket 04bc1f4a).
The surface under contract: the `AgentProvider` protocol (`build_command`,
`child_environment`, `prepare_home`, `tool_call_completed`, `run`), `RunSpec`,
`RunResult` / `TokenUsage`, `RunResult.to_dict()` (schema 1, the shape of `result.json`),
the `registry` facade (`get_provider`, `PROVIDER_NAMES`, `probe`, `max_prompt_bytes`),
`CapabilityProfile` / `McpServer` / `ToolGuard` / `Credentials`, `chain.run_chain`,
`envelope.unwrap`, and the exit codes `PROVIDER_FALLBACK_EXIT_CODE = 3`,
`TIMEOUT_EXIT_CODE = 124` and `TIMEOUT_REPLAYABLE_EXIT_CODE = 4`. A change to any of
these is a **breaking** entry below and a major-or-minor bump while the package is 0.x;
a new provider is additive.

## Tags

Each version is tagged on the brain-v42 repository as `headless-agents-vX.Y.Z`, on the
commit that shipped it — deliberately outside the `v*` pattern, which names brain-v42's own
version and drives its release workflow. Pin it:

```sh
uv add "headless-agents @ git+https://github.com/hawkixs/brain-v42.git@headless-agents-v0.5.1#subdirectory=packages/headless-agents"
```

The earlier `v0.6.0` tag (2026-09-14) also carries 0.1.0 and stays valid; it is the last
time the member rode a brain-v42 tag.

## 0.5.1 — 2026-09-26 (tag `headless-agents-v0.5.1` after merge)

Ticket ha-051-agy: headless-agents 0.5.0 refused the agy rail outright, because agy
1.2.11 had no passing isolation proof -- the live proof measured the repository's own
`CLAUDE.md`/`AGENTS.md`/`GEMINI.md` reaching the model with no tool call involved.

### Fixed
- **The ephemeral HOME agy starts in never sits under a git work tree.** Its root is the
  first of `XDG_RUNTIME_DIR`, the system temporary directory, then the operator's
  `~/.cache/headless-agents/agy-homes`, whose ancestry up to `/` carries no `.git`
  (file or directory); if none qualifies the run is refused before anything starts.
  Otherwise agy's upward walk would reach that repository's instruction files
  (review of PR #228; on the operator's host `/tmp` itself is a repository).
- **agy no longer loads the repository's instruction files natively.** Measured on agy
  1.2.11: with a workspace, the CLI walks from its own process `cwd` up to the nearest
  `.git` root and loads every `GEMINI.md`/`AGENTS.md` it finds along the way,
  unconditionally and before the first turn -- invisible to the `PreToolUse` guard,
  which gates a tool's ARGUMENTS, never the CLI's own startup. Measured alongside it:
  `agy --help` has no flag for this, and no `settings.json` key was found that disables
  it either (the one candidate in the installed binary, a Go struct field tagged
  `json:"contextFileName"`, sits nowhere near the "Rules" discovery code agy's own
  bundled documentation describes, which hardcodes `GEMINI.md`/`AGENTS.md` with no
  mention of an override). The fix masks the files from agy's view instead of
  configuring it away: a workspace run's process `cwd` is now always the ephemeral HOME,
  never the workspace directory itself. The HOME has no `.git` ancestor and carries
  neither file, so the walk finds nothing to load. Every tool argument stays an absolute
  path checked against the workspace root regardless of cwd
  (`agy.workspace_guard_holds`), so confinement is unaffected. Residual, not fixed by
  this: agy also walks up from a file's own directory when a tool call actually opens or
  edits it, per the same bundled documentation -- see the "NATIVE INSTRUCTION-FILE
  DISCOVERY" section of `providers/agy.py`'s module docstring. A further, narrower
  residual: `run_command`'s own default `Cwd`, when the model omits it, now resolves
  inside the ephemeral HOME rather than inside the workspace -- `run_command` was already
  unconfined once `shell` is armed, so this changes convenience, never confinement.
- **The isolation proof now binds to a fingerprint of the installed package's own
  isolation-building source for that rail** (`proofs.isolation_fingerprint`), not to the
  executor's `--version` string alone. The CLI's version never says whether
  headless-agents itself changed how it runs it: a proof recorded while agy 1.2.11 ran
  under the fix above must not silently cover a later downgrade, or a future
  regression, back to the leaking `cwd` -- agy's own version string would be identical
  either time. `isolation_ok` now refuses a proof whose recorded fingerprint does not
  match the rail's currently installed source. Proofs recorded before this shipped
  carry no fingerprint at all and are grandfathered as a match, so claude's, codex's and
  opencode's existing proofs stay valid -- their rails did not change in this release.
  Scoped to isolation only; confinement proofs are unaffected.

**Operators must re-record the agy proof** after installing 0.5.1: the one recorded
under 0.5.0 is both a documented failure (`"passed": false`) and, from here on, missing
the fingerprint this release binds to.

```sh
HA_LIVE=1 pytest -m live tests/live/headless_agents/test_proofs_live.py -k "isolation and agy"
```

### Unchanged
- Everything under contract at the top of this file. claude's, codex's and opencode's
  isolation proofs, if already passing for their installed version, need no
  re-recording: this release did not touch their rails.

## 0.5.0 — 2026-09-26, lot 5 of 5: the `live` suite, README and CHANGELOG (tag `headless-agents-v0.5.0` after merge)

Lot 5 completes 0.5.0; the package version is `0.5.0`, so `ha --version` prints `ha 0.5.0`.
The four lots before it -- roles and the engine (`run.json`, the `steps/` layout, the write
protocol, the new `ha run` grammar), `ha show`/`ha runs`/tool counters, `workflows.toml` with
the `implement` shape, and the `review` shape with the vendor rule -- shipped the whole
surface below; this entry is where the release is written down and tagged.

### Changed (breaking)
- CLI grammar: `ha run TARGET [PROMPT | -] ...` replaces the 0.4.0 grammar. `-p`/`--provider`
  and `--chain` are removed: the provider is the target (`ha run codex "..."`), and a chain
  is declared on a role in `roles.toml`. Both flags still parse -- hidden from `--help` --
  only to fail before anything runs, with a migration message, exit `2`.
- `ha run --json` now prints `run.json` -- the run's own report, replaced atomically after
  every step -- instead of a provider's `result.json`.
- Every run directory gains a `steps/` subdirectory, for every target, read-only or write:
  `steps/<NN>-<slot>-<role>/` holds that step's logs and its own `result.json`; a role's
  fallback chain nests further under `steps/<NN>-<slot>-<role>/links/<index>-<provider>/`.
  `run.json`, `prompt.md` and, for a write run, `change.patch` and the worktree stay at the
  top of the run directory.
- Executor isolation (security fix): the claude rail now runs every invocation under a
  per-run `HOME` and `CLAUDE_CONFIG_DIR` holding nothing but a copy of the Claude login,
  instead of the operator's real `HOME` -- closing the path by which a run could load the
  operator's own `CLAUDE.md`, skills, plugins, hooks, settings or user MCP servers. codex
  already isolates every invocation under an ephemeral `CODEX_HOME`; that is unchanged.

### Added
- `run.json`: `"kind": "run"` is written right after `"schema"` (schema stays `1`), so a
  consumer can tell it apart from a step's `result.json` (schema 1, unchanged, still
  carries `"provider"`, never a `"kind"`) before reading anything else. A `run.json`
  written by a pre-release `main` build that predates this key has no `"kind"` and still
  reads as a run.
- The engine API, roles (`roles.toml`) and workflows (`workflows.toml`) with their two
  shapes, `implement` and `review`, and `--run`, `--continue`, `--findings`; the vendor
  rule, enforced before any review runs, that refuses a reviewer sharing a provider with
  whoever wrote a commit of the reviewed range.
- The state directory `~/.local/state/ha`: the run registry, lineages, review results,
  per-commit provenance, and quarantines (repository and operator scope).
- Residue commits after a failed write step, so no commit `ha` makes in a workspace is
  ever unrecorded.
- `ha show RUN_ID` / `ha show --dir PATH`, `ha roles`, `ha workflows`, `--version`,
  per-step tool counts, and the `role` context scope (`context[].scope == "role"` on a run
  that was given role instructions).

### Unchanged
- `result.json` schema 1 and the `AgentProvider` protocol (`build_command`,
  `child_environment`, `prepare_home`, `tool_call_completed`, `run`) -- the versioned
  contract this file opens with.
- A provider's behaviour when a role gives it no instructions, except the executor
  isolation above.

### Migrating from 0.4.0
- `ha run -p codex "task"` -> `ha run codex "task"`.
- `ha run --chain codex:MODEL,claude:MODEL "task"` -> declare
  `chain = ["codex:MODEL", "claude:MODEL"]` on a role in `roles.toml` and run that role:
  `ha run <role> "task"`.
- `ha run --json` readers: check `"kind"` first -- `"kind": "run"` names `run.json` (this
  file). `result.json` has no `"kind"` and carries `"provider"` at the top level. A
  `run.json` written by a pre-release main build also has no `"kind"`, but it carries no
  top-level `"provider"` and has `"steps"` instead, and still reads as a run. Read a step's
  own provider and model from `steps[].provider` / `steps[].model`, and its full
  `result.json` from `run_dir / steps[].dir / "result.json"` -- `steps[].dir` is already
  the relative directory, e.g. `steps/01-run-codex`, so that step's file is
  `steps/01-run-codex/result.json`.
- Library users are unaffected: `get_provider(name).run(spec)`, the `AgentProvider`
  protocol and `result.json`'s schema-1 shape did not move. red-rail and red-arena call
  the library, never the CLI, so this release changes nothing for them.

## 0.4.0 — lot 4 of 4: the `ha` CLI (tag `headless-agents-v0.4.0` after merge)

Lot 4 completes 0.4.0; the package version is `0.4.0`. The four lot sections below
(facade, workspace, `.git` tripwire, `openai-compat`) are part of the same release.

### Changed (breaking)
- `McpServer.require_loopback` is **removed**, replaced by
  `allowed_networks: tuple[str, ...] | None = ("127.0.0.0/8", "::1/128")`. A caller that
  never set it is unaffected (loopback only, same error message). `require_loopback=False`
  becomes `allowed_networks=None` ("no restriction", spelled out; an empty tuple is
  rejected). Passing `require_loopback` fails with a migration message instead of being
  ignored. A private network is admitted by listing it; host names are never resolved (a
  literal IP is matched, `localhost` only when a listed network holds `127.0.0.1`).
  In this repository the Dream's `brain_mcp_server` migrated to `allowed_networks=None`;
  its golden fixtures pass unchanged.

### Added
- `profile.mcp_no_proxy_hosts(server)` and `capability.scoped_environment(no_proxy_hosts=)`
  / `merged_no_proxy(environ, extra_hosts)`: a listed private literal host is appended to
  `NO_PROXY` (a host, never a CIDR).
- `mcp_profiles`: named MCP profiles in `$XDG_CONFIG_HOME/ha/mcp.toml` (default
  `~/.config/ha/mcp.toml`) — `url`, `bearer_env` (the variable NAME), `tools`, optional
  `name`, `headers`, `allowed_networks` (`"any"` = no restriction). A key that looks like a
  secret value is refused.
- `ha` (`[project.scripts]`): `ha providers`, `ha run`, `ha runs`, `ha clean`. See the
  README. Exit codes: `0` answer, `1` failure, `2` invalid usage, `3` provider unavailable
  / chain exhausted, `4` replayable timeout, `5` `--write` with no change, `124` timeout.
- `ha run --write`: worktree on `ha/<run_id>`, the `.git` tripwire read by the rail AND by
  the CLI around the whole run (no git command at all if either fired), a carrier commit
  through `git_tripwire.git_command` with the repository's hooks running, the patch and
  diffstat printed, never a merge. `ha clean` removes the worktree and keeps the branch.

### Changed (before the tag, from the end-to-end run of 2026-09-24)
- `ha run` gives each link its own model: `--chain P1:MODEL,P2:MODEL` (the model is
  everything after the first colon), then `-m`, then the operator's declared defaults in
  `$XDG_CONFIG_HOME/ha/models.toml` (default `~/.config/ha/models.toml`, one
  `provider = "model"` line each; an unknown provider or a non-string is refused). A link
  left without a model is refused before anything runs (exit `2`), except agy. Found
  end-to-end: without `-m` the rails refused an empty model — and opencode's refusal
  exited `3`, which a chain reads as "unavailable": a configuration error fell through to
  the next link. And one `-m` shared by every link made `--chain codex,claude` unusable.
  A provider named twice in a chain is refused.
- README: the `brain-read` MCP profile example carries `X-Brain-Tool-Profile = "native"`.
  Without it brain's compact catalogue publishes, besides its session lifecycle tools,
  only its two gateway tools, and a
  read-only allowlist found no tool at all (measured end-to-end, all four rails).

### Behaviour
- **codex keeps its shell tool in every workspace mode** (spec decision 13). Measured
  2026-09-24: codex reads files only through its shell, and a writable workspace with
  `shell=False` turned it off — the run changed nothing, silently for a library caller.
  Under `workspace-write` that shell runs inside the same OS sandbox as `apply_patch`
  (writes confined to the writable roots, network off), so `Workspace.shell` no longer
  changes codex's command, and `ha run --write -p codex` needs no `--shell`. The other
  rails keep the flag's meaning: their shell is unconfined. Consequence: a writable codex
  run that read anything has started a `command_execution`, so a chain never replays it
  after a timeout (fail-closed, as for any write).

### Fixed (pre-tag hardening, lot-1 review minors)
- `registry.probe`: `--version` runs in its own session and a timeout — or an interrupt —
  kills its whole process group by its id (never `getpgid`, which fails once CPython has
  reaped an exited launcher), so a forking wrapper leaves no descendant behind; a cleanup
  failure never masks the interrupt; the version is read from stderr when stdout is
  empty. A descendant that escapes through its own `setsid` is out of reach.
- `RunSpec.run_dir=Path('.')` names the run after the current directory instead of an
  empty id (`spec.run_dir_name`: lexical, so a `latest` symlink keeps its own name); a
  `run_dir` with no name at all (the filesystem root) is refused at construction, and by
  `ha run` before it plans chain links or a worktree. `ha run --run-dir` is anchored at
  the CLI's cwd.
- `run_record.record` never raises `OSError`: a `result.json` that cannot be written costs
  a line in `stderr_log`, never the `RunResult` of a run that already happened. It writes
  through an exclusively created, uniquely named temporary file (`0600`) and cleans up
  only that one.
- The three findings above marked "by its id", "before it plans" and "exclusively" come
  from the independent codex/gpt-6-astra review of PR #197 (verdict PATCH_THEN_SHIP).
- README: every python example's imports are checked by a unit test.

### Measured (2026-09-24, real CLIs, throwaway repository)
- `ha providers`: the four CLI rails found, the three presets unavailable without their key.
- `ha run -p codex` read-only: read the repository and answered.
- `ha run --write`, codex `--shell` and claude: each fixed the bug, committed it on its own
  `ha/<run_id>` branch, printed the diffstat and the patch path; `main` untouched.
  `ha clean` removed each worktree and kept the branch.
- Live suite on the release `44a13a7e`: 41 passed, 3 skipped (the HTTP presets, no key),
  1 failed twice — `test_repository_instructions_reach_the_agent[opencode]`, a generic
  answer instead of the repository's code word; a manual `ha` replay found it. Tagged as
  spec decision 12's known limit (weak model `glm-5.3-flash` obeying the task over the
  `<instructions>` block), spec decision 14.
- HTTP presets replayed with their keys: `mistral` (`mistral-small-latest`), `openrouter`
  (`openai/gpt-4o-mini`) and `nvidia` answered with measured usage, and a refused key stopped
  the chain on all three with the key written nowhere. nvidia's `openai/gpt-oss-20b`, a
  reasoning model, took 38 s to over 60 s for one word; the live test now defaults to
  `z-ai/glm-5.3-flash` (29-38 s). Several catalogue models answered 404 or 410.
- After the hardening, codex live (workspace and tripwire suites): 10 passed — including a
  writable run WITHOUT `shell` that edits inside and is refused outside, and a write under
  `.git` refused or caught. 1 failed, identically on `44a13a7e`:
  `test_codex_reads_outside_by_design` — the model now DECLINES to read the outside file
  ("I can't read a file outside the allowed workspace", 4 runs out of 4) without trying.
  The residual stands at the sandbox level (codex's sandbox confines writes, not reads) but
  this test no longer demonstrates it: a model refusal is not a confinement.

## Unreleased — 0.4.0, lot 1 of 4: the facade

Nothing here is tagged yet: 0.4.0 ships after lot 4 (the `ha` CLI), per
`docs/specs/2026-09-23-headless-agents-0.4.0-design.md`, in the private brain-v42-internal repository.

### Added
- `registry`: `get_provider(name)`, `PROVIDER_NAMES`, `UnknownProvider` (a `ValueError`
  whose message lists the valid names), `probe(name) -> Probe(available, detail,
  version)` — zero quota: the executable on `PATH` and its `--version`, never a model
  call, bounded by a timeout — and `max_prompt_bytes(name)`: `None` for the stdin rails
  (claude, codex), the argv limit for agy and opencode. Read it instead of hard-coding a
  limit.
- `RunResult.text`: the final answer, read by the provider from the file its rail writes
  the answer to: `report_log` for codex, agy and opencode. For claude it is also
  `report_log` when one is set, named or given by `run_dir`: claude's stdout alone lands
  there. Without one, it is the bytes this run appended to `raw_log`, stderr included.
  Verbatim — an answer that is itself JSON is never re-read as an envelope — and `None`
  when the run failed or answered nothing.
- `providers.claude.run_claude(answer_log=...)`: stdout alone goes to that file, while
  stderr — the OTEL console stream, any CLI warning — stays in `raw_log`, where
  `tool_call_completed` reads it. Unset, nothing changes, so the Dream's runs are
  byte-identical.
- `RunResult.run_id`, `RunResult.stderr_log`, `RunResult.raw_log`, and
  `RunResult.to_dict()`: the JSON-safe schema-1 form (`result.RESULT_SCHEMA_VERSION = 1`).
  `context`, `workspace` and `branch` belong to the key set from schema 1 and stay `null`
  until lots 2 and 4 fill them.
- `RunSpec.run_dir`: the logs a caller leaves unset default to `report.log`,
  `events.jsonl`, `stderr.log` and `raw.log` inside it (explicit paths still win), its
  name is the `run_id`, and the run writes `result.json` there.

## Unreleased — 0.4.0: the `.git` tripwire (lot 4 entry condition, ticket 0b622f47)

### Added
- `git_tripwire.Tripwire`: armed by every CLI rail on a **writable** workspace, it
  fingerprints before the run every place a later git command would execute from —
  `<ws>/.git` (directory or a linked worktree's file), the git dir's `config`,
  `config.worktree`, `commondir`, `gitdir`, `hooks/` and `info/`, the common dir's
  `config`, `hooks/` and `info/`, every `core.hooksPath` directory (husky-style, often
  inside the work tree), and the operator's `~/.gitconfig` / `$XDG_CONFIG_HOME/git/config`
  — and compares after it. The index, objects and refs are not watched: an agent
  committing with its own shell changes them, and nothing in them executes later.
- `git_tripwire.git_command(root)` / `git_environment(environ, root)`: the only way a
  runtime may run git in an agent-written tree — `-C <root> --git-dir <resolved>
  --work-tree <root> -c core.fsmonitor=false -c safe.bareRepository=explicit`, every
  inherited `GIT_*` dropped, discovery bounded by `GIT_CEILING_DIRECTORIES`. Both raise
  `GitTampered` on a workspace that tripped or whose git dir cannot be pinned. Measured
  (git 2.34): a repository planted in a subdirectory (`sub/.git` with an index and a
  `core.fsmonitor`) fires when plain git runs from `sub` or below; through
  `git_command` it never does.

### Behaviour
- A writable run whose `.git` state changed is a **non-replayable failure**: exit `1`
  whatever the child returned (never the chain's `3`/`4`), `text` is `None`, and each
  changed path is named on stderr (`git tripwire: <path> changed during the run; no git
  command may run in this workspace`).
- `result.json`: the `workspace` block of a writable run carries `git_tampered` — `[]`
  when clean, else the changed paths. Absent for a read-only run (nothing armed).

### Measured (live, 2026-09-24, `tests/live/headless_agents/test_git_tripwire_live.py`)
Each rail, in a writable workspace, was asked to write `.git/hooks/pre-commit` and append
to `.git/config` with its file tools:
- **opencode wrote both**; the tripwire caught it: exit `1`, both paths in `git_tampered`,
  no answer. This is the hole the tripwire exists for.
- claude refused (its Write tool treats `.git/hooks/*` as a sensitive file); codex refused
  (its `workspace-write` sandbox keeps `.git` read-only on Linux); agy refused (the package
  guard). All three: `git_tampered == []`. A rail with `shell=True` is not confined by
  these refusals; the tripwire does not depend on them.
- codex emits `item.started` **and** `item.completed` for a `file_change`: its write-mode
  taint already sees a patch start, so a deadline mid-patch is never the replayable `4`.

## Unreleased — 0.4.0, lot 3 of 4: `openai-compat` and its presets

Per `docs/specs/2026-09-23-headless-agents-0.4.0-design.md` (3.2, in the private brain-v42-internal repository).

### Added
- `providers.openai_compat.OpenAICompatProvider`: text-only chat completions over HTTP,
  standard library only (`urllib`), each call in a killable child process
  (`providers._openai_worker`) so the deadline and the process-group kill behave as on the
  CLI rails. The prompt and the key travel on the child's stdin: never in argv, the child
  environment (the key variable is removed even if `environment_passthrough` names it) or
  any log.
- Four registry names: the presets `openrouter`, `mistral`, `nvidia` (fixed endpoint and
  key variable NAME) and the generic `openai-compat` (`RunSpec.extra["base_url"]` and
  `RunSpec.extra["key_env"]`). `PROVIDER_NAMES` now holds the spec's eight names;
  `registry.HTTP_PROVIDER_NAMES` names the four HTTP ones.
- `registry.probe(name, environ=None)`: for an HTTP provider, zero quota means the key
  variable's presence -- the detail names the variable, never its value. The generic
  provider is reported available ("configured per run").
- `registry.max_prompt_bytes` is `None` for the four HTTP providers.
- Request options through `RunSpec.extra`: `response_format`, `temperature`,
  `max_tokens`. The context bundle's preamble becomes a `system` message. The
  `openrouter` preset asks for `usage.include`, the only source of `cost_usd`.

### Behaviour
- Exit codes: `0` answer; `124` own deadline; `3` HTTP 429/5xx or host unreachable (the
  chain advances: an HTTP run has no tool, so nothing could have been written); `1` HTTP
  401/403, a malformed reply or any other failure; `2` a spec that cannot work (no model,
  missing or invalid `base_url` -- not http(s), carrying credentials, a query or a
  fragment -- an unsupported option, or `base_url`/`key_env` given to a preset), refused
  before anything is sent. A missing key is `1`, named by its variable.
- A profile with `mcp` or `workspace` raises `ValueError`.
- A failure is logged as a category and an HTTP status, never as the response body.

## Unreleased — 0.4.0, lot 2 of 4: the workspace capability

Per `docs/specs/2026-09-23-headless-agents-0.4.0-design.md` (3.3, in the private brain-v42-internal repository), with the amendments in
its section 8 ("Measurement amendments (2026-09-23, lot 2)"): live measurement against the
four rails' real CLIs changed three points the design left open — decisions 9-12.

### Added
- `profile.Workspace(path, write=False, shell=False)`: a directory an agent may read, or
  read and edit, and nothing outside it — confined per rail by each rail's own mechanism
  (codex: OS sandbox; claude: `--restricted`; opencode: the `tools`/`permission` walls;
  agy: the new package-owned guard below). `shell=True` is refused with `ValueError`
  unless `write=True`. `CapabilityProfile.workspace: Workspace | None = None`;
  `workspace=None` keeps every rail byte-for-byte on its pre-0.4.0 behaviour — the Dream
  golden fixtures (3 rails x 6 phases) gate it.
- `context` module: `resolve_context(level, repository_root, user_files, ...)` builds a
  `ContextBundle` from `CLAUDE.md`/`AGENTS.md`/`GEMINI.md` at a repository root (tracked
  or ignored) plus the caller's user-level files. `RunSpec.context: ContextBundle | None`;
  each rail delivers it through ONE channel, the preamble — repository content included in
  every mode, not only write mode (decision 12). `RunResult.context`: the injected files'
  path, scope, size and sha256, filled from schema 1's key set for the first time.
  `RunResult.workspace`: `{"path", "write", "shell"}` when the run carried one, else
  `None`.
- `capability.INVALID_USAGE_EXIT_CODE = 2`: the run was refused BEFORE any spawn because
  its own inputs cannot work — a prompt that, with its context, no longer fits the argv of
  the rail that must carry it. Never a switchover: the next chain link gets the same
  input. claude also returns it when the preamble alone, carried as one
  `--append-system-prompt` argv element, exceeds `131 071` bytes (measured: the kernel's
  `MAX_ARG_STRLEN` is 131072 bytes, refused with `E2BIG`) — refused before `Popen` rather
  than surfacing as an opaque `OSError` the "binary is missing" handler would have silently
  read as `PROVIDER_FALLBACK_EXIT_CODE`.
- `guards.agy_workspace`: the package-owned `PreToolUse` guard confining an agy workspace
  run — standard-library only, fail-closed (a missing, unreadable or malformed
  configuration denies everything), Unicode case-fold key checking against agy's own
  `bytes.EqualFold` field matching, `run_command`'s `Cwd` confined when present. Shipped as
  package data (`importlib.resources`), copied into the ephemeral HOME and PROVEN there
  before spawn by its probes (two more, on `.git` writes, when writes are armed).

### Behaviour
- An agy profile carrying `workspace` uses the package-owned guard
  (`guards.agy_workspace`) instead of the caller's `ToolGuard`; a profile carrying both is
  rejected with `ValueError` before anything runs — the two do not compose. Because agy's
  `view_file` cannot list a directory, the prompt also carries the workspace's tracked
  plus untracked-not-ignored file list.
- opencode narrows its `tools`/`permission` walls to a fixed built-in set instead of
  stacking a third layer: `read`/`glob`/`grep`/`list` always, `edit`/`write` when
  writable, `bash` when `shell` is armed; `external_directory` stays denied in every mode.
  Residual, accepted and documented: `read` follows a symlink INSIDE the workspace to a
  target outside it (measured live 2026-09-23).
- codex's sandbox switches between `read-only` (shell tool ON — its only way to read a
  file) and `workspace-write` (shell tool only when `shell=True` — SUPERSEDED before the
  tag by spec decision 13: the shell is ON in both modes, see the lot-4 Behaviour entry
  above); a read-only codex agent
  can still read outside the workspace by design — an accepted residual, unchanged from
  the read-access decision (spec 3.3, decision 7).
- claude's workspace run adds `--restricted` (file tools confined to the working
  directory; user, project and local settings ignored — a trusted repository's own
  `.claude/settings.json` cannot widen it, measured) on top of the narrowed
  `--permission-mode`/`--tools`.
- The write-mode instruction-file channel designed in 3.3 (an `AGENTS.md`/`CLAUDE.md`
  written into the workspace) is NOT shipped: measured live on 2026-09-23, it failed on
  three rails out of four (claude's `--restricted` does not auto-load it; opencode's
  `OPENCODE_DISABLE_PROJECT_CONFIG=1` also disables `AGENTS.md`; agy reads it only inside
  a git repository). The preamble is the single channel instead (decision 12). Nothing is
  ever written into a workspace.

### Security
- A codex run with a `workspace` uses an EPHEMERAL `CODEX_HOME`: a private `0700`
  directory holding only a symlink to the real `auth.json`, built under a root outside
  every writable root the sandbox could itself reach, torn down after the run with a
  hardened write-back of a rotated `auth.json` (`O_NOFOLLOW`, size-bounded,
  `account_id`-matched, compare-and-swap on the real file's digest) so a legitimate OAuth
  refresh survives the teardown. Residual, deliberately not defended: the sandbox can
  still READ the real `auth.json` through the symlink — this rescue protects its
  integrity, not its confidentiality (decision 11).
- The ephemeral HOME and a workspace's `path` are refused if they would overlap, in both
  directions: a HOME inside the workspace would let the agent read `mcp_config.json` (a
  literal bearer, agy) or rewrite its own `workspace-guard.json`; a workspace inside the
  ephemeral root would let a run reach another run's HOME.
- `providers/codex.py` sets `project_doc_max_bytes=0` in every mode (not only write mode):
  with the preamble now carrying repository content on every rail, a tracked `AGENTS.md`
  codex would otherwise read natively could reach it twice.
- agy's workspace guard denies a write whose raw or resolved target, relative to the
  root, has a `.git` component (compared with `casefold()`), and its pre-spawn probe
  proves it when writes are armed. Residual: claude, opencode and codex (unmeasured
  whether codex's `workspace-write` keeps `.git` read-only) can write `<ws>/.git` in
  write mode: hooks and config run later, outside any sandbox, when git runs in that
  checkout; agy denies it in its guard. Lot 4 must not run git in a workspace whose
  `.git` changed (tracked by a Brain ticket).
- codex fails closed (exit `3`, no spawn) when the uid has no passwd entry to derive the
  `CODEX_HOME` fallback root from.

## 0.3.0 — 2026-09-20 (`headless-agents-v0.3.0`)

### Changed (breaking: a new exit code, and the chain advances on it)
- `capability.TIMEOUT_REPLAYABLE_EXIT_CODE = 4`: the runner's own deadline fired AND the
  event stream proves no tool call on the declared server ever **started** — not "none
  succeeded" (a call in flight may still commit after the kill) but "none was issued".
  `capability.FALLBACK_EXIT_CODES = {3, 4}` is the set `chain.run_chain` advances on.
  Measured on 2026-09-19 (Brain ticket f4277a90): a quota-dead `opencode` blocked for the
  full deadline of 37 Dream phases with zero bytes of events, and a chain reading `124`
  never tried the two links behind it — seven projects lost to a link that never spoke.
- `providers/opencode.py`, `providers/codex.py`, `providers/agy.py`: `run_*` returns `4`
  instead of `124` on a `TimeoutExpired` whose stream carries no started call, and appends
  the reason to `stderr_log`. Each rail owns the predicate (`tool_call_started`) because
  each stream orders its events differently: opencode writes `tool_use` in its terminal
  state only, so the proof is the absence of any `step_start`; codex writes `item.started`
  before an `mcp_tool_call` executes; agy writes an `ACTIVE` `call_mcp_tool` step first.
  A child that exits `124` by itself is still read as a plain timeout. **A consumer that
  compared `exit_code == 124` to mean "the deadline fired" must now also accept `4`.**
- `providers/claude.py` is unchanged and never returns `4`: its only witness is the OTEL
  console stream, which a batch exporter flushes on an interval, so an empty `raw_log` at
  the kill does not prove an empty run.
- `chain.ChainResult.dead_links` (new field, default `()`): the links that returned `4`,
  last link included. A `3` is never counted as dead — it can be transient. The chain
  itself remembers nothing across runs; the caller decides what to do with a dead link
  (the Dream retires it for the rest of the night).
- `capability.failure_code_after_a_write(child_code)`: every rail now reports a child that
  exited **3 or 4 by itself after a completed tool call** as an ordinary failure (`1`),
  never as the child's own code. Before, the child's code was passed through and a chain
  would have replayed a run that provably wrote. `1`, `2` and any other code still pass
  through unchanged.
- `providers/opencode.py`, `providers/codex.py`, `providers/agy.py`: a `RunSpec.deadline`
  that has **already expired at launch** returns `TIMEOUT_EXIT_CODE` (124) without
  launching the child, with the reason in `stderr_log`. Before, the child was launched
  with a zero budget, killed at once on an empty stream, and — with `4` — every remaining
  link of a chain would have been condemned in seconds for the caller's spent budget.

### Unchanged
- `RunSpec`, `RunResult`, `TokenUsage`, `CapabilityProfile`, `McpServer`, `ToolGuard`,
  `Credentials`, `AgentProvider`, `envelope.unwrap`, `PROVIDER_FALLBACK_EXIT_CODE = 3`,
  `TIMEOUT_EXIT_CODE = 124`, and every `run_*` path that is not the runner's own deadline.

## 0.2.0 — 2026-09-15 (`headless-agents-v0.2.0`, immutable release `e11e3660`)

### Added
- `providers/opencode.py`: the `opencode` provider (`opencode run --format json`, OpenCode
  Go). Inline configuration through `OPENCODE_CONFIG_CONTENT` with the bearer referenced as
  `{env:<var>}` (no secret on disk); a fail-closed tool **allowlist** in the config's `tools`
  map; an ephemeral HOME that borrows the operator's `~/.config/opencode/node_modules` and
  refuses to start without it (a fresh HOME would `bun install` from npm); `step_finish`
  telemetry summed into `RunResult.tokens` and `cost_usd`; a fence wrapping the whole
  report is unwrapped (0.2.0 as tagged includes this).
- `RunResult.cost_usd` is populated by a provider for the first time.

### Unchanged
- `RunSpec`, `RunResult`, `TokenUsage`, `CapabilityProfile`, `McpServer`, `ToolGuard`,
  `Credentials`, `AgentProvider`, `chain.run_chain`, `envelope.unwrap`, exit codes.
  `RunSpec.reasoning_effort` is read by the new provider as opencode's `--variant`.

### Breaking
- None.

## 0.1.0 — 2026-09-14 (`v0.6.0`, immutable release `84138170`)

First release as a uv workspace member (Brain ticket b2a2d1a5). Providers `codex`,
`agy`, `claude`; the `CapabilityProfile` model; ephemeral HOMEs and credentials; the
fallback chain; the envelope unwrap. Dependency: `pydantic` only.
