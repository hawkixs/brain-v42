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
  the answer to (`report_log` for codex, agy and opencode; the bytes this run appended to
  `raw_log` for claude). Verbatim — an answer that is itself JSON is never re-read as an
  envelope — and `None` when the run failed or answered nothing.
- `RunResult.run_id`, `RunResult.stderr_log`, `RunResult.raw_log`, and
  `RunResult.to_dict()`: the JSON-safe schema-1 form (`result.RESULT_SCHEMA_VERSION = 1`).
  `context`, `workspace` and `branch` belong to the key set from schema 1 and stay `null`
  until lots 2 and 4 fill them.
- `RunSpec.run_dir`: the logs a caller leaves unset default to `report.log`,
  `events.jsonl`, `stderr.log` and `raw.log` inside it (explicit paths still win), its
  name is the `run_id`, and the run writes `result.json` there.

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
