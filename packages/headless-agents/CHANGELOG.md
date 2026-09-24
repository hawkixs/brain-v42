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
uv add "headless-agents @ git+https://github.com/hawkixs/brain-v42.git@headless-agents-v0.3.0#subdirectory=packages/headless-agents"
```

The earlier `v0.6.0` tag (2026-09-14) also carries 0.1.0 and stays valid; it is the last
time the member rode a brain-v42 tag.

## Unreleased — 0.4.0, lot 1 of 4: the facade

Nothing here is tagged yet: 0.4.0 ships after lot 4 (the `ha` CLI), per
`docs/specs/2026-09-23-headless-agents-0.4.0-design.md`.

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

## Unreleased — 0.4.0, lot 2 of 4: the workspace capability

Per `docs/specs/2026-09-23-headless-agents-0.4.0-design.md` (3.3), with the amendments in
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
  file) and `workspace-write` (shell tool only when `shell=True`); a read-only codex agent
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
