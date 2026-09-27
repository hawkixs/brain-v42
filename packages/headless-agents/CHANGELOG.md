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

## Unreleased — 0.5.3, lot 4b: first come, first served admission

0.5.2 gave an unconfined writer only a best-effort preference at admission (lot 3's
writer-intent lock and admission gate), because `flock` orders no waiters:
- a continuous, overlapping stream of readers could keep a writer still polling for
  admission out until its deadline;
- a stream of writers could make a waiting reader time out.

Global admission is now first come, first served (decision 7ef98bc4).

### Changed
- **A ticketed admission queue replaces the writer-intent lock and the admission gate.**
  Every admission takes a ticket in `<state>/admission/` and waits its turn:
  - shared admissions queued together are admitted together;
  - an unconfined write waits for every admission queued before it, and every admission
    queued after it waits for it;
  - `--wait` covers the queue and the global lock with its one deadline.

  `ha clean` queues like any run.
- **A crashed waiter never blocks.** A waiter's file becomes visible only once it is
  locked, and it is removed before its lock is released. A visible file nobody holds
  therefore means exactly "a dead waiter", skipped and removed at the next poll: no pid
  probing, no heartbeat.
- **An explicit `--wait` that expires at the global admission says what the run waited
  for:** `waiting behind N earlier admission(s) (run ids)`, or that runs, or an unconfined
  write, still hold the global lock. It used to name a lock cut out of an exception's
  text. The engine now reads the timeout's own fields: `locks.AdmissionTimeout` carries
  the phase (`queue` or `global`) and the waiters it waited for, and a test forbids
  parsing exception text in `engine.py`.
- Lock order: the admission ticket (an instant) and the admission waiter (while queued)
  take ranks 2 and 3, in place of the writer-intent lock and the gate. The global lock,
  the lineage registry and the lineages keep theirs.

### Unchanged
- **Exclusion.** The global lock is still the `flock` of `unconfined.lock`, shared or
  exclusive, taken exactly as before. The queue only decides who may try it, and when:
  a queue bug can cost fairness or time, never let an unconfined write run beside
  another run. The exclusion invariants are pinned by process tests written against
  0.5.2's gate, and they pass unchanged on the queue.
- The two refusals without `--wait`, word for word, and every exit code.

Leftover `writer-intent.lock` and `admission-gate.lock` files in a state directory are
no longer used and are harmless. **Upgrade window:** an `ha` 0.5.2 process still running
admits through its gate while 0.5.3 processes queue. Exclusion holds between the two, but
fairness across the two populations does not.

## Unreleased — 0.5.2, lot 2: proof state visible

`ha providers` used to say only whether a rail's executable was found: an isolation
proof going stale silently -- a CLI updating itself overnight, with the proof still
naming the old version -- refused every run on that rail with nothing having said so
beforehand (spec §3.2, §4, Q2).

### Added
- **Five proof statuses, read ahead of a run**: `passed`, `failed`, `missing`, `stale`
  (always naming what the proof was recorded for -- another version, or, at the same
  version, the isolation source having moved under it) and `unreadable` (a record
  present but unparsable, naming another rail, or shaped wrong -- distinct from
  `missing`, since an operator fixes the two differently), per rail and per proof kind
  (`headless_agents.proof_state`).
- **Three modes**, computed only from `proofs.isolation_ok()` and `proofs.confinement()`
  -- the exact functions the engine itself calls before a run, never re-derived from the
  status above: `refused`, `writes serialised`, `parallel`.
- **`ha providers` and `ha providers --json` show them.** A CLI rail's row gains
  `isolation`, `confinement`, `mode` and `reprove` (the one command that would re-prove
  whatever is not passed, or `null`/absent once everything already is); an HTTP
  provider's row gets the same four keys as `null`. Additive only: every existing key
  keeps its value and its meaning.
- **One source for the re-prove command.** The engine's own isolation refusal now names
  `proof_state.reprove_command()`'s output instead of building its own string, so the
  refusal and `ha providers` can never name a different command for the same rail.

No gate, lock, record format or exit code changed: `ha providers` still exits 0, and
`proofs.py` and `providers/*.py` are untouched (lot 1b's territory, PR #237 in flight).

## Unreleased — 0.5.2, lot 3: bounded admission waits

- `ha run … --wait SECONDS` uses one explicit, monotonic admission deadline
  for the global, lineage registry, and lineage locks; expiry returns exit 2
  before any provider step runs, and leaves nothing behind -- an unstarted
  run's entry is forgotten, read, write, or review alike. An invalid
  `--wait` value is rejected before any admission is attempted; it names no
  contested lock, only the flag itself.
- A lock granted past the deadline is refused, never accepted late.
- `ha clean` is admitted through the same gate as every other run, instead
  of taking the global lock directly.
- An admission gate gives an unconfined writer that already holds it
  exclusion over every later run, shared or not, until it releases the gate
  or times out; a reader arriving after a writer has won the gate queues
  behind it. This is a **best-effort** mitigation, not a fairness
  guarantee: `flock` does not order waiters, so a writer still polling for
  admission can in principle be overtaken by a continuous, overlapping
  stream of readers, and a continuous stream of writers can likewise make
  a waiting reader time out. A fair FIFO admission queue is planned for
  0.5.3.
- Runs without `--wait` retain the existing 10-second lock bounds. Provider
  `--timeout`, proof requirements, and exit-code meanings are unchanged.

## Unreleased — 0.5.2, lot 6: model catalogue and live drift

Lot 6 of the parallel-runs design (§3.6): a native `ha models`, independent of lots 1-5
and brought forward at the operator's request so the ha-delegate skill needs no interim
script.

### Added
- **`ha models [--provider NAME] [--json] [--refresh]`.** Validates the operator's own
  `~/.config/ha/catalog.toml` (or `$XDG_CONFIG_HOME/ha/catalog.toml` when that variable is
  absolute) against the frozen schema v1 that red-skills owns, naming the offending key on
  every departure from it -- a warning for an unknown field or a non-table top-level value,
  an error for everything else, including an effort outside its provider's rule.
- **Bounded live-list queries** for the three rails that have one: `opencode models`,
  `agy models`, and OpenRouter's `GET /api/v1/models`. Every other rail is reported
  `catalogue-only`. A query that times out or answers unreadable output is reported, never
  fatal, and never taken to mean a catalogued model disappeared.
- **Usage and drift reporting.** The report merges the catalogue with `roles.toml`'s
  declared links and `models.toml`'s defaults; `--refresh` adds `live_uncatalogued`,
  `catalogued_gone`, `unknown_role_model` and `stale_verification` (30 days) without
  changing anything. The JSON form always carries the drift.
- **Read-only contract.** `ha` never rewrites `catalog.toml`; the data is the operator's,
  never the package's.

## Unreleased — 0.5.2, lot 1: a sound codex confinement proof

Ticket e454b011: the codex confinement proof never concluded, and the reader behind it
was lenient. Fixed before any new confinement proof is trusted (spec §3.1).

### Fixed
- **The live confinement probe runs one outside target per provider run**, each with its
  own prescribed shell command and its own nonce, plus a workspace control write so a run
  that could not write at all is not mistaken for confinement.
- **codex evidence is credited only to a trusted system shell's single-line, exact-path
  refusal of the prescribed command.** The former reader credited every target merely
  named anywhere in a denied command's output — by basename, so a workspace-local script
  the model planted as `zsh` could forge one, and as a bare substring, so a marker and a
  path on different lines, or a refusal naming a sibling (`config.bak`) or a child
  (`config/x`) of the target, could also pass as refused. `refused_attempts` now trusts
  only an exact absolute path in `_TRUSTED_SHELLS` wrapping the exact prescribed command
  (`probe_command`, `_probed_target`), and requires the marker and the whole target path
  on the SAME output line (`_refusal_line`).
- **The verdict checks bytes first** (`confinement_verdict`): a changed outside target
  fails the rail even when a run crashed mid-probe, and claude is now skipped as
  unprovable (its tool log names no path for a rejected call, Q91=b) instead of being
  probed for nothing.
- **Any unreadable or changed outside target fails the rail, shell startup files of the
  probe's HOME included.** `outside_changes` never raises on the read it performs — a
  target the agent deleted, corrupted or made unreadable counts as changed, not as a
  crash that skips the byte check — and the live probe now also watches `.zshenv`,
  `.zprofile`, `.zshrc`, `.zlogin`, `.bashrc`, `.bash_profile`, `.bash_login` and
  `.profile`: a sandboxed command able to write one of these could forge a future refusal
  through a shell function or alias.

The proof record format is unchanged (`confinement: {passed, date}`), so an installed
headless-agents 0.5.1 honours a proof recorded by this harness.

### lot 1b — a conclusive codex confinement proof from the kept session rollout

Learnings a5460289 and 80934778: `codex exec --json` never logs a sandbox-refused
command — the refusal exists only in the session's own rollout, as a `custom_tool_call`
named `exec` plus its `custom_tool_call_output`. Lot 1's own reader (above) could
therefore never see a codex refusal at all, and left the codex confinement proof
inconclusive.

- **A probe-only entry point** (`CodexProvider.run_with_rollout`, `run_codex(...,
  rollout_log=...)`) runs codex WITHOUT `--ephemeral`, so its session rollout survives
  inside the run-owned `CODEX_HOME` long enough to be copied out to
  `run_dir/rollout.jsonl` (mode `0600`) before that home is torn down, on every exit
  path. **Production argv is unaffected**: `CodexProvider.run` (used by every other
  caller, and by `run_with_rollout` itself for everything except the one flag) still
  builds the exact command it always has.
- **`refused_attempts` now unions the rollout evidence into codex's existing
  `events.jsonl` reading**, opt-in via a new `rail_version` keyword: a `custom_tool_call`
  named `exec`, status `completed`, whose input is EXACTLY the one measured script for
  `probe_command(line, target)` — not a JS parser, so any drift (another argument, a
  second statement, a hand-written `text(...)`, another nonce or target, a batched
  command) is inconclusive, never a false credit — paired by `call_id` with its own
  `custom_tool_call_output` in the exact measured two-part shape, bound to THIS run by
  its one `thread.started` in `events.jsonl` matching `session_meta.id`, `rail_version`
  matching `session_meta.cli_version`, and every `turn_context.sandbox_policy` matching
  ha's own write argv. The model's own narration of the same refusal — an
  `agent_message`, a `task_complete.last_agent_message`, a `function_call_output` — is
  never evidence. Every lot-1 test keeps its meaning: without `rail_version` the rollout
  is never consulted.
- **The live probe never touches the operator's real session store.** `test_confinement`
  now fails the codex run closed to INCONCLUSIVE — not a pass — if any rollout under the
  operator's real `~/.codex/sessions` was written during the probe (honouring `$CODEX_HOME`
  when set, else `~/.codex`).

Review round, before merge:
- **`session_meta` must be exactly one record, and the first one**; a call's governing
  `turn_context` must be the LATEST one strictly BEFORE it, never one appended after; every
  `custom_tool_call` is tracked by `call_id` regardless of its own name or status BEFORE the
  `exec`/`completed` filter runs, so an id reused by an ineligible call is dropped just like
  two eligible calls sharing one.
- **The rollout copy (`_keep_rollout`) and the reader (`_rollout_refusals`) are both
  TOCTOU-free**: every directory level from the run-owned `CODEX_HOME` down to the rollout
  file is opened `dir_fd`-relative with `O_NOFOLLOW` (never a symlinked intermediate
  directory, which `Path.glob` would happily traverse), the size cap is enforced on bytes
  actually read through the opened descriptor in a loop (a single `os.read` can be short),
  never on an earlier `stat` a concurrent writer could race past.
- **`turn_context.sandbox_policy` also accepts ha's own write argv plus a `writable_roots`
  entry** (ticket 0b3fcdbf / PR #236 adds `sandbox_workspace_write.writable_roots=["<scratch>"]`
  to every workspace-write run; codex 0.156.0 measured 2026-09-27 to record it verbatim,
  never replacing the four required keys) — but only when every entry is an ABSOLUTE path
  and is structurally safe: never equal to, an ANCESTOR of, or a DESCENDANT of the
  workspace, and — symmetrically — never equal to, an ancestor of, or a descendant of any
  probed target. A forged or misrecorded entry naming one of them credits nothing, rather
  than trust a policy that would make the refusal meaningless.
- **Documented residual**: `refused_attempts` alone cannot see whether an outside target's
  own bytes changed — that ordering lives in `confinement_verdict`, which checks bytes
  UNCONDITIONALLY before any rollout evidence matters (pinned by a dedicated test). A
  rollout-based PASS still assumes a non-adversarial agent for the narrower claim "this
  reader's credited refusal is genuine": codex's own `--json` event stream carries no record
  at all for the code-mode `exec` tool call (allowed or refused) and shares no `call_id` with
  the rollout, so there is no independent, ha-observed stream to cross-check a credited
  `call_id` against. An agent that escapes the sandbox, leaves every target untouched and
  forges its own rollout is not defended against by this module.

Review round 2, before merge:
- **The live probe's operator-store guard now derives its own-store check from
  `resolve_real_codex_home`** — extracted from what was inline in `run_codex` — unioned
  with the parent (pytest) process's own resolution: the probe's `RunSpec` environment
  never carries `CODEX_HOME`, so `run_codex` falls back to `Path.home()/.codex` (this
  process's own `$HOME`, never the spec's); a guard that read `$CODEX_HOME` from the
  parent process alone could watch a store `run_codex` never resolves to for that spec,
  and miss a real write elsewhere. Checked for every combination of parent `$CODEX_HOME`
  set/unset crossed with the spec environment carrying `CODEX_HOME` or not.
- **`_keep_rollout`'s caller is now `except Exception`, not `except OSError`**: the one
  promise is that a failed rollout copy never changes `run_codex`'s own exit code or
  raises, and nothing guarantees every future failure mode stays an `OSError`.
- **`os.scandir` on a directory file descriptor, pinned against a real on-disk tree.** An
  agy finding claiming it raises `TypeError` on this platform did not reproduce (measured
  on the package's own Python 3.12.12); a new no-mocks test exercises
  `_rollout_candidate_descriptors`/`_keep_rollout` against a real `sessions/YYYY/MM/DD`
  tree and checks the kept file's bytes.

Review round 3, before merge:
- **The `writable_roots` checks now compare NORMALISED paths, not raw lexical ones.**
  `/base/other/../workspace` designates the workspace but is not equal to it as `Path`
  components, so it slipped past the equal/ancestor/descendant checks undetected. Every
  writable_roots entry, the workspace and every probed target must now be an absolute,
  already-normalised path (`_is_absolute_and_normalised`: no `..`, no `.`, no doubled
  slash, no trailing slash) before any comparison runs at all — never resolved through a
  symlink, since the path a rollout names may no longer exist by the time the proof is
  read. 24 new parametrized tests cover four non-normalised shapes across six
  equal/ancestor/descendant relations to the workspace and a target.

No contract surface change; production argv unchanged; the proof record format is
unchanged (`confinement: {passed, date}`).

### Fixed — write-run robustness (ticket 0b3fcdbf)

A codex write run whose agent ran the test suite (run 20260927T014150-f71e5aaa, ha 0.5.1)
committed pytest's temporary tree with the task's files, crashed on the diff, and
left its unconfined intent behind for the operator quarantine to find.

- **A codex write run's sandbox gets one writable temp root outside the worktree.**
  `/tmp` and the operator's `$TMPDIR` were closed and nothing replaced them, so Python's
  `tempfile` fell back to the current directory -- the worktree -- and a sandboxed
  `pytest` created `pytest-of-<user>/` there, which the engine's `git add -A` swept
  into the commit. The per-run scratch directory (fresh, empty, outside the workspace,
  removed after the run) is now declared as `sandbox_workspace_write.writable_roots`,
  and `TMPDIR`, `TEMP` and `TMP` all name it; `/tmp` and the operator's `$TMPDIR` stay
  closed.
- **The engine's commit leaves tool artifacts out, on every write rail, and names
  them.** A writable temp dir does not stop a project's own relative `--basetemp`, a
  cache directory pytest did not create (so it wrote no `.gitignore` into it) or
  bytecode from landing in the worktree, and `git add -A` committed them. The engine's
  commit, its "anything changed?" check and a continuation's "clean?" check now leave
  out UNTRACKED tool output only -- under `write_flow.TOOL_ARTIFACT_DIRS`
  (`__pycache__/`, `.pytest_cache/`, `pytest-of-*/`), with
  `write_flow.TOOL_ARTIFACT_SUFFIXES` (`.pyc`, `.pyo`), and the entries of every
  pytest temp root recognised by the layout pytest writes, whatever `--basetemp` named
  it: a `<prefix>current` symlink naming a sibling `<prefix><N>` directory. Only the
  symlink and those numbered directories are left out, and only when each is new (a
  real directory git tracks nothing under) -- never the directory holding them, so a
  forged layout hides neither a tracked edit nor a file beside it (review of #236). A
  tracked modification or deletion is always the task's and always committed, whatever
  its name (review of #236: a deleted tracked `.pyc` fixture read as no change). The
  engine reads the worktree once (`git status -z`), stages every tracked change
  (`git add -u`) and exactly the untracked paths it kept, literally, NUL-separated on
  stdin. What is left out stays in the worktree and is named on stderr; a run whose
  agent only ran the tests changes nothing (exit `5`).
- **Git output that is not UTF-8 no longer crashes the engine, and what is recorded
  keeps its bytes.** The diff of a file that is not UTF-8 (`'utf-8' codec can't decode
  byte 0xff`) and a hook printing such bytes both crashed the run after its commit.
  `gitops.git` now decodes its output with undecodable bytes replaced, and
  `binary=True` returns the bytes untouched: `change.patch` and `commit.log` are
  written as git printed them, so the patch still rebuilds the commit.
- **A review reads the diff the commit holds, or is refused.** Its `git diff --binary`
  went through the text path, which also turns `\r\n` into `\n`: the reviewers and
  the judge read, and `change.patch` kept, content the reviewed commit does not hold.
  The diff is now read as bytes, written to `change.patch` as is, and handed to the
  panel decoded strictly; a diff holding bytes that are not UTF-8 is refused before
  any reviewer runs, naming the file. The range's `git log` is split on its own
  separators, so a subject holding `\r` no longer crashes the attribution, and the
  tripwire reads `core.hooksPath` as bytes decoded like a path, so a value that is not
  UTF-8 is watched exactly instead of crashing the arming.
- **A forged reflog subject names no commit.** The branch reflog and the `HEAD` log an
  unconfined write reads were decoded with replacement and split with
  `str.splitlines`, so a subject holding `\r` -- which git never writes, but an agent or
  a hook can -- became an entry of its own, and the commit id inside it was attributed:
  an unrelated commit could be recorded as a hook's or the agent's. Both logs are now
  read as bytes and split on git's own separators (`-z` for `git reflog`, `\n` for the
  file); an entry whose id is not a commit id (40 or 64 lowercase hex) is never
  attributed -- the branch reflog then fails the attribution, the `HEAD` log reads as
  rewritten.
- **A failure after the engine's commit finalises the run instead of leaving it for the
  operator quarantine.** An exception raised by the live engine from its commit through
  publication left `run.json` at `running`, the pending write and the unconfined intent
  behind, so the next run found the operator quarantined (`stale_unconfined_intent`). The
  write is now published as `failed` with its lineage compromised (`engine_error`),
  `run.json` is written and the intent removed -- only once the lineage holds no pending
  write. Every commit is attributed first: the ones the commit step named, or, when it
  failed before naming them, the ones recovered from the branch, `HEAD` and the branch
  reflog; a commit no one can name keeps the pending write and the intent. A process
  that dies there, an interruption, a git found tampered, or a publication that fails
  in turn still leaves the intent, and the quarantine of a genuinely stale one is
  unchanged.

## 0.5.1 — 2026-09-26 (tag `headless-agents-v0.5.1` after merge)

Ticket ha-051-agy: headless-agents 0.5.0 refused the agy rail outright, because agy
1.2.11 had no passing isolation proof -- the live proof measured the repository's own
`CLAUDE.md`/`AGENTS.md`/`GEMINI.md` reaching the model with no tool call involved.

### Fixed
- **The ephemeral HOME agy starts in never sits under a git work tree.** Its root is the
  first of `XDG_RUNTIME_DIR`, the operator's `~/.cache/headless-agents/agy-homes`, then
  the system temporary directory, whose ancestry up to `/` carries no `.git` (file or
  directory); if none qualifies the run is refused before anything starts, and the
  ancestry is checked again right before agy launches. Otherwise agy's upward walk
  would reach that repository's instruction files (review of PR #228; on the
  operator's host `/tmp` itself is a repository). Residual: a writer racing that last
  check -- only the operator or root above the first two roots, any local user only
  under the temp-directory fallback.
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
