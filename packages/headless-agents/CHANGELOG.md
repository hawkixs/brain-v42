# Changelog — headless-agents

The versioned contract a consumer pins (red-rail ADR-0003, Brain ticket 04bc1f4a).
The surface under contract: the `AgentProvider` protocol (`build_command`,
`child_environment`, `prepare_home`, `tool_call_completed`, `run`), `RunSpec`,
`RunResult` / `TokenUsage`, `CapabilityProfile` / `McpServer` / `ToolGuard` /
`Credentials`, `chain.run_chain`, `envelope.unwrap`, and the exit codes
`PROVIDER_FALLBACK_EXIT_CODE = 3` and `TIMEOUT_EXIT_CODE = 124`. A change to any of
these is a **breaking** entry below and a major-or-minor bump while the package is 0.x;
a new provider is additive.

## Tags

Each version is tagged on the brain-v42 repository as `headless-agents-vX.Y.Z`, on the
commit that shipped it — deliberately outside the `v*` pattern, which names brain-v42's own
version and drives its release workflow. Pin it:

```sh
uv add "headless-agents @ git+https://github.com/hawkixs/brain-v42.git@headless-agents-v0.2.0#subdirectory=packages/headless-agents"
```

The earlier `v0.6.0` tag (2026-09-14) also carries 0.1.0 and stays valid; it is the last
time the member rode a brain-v42 tag.

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
