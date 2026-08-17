# Dream agents: migration Claude CLI → Codex CLI

Date: 2026-07-13
Status: accepted for implementation as a guarded canary

## Problem

The six agentic Dream phases (`scan`, `clean`, `connect`, `synth`, `promote`,
`reorg`) currently run through `claude -p` and consume the user's Claude
subscription. The user's larger allowance is now attached to ChatGPT/Codex.
The migration must move those six phases to the authenticated Codex CLI without
weakening the Dream write guardrails or corrupting phase telemetry.

`ROADMAP` and `EXTRACT` are deliberately separate. They call the NVIDIA API
directly and are not affected by replacing `claude -p`; the current ROADMAP
timeout therefore remains a distinct follow-up.

## Requirements

- Use the existing ChatGPT login (`codex login status`), not an OpenAI API key.
- Preserve prompts, dependency injection, phase ordering, retries, validators,
  preflight skips, killswitches, DRY/WET semantics, and post-run alerting.
- Default the completed migration to Codex, with Claude available only through
  an explicit rollback setting.
- Never fall back automatically from Codex to Claude after a phase starts: a WET
  phase may already have committed MCP writes.
- Expose only the Brain MCP server and only the exact tools required per phase.
- Disable shell, filesystem mutation, web, apps, skills, and sub-agents.
- Keep a hard wall-clock timeout because Codex has no stable `--max-turns`
  equivalent.
- Store the final response, JSONL events, and stderr separately.
- Keep the existing Claude OTEL parser unchanged; add a Codex JSONL parser.
- Record unavailable subscription-cost metrics as `NULL`, never as a fake zero.
- Make a single `scan` DRY canary pass before enabling all six phases.

## Options considered

1. **Provider adapter plus guarded canary (selected, confidence 0.92).** Add a
   Codex runner and provider-aware telemetry, retain an explicit Claude rollback,
   then validate `scan` DRY before the full switch. This preserves operational
   contracts and gives the smallest safe blast radius.
2. **Hybrid rollout by phase (viable, confidence 0.86).** Move the expensive
   former Opus phases first, then the fast phases. It reduces Claude consumption
   gradually but keeps two providers and two failure modes active longer.
3. **Direct all-at-once shell replacement (viable, confidence 0.81).** Replace
   `claude -p` inline with `codex exec`. It is quick, but command construction,
   process cleanup, telemetry, and security policy become difficult to test.
4. **OpenAI Responses API (rejected, confidence 0.07).** It is structurally
   clean but would use API billing rather than the user's ChatGPT subscription.
5. **Codex cloud scheduled task (rejected, confidence 0.04).** The local private
   Brain MCP and host-side systemd contracts make a cloud runner a poor fit.
6. **Local open-weight model (rejected, confidence 0.03).** It avoids subscription
   usage but introduces a new inference stack and uncertain tool-use quality.

## Architecture

`scripts/dream.sh` remains the orchestration policy owner. It selects provider,
model, reasoning effort, paths, and timeout, then delegates the Codex process to
`scripts/dream/codex_runner.py`.

The runner is a narrow process adapter. It:

1. validates phase/model/timeout and the MCP token environment;
2. creates a runtime directory outside the repository;
3. builds an isolated `codex exec` command;
4. starts Codex in its own process group;
5. writes JSONL stdout and stderr to separate files;
6. terminates the whole process group on timeout and returns exit `124`.

The command uses `--ignore-user-config`, `--strict-config`, `--ignore-rules`,
`--ephemeral`, `--skip-git-repo-check`, `--sandbox read-only`, and `--json`.
Configuration overrides disable shell/unified exec, multi-agent, web/browser,
computer use, image generation, apps/plugins, and skill/workspace dependencies.
The Brain MCP is re-declared explicitly with `required=true`, bearer-token env
indirection, per-phase `enabled_tools`, and bounded startup/tool timeouts.

### Phase policy

| Phase | Model class | Reasoning | MCP tools |
|---|---|---|---|
| scan | fast | medium | decay status, consolidation candidates, list, search |
| clean | fast | medium | search, get, consolidation, decay, merge, delete, list |
| connect | fast | medium | backfill links, list orphans, assign domain |
| synth | deep | high | clusters, get, learn, save snippet, search, list, neighbors, path |
| promote | deep | high | get, search, propose ADR, create runbook, list ADRs/list, neighbors/path |
| reorg | deep | high | search, list, get, update |

Defaults are `gpt-5.6-terra` for fast phases and `gpt-5.6-sol` for deep
phases. Environment variables can override both models and reasoning effort.

The current prompts are the semantic guardrail. The MCP allowlist is the
enforced capability boundary. In phase DRY mode, mutating tools remain available
only where the existing prompt must simulate or prepare a result; the Brain MCP
tools themselves and phase validators continue to enforce their DRY/WET
contracts. A later hardening can split tool lists by dry/wet once every tool's
mutation semantics are machine-readable.

## Data flow

```text
rendered prompt + dependency reports
              |
              v
     codex_runner.py --phase ...
              |
              +--> Codex final response --> <date>_<phase>.log
              +--> Codex JSONL events  --> <date>_<phase>.events.jsonl
              +--> Codex stderr        --> <date>_<phase>.err.log
                                      |
                                      v
                           provider-aware dream parser
                                      |
                                      v
                                 dream_runs row
```

For Codex, `turn.completed.usage` supplies input, cached input, and output
tokens. Completed MCP tool-call items supply tool-call counts. `turn.failed`,
`error`, and stderr supply failure details. `cost_usd`, cache-creation tokens,
and API-call count are nullable because the subscription event stream does not
guarantee them.

The systemd unit applies `UMask=0077` because JSONL MCP results may contain full
Brain records; reports, events, and stderr therefore remain private to the user.
It also waits up to 30 seconds for the configured MCP server's auth-exempt
`/health` route. The independently managed HTTP service must already be active;
the Dream unit does not bypass its production-enable gate.

## Failure policy

- Missing Codex binary, inactive ChatGPT login, missing MCP token, malformed
  JSONL, missing final report, MCP startup failure, and non-zero Codex exit are
  phase failures.
- Timeout returns `124`, preserving Dream's existing timeout classification.
- Hard failures retain the existing one retry except PROMOTE; timeouts are not
  retried.
- No provider fallback occurs within a run.
- Existing PROMOTE and REORG validators remain authoritative after Codex exits.
- A failed canary leaves production configured for Claude until the failure is
  understood; a successful canary permits the explicit full flip.

## Verification and rollout

1. Unit-test command construction, per-phase allowlists, process timeout cleanup,
   and JSONL telemetry parsing using fixtures.
2. Update shell contract tests for provider selection and unchanged phase
   timeouts/retry semantics.
3. Run the focused unit/integration suite and shell syntax checks.
4. Run one real `scan` with `DRY_RUN=true`, fast model, medium reasoning, and a
   2–3 minute cap.
5. Verify non-empty report, `turn.completed.usage`, Brain-only MCP calls, no shell
   or web items, no business-data mutation, and a correct `dream_runs` row.
6. Flip all six phases to Codex, keep `BRAIN_DREAM_AGENT_PROVIDER=claude` as the
   documented rollback, reload the user service, and inspect the next daily
   check.

## Out of scope

- Migrating or repairing NVIDIA-backed ROADMAP/EXTRACT.
- Changing phase business logic or prompt output formats.
- Automatic multi-provider failover.
- Repricing subscription usage into estimated dollar cost.
