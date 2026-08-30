# Live client activity for the brain — generalizing "Codex activity"

**Date**: 2026-08-06
**Status**: design validated by the operator, implementation plan to be written
**Brain ticket**: `2dfbb83d-f6cf-4570-9b13-502acc8c776c`
**Scope**: end to end — `brain_v42` then the red-monitor panel

## The problem, measured

The red-monitor "live workload — Codex activity" panel is specific to
Codex, from the receiver down to the labels. The operator wants the concept for everything
that connects to the brain, starting with Claude sessions.

The existing chain, read and not assumed:

1. The Codex CLI pushes its own OTLP logs over loopback to `POST /v1/logs` on the
   sidecar — `metrics/server.py:156`. Hardened: loopback-only (403 otherwise),
   256 KiB, 4 requests in flight (503), strict `Content-Type` and encoding.
   Configured in `~/.codex/config.toml`: `endpoint =
   "http://127.0.0.1:9200/v1/logs"`.
2. `metrics/codex_telemetry.py` turns this into an in-memory pseudonymized registry:
   `conversation.id` → HMAC `codex-<32hex>`, TTL 600 s, cap of 64 conversations,
   dedup by fingerprint, only 5 attributes projected.
3. `metrics/cockpit.py:113` injects it into `GET /api/cockpit`.
4. red-monitor: `internal/web/brain.go` re-proxies the raw bytes (cache TTL
   1.5 s + stale-on-error); `frontend/src/tabs/brain/BrainActivity.jsx`
   displays it. "Codex" labels are also hardcoded in `BrainStatusBar.jsx:55` and
   `brainPresentation.js:50` (`codex-anonymous`).

### The bias of nature

This panel does not measure what connects to the brain. It measures what the Codex
CLI chooses to say about itself. A Codex that never calls a brain tool
still appears in it. Generalizing by simply adding Claude to the same
receiver would inherit the bias instead of correcting it.

It's the same category error that was fixed on the dream side: confusing a
proxy with what we actually want to know.

## Existing ground

What the brain already observes about its clients, without adding anything:

- **The provenance actor.** `brain_v42/provenance.py` (shipped 2026-08-06,
  commits `a66c92ba` and `fb34a826`) exposes `normalize_agent`, an actor
  ContextVar, and `is_human_actor`. `ProvenanceMiddleware`
  (`mcp/provenance_middleware.py`, registered **unconditionally** at
  `mcp/server.py:261`) sets the actor before every tool call.
  `metrics/instrument.py:51` no longer reads the header: it calls
  `get_current_actor()`. Identity is therefore already out of the metrics path.
- **The `brain_sessions` table** (`db/tables.py:689`): `project_key`,
  `client_key`, `status`, `last_heartbeat_at`, capture ledger.

Two measured caveats, not to be rediscovered:

- `brain_sessions.last_heartbeat_at` only moves on an explicit user
  command (CLAUDE.md rule: no hook touches the lifecycle). An `open`
  session can have been dead for two days. **Liveness on the brain side
  is tool-call throughput, not the session table.** This spec therefore
  does not use `brain_sessions` as a liveness source.
- The actor is a **project**, not a session: Claude Code sends `${PWD}`,
  normalized to a basename. Two Claude sessions in `brain-v42` are
  indistinguishable without new correlation.

## Measurements from 2026-08-06

Made, not deduced:

- `CLAUDE_CODE_SESSION_ID` is exported in the environment of a Claude
  Code session (observed value: a UUID). Measured from an `sdk-cli` session with
  `CLAUDE_CODE_CHILD_SESSION=1` — **the environment of an interactive session
  may differ**.
- `.mcp.json` expands `${VAR}` in headers: `X-Brain-Agent: "${PWD}"` is
  in place in `~/.claude.json:3807`.
- `~/.codex/config.toml` sends `X-Brain-Agent = "codex"` as a **static
  literal**, with no conversation identifier. Codex therefore cannot be
  joined, and all of its tool calls collapse onto a single `codex` actor.
- `internal/web/brain.go` proxies the raw bytes: adding fields to
  `/api/cockpit` requires **no** Go work at all.

## Structuring decisions

### Universe: merging the two sources

Option chosen by the operator: neither "the machine's agents" (OTLP only,
which inherits the bias), nor "the brain's clients" (derived only, no tokens or
cost), but **the two merged** — one row per client, columns filled by
whichever source is available.

### Row unit: the session, with an accepted residual

**One row = one session when a session identity exists; otherwise a
residual row per actor, labeled "unattributed".**

Consequence for Codex: N conversation rows (tokens, turns, no brain
calls) **plus** a `codex — unattributed` row (its tool calls, no
tokens). This apparent redundancy is the honest result: Codex doesn't say
which conversation is calling the brain. The panel shows this gap instead of
papering over it with an invented correlation.

Consequence for Claude: rows joined from both sides, plus a residual for
any client that doesn't send the session header.

### Join in pseudonym space

The merge key is the **HMAC of the session UUID**, never the UUID. Both
sides hash with the process secret already used by `codex_telemetry`.
The current property — no raw identifier leaves the registry — is
preserved, not bypassed.

### Brain-side observation in the middleware, not in the instrumentation

The liveness increment hooks into `ProvenanceMiddleware`, which runs
unconditionally, not into `instrument_tool`, which is only wired in if a
metrics collector exists. A silently mute provenance is worse
than no provenance at all; the same reasoning applies to activity.

The registry records `calls` and `last_seen`, nothing more — no tool name.

**A naive increment would be wrong in production.** Measured on 2026-08-06 (commit
`58329a84`): in the `compact` profile, the gateway executes the internal call via
`FastMCP.call_tool` with `run_middleware=True` (default, fastmcp 3.4.2), so
`on_call_tool` fires **twice** per client call —
`['brain_call_tool', 'inner_tool']`. A `calls += 1` would therefore double-count in
`compact`, which is the production profile, and single-count in the native profile: two
`brain_calls` values that aren't comparable to each other. `last_seen` is insensitive to
this, being idempotent.

Fix chosen: a **re-entrance guard** — a depth ContextVar,
incrementing only at `depth == 1`. Preferred over a filter on gateway
names, which would miss `brain_find_tool` and the lifecycle tools, and
would need maintaining every time a gateway is added. The guard counts exactly
one event per client call, in both profiles.

Do not cite learning `b77dba43` on this point: it is refuted by
`310a9953`. Synergy worth noting for later — ticket `c352eaaa` (removal of
the metrics monkey-patch) becomes viable, and metrics as well as activity
would then share this middleware and this guard.

### The registry changes name, not discipline

`CodexConversationRegistry` becomes `ClientActivityRegistry`
(`metrics/client_activity.py`). TTL 600 s, cap, HMAC, dedup by fingerprint: the
pattern is good, it's the name that's too narrow. The two OTLP decoders
become two projections into this single registry.

### The process boundary, and why the existing rail doesn't fit

Measured on 2026-08-06: these are **two processes**, not one.

| PID | Command | Port | Owns |
|-----|----------|------|-------|
| 2925883 | `-m brain_v42.mcp.server --http-server` | 8765 | `ProvenanceMiddleware` |
| 1144772 | `-m brain_v42.metrics` | 9200 | OTLP receiver, registry, `/api/cockpit` |

Two consequences that the first draft of this spec ignored:

1. The middleware and the registry share no memory. The data-flow diagram
   had them converging on the same object — that was wrong.
2. The HMAC secret is `secrets.token_bytes(32)` **per process**
   (`codex_telemetry.py:320`). "Same HMAC on both sides" was therefore
   impossible: two distinct secrets, a join that would never match.

**The existing cross-process rail doesn't fit.** `flusher.py:34` writes to
`process_metrics` every **30 s**, and `collector_db.py:137` reads a 60 s
window. For a panel polling every 2 s, `brain_calls` and `last_seen` would be
30 s stale while `tokens` would be fresh at 2 s — two different
freshnesses in the same row, exactly the kind of mixing that misleads.
Rejected.

**Chosen: a second loopback receiver on the sidecar.** The MCP process pushes
its observations to `:9200`, with the same hardening as the existing OTLP receiver —
loopback-only, bounded body, capped in-flight requests, fail-closed rejection.

**Hashing happens on reception, on the sidecar side.** So no shared secret,
and the per-process `secrets.token_bytes(32)` stays intact. The raw session UUID
then crosses a loopback socket between two local processes — this is
**exactly** the posture of the current OTLP receiver, which already receives
Codex's raw `conversation.id` values in the clear over loopback and hashes them on
arrival. We're adding an emitter to a receiver that already exists, not changing the
property: "no raw identifier leaves the registry" is about what goes out in the
payload, not what comes in over loopback.

The alternative — a shared secret provisioned to both processes, each hashing
on its own side — avoids the UUID transit but turns an ephemeral secret into a
configuration item to provision and rotate. Rejected for that reason.

### Additive contract

We **add** `clients[]` to `/api/cockpit` and leave `activeConvs`,
`metrics.active_convs` and `metrics.ctx_tokens` intact while the panel
transitions. No breakage for the existing panel.

## Data flow

```text
     MCP process (:8765)                       metrics sidecar (:9200)
┌───────────────────────────┐        ┌──────────────────────────────────────┐
│ ProvenanceMiddleware      │        │ Codex CLI ────OTLP────┐              │
│  X-Brain-Agent            │        │ Claude Code ──OTLP────┤              │
│  X-Brain-Session          │        │                  decoders (2)        │
│  re-entrance guard        │        │                       │              │
└─────────────┬─────────────┘        │                       ▼              │
              │ bounded loopback push│        ClientActivityRegistry        │
              └──────────────────────┼──────────────→ (HMAC on reception)   │
                                     │                       │              │
                                     │  cockpit.py → /api/cockpit.clients[] │
                                     └───────────────────────┬──────────────┘
                                                             │
                                        red-monitor brain.go (proxy unchanged)
                                                             │
                                             frontend "Live workload"
```

## Shape of a row

```json
{
  "id": "<pseudonyme>",
  "kind": "session" | "unattributed",
  "agent": "claude",
  "actor": "brain-v42",
  "started": "12:31",
  "last_seen_s": 4,
  "model": "claude-opus-5",
  "turns": 12,
  "tokens": 128000,
  "cost": 1.23,
  "brain_calls": 37
}
```

Every unmeasured field is `null`, never `0` — a doctrine already in place in
`cockpit.py` ("None = not measured; 0.0 would be indistinguishable from a real zero").

Two fields look alike and don't mean the same thing:

- **`agent`** comes from the OTLP source and from it alone: the decoder that produced
  the row knows whether it was talking to Codex or Claude Code. A row fed
  only from the brain side has `agent: null`. Do not infer it from the actor name: that
  would be guessing.
- **`actor`** is the normalized value of `X-Brain-Agent`, as
  `provenance.normalize_agent` produces it. It's a vocabulary already in place
  in the code, and it is deliberately heterogeneous: Claude Code puts the
  basename of the **LAUNCH directory** there (`${PWD}` expanded — this is NOT a
  project name: launched from `/home/hawixs` the actor is `hawixs`, from a
  worktree it's the worktree's name, and the same project produces `brain_v42`
  (underscore, basename) or `brain-v42` (hyphen, literal from `.mcp.json`) depending on
  the path taken — measured, thread of ticket `a3fa6696`); Codex puts a
  service label there (`codex`). The panel displays it as-is without claiming
  that it's a project.

By row type: an `unattributed` row has `tokens`, `turns`, `cost`, `model` and
`agent` set to `null`, and its `id` is derived from the actor. An OTLP-only row has
`actor` and `brain_calls` set to `null`. A joined row has all of them.

## The red-monitor panel

Separate repo: `~/hawkixs_infra/git_repo/ReD_v1/projects/red-monitor`, with its
own conventions and its own test suite. No Go work: `brain.go` proxies
the raw bytes, the new fields arrive on their own.

`BrainActivity.jsx` stops being Codex-specific:

- it consumes `clients[]` instead of `live.activeConvs`;
- the title changes from "Codex activity" to "Live workload", and the hardcoded
  labels disappear from `BrainStatusBar.jsx:55` (`<StatusMetric label="Codex">`)
  and from `brainPresentation.js:50` (`shortPseudonym` returns `codex-anonymous`
  by default — becomes neutral);
- an `unattributed` row is visually distinct and carries its reason, so
  that "Codex doesn't say which conversation is calling the brain" reads
  without needing to know this spec;
- the warning "declared by the client, not proven" is **in the panel**,
  not just in the documentation.

`Brain.test.jsx` already carries `activeConvs` fixtures; they remain valid
as long as the contract stays additive, and they switch over with the panel.

## The spike is a gate, the design survives it

Task 1 of the plan, before any detailed design. Two things to prove:

1. `${CLAUDE_CODE_SESSION_ID}` does expand in a `.mcp.json` header, in
   an **interactive** session (today's measurement comes from an `sdk-cli` session).
2. The `session.id` attribute of the Claude Code OTLP carries **the same UUID** as this
   variable.

If the spike fails, nothing collapses: every row becomes `unattributed`
plus OTLP-only rows. This is exactly the "two unjoined sections" mode
that was ruled out during the brainstorm. The design degrades, it doesn't get rewritten.

## Tests

TDD, Red-Green-Refactor cycle like the rest of the project.

Unit:

- Claude Code OTLP decoder — attribute schema distinct from Codex
  (`claude_code.user_prompt`, `claude_code.api_request`; `session.id`,
  `input_tokens`, `output_tokens`, `cost_usd`);
- join in pseudonym space: two sources, same UUID, a single row;
- unattributed residual: calls without a session header → actor row;
- **re-entrance guard**: a call in the `compact` profile produces exactly one
  increment despite the two `on_call_tool` firings — the test must
  simulate the real nesting (gateway then inner tool), otherwise it goes
  green without proving anything;
- observation receiver: hardening identical to `/v1/logs` — non-loopback
  rejected, out-of-bound body rejected, saturation as 503, malformed fail-closed;
- non-regression of the bounds: TTL, cap, dedup, malformed rejections;
- payload shape: `null` and not `0` for every field with no source.

The spike is manual and cannot be automated: it depends on the environment
of an external client.

## Accepted limitations

- **`X-Brain-Agent` and `X-Brain-Session` are declared by the client, and are therefore
  spoofable.** A hygiene signal, not a security boundary — the same posture as
  the session `client_key`, "declared, not proven". `collector.py:100`
  already caps cardinality against exactly that. This warning must
  accompany the panel: without it, someone will one day read it as proof.
- **Enabling Claude Code's OTLP is a config change on each
  client**, not a server-side flip.
- **A change of posture on privacy**: the rows expose the project name
  (derived from `${PWD}`), where the Codex panel was entirely
  pseudonymous. An explicit choice by the operator on their own machine, not a side
  effect.
- **Codex will remain partially blind** as long as its MCP config doesn't carry a
  conversation identifier. This is not a flaw of this design.
- **One more emitter toward the sidecar is one more surface.** Loopback-only
  and bounded like `/v1/logs`, but the project's network boundary is tracked
  closely: to be recorded in the "Tracked network boundary" block of CLAUDE.md at
  rollout time, not after.
- **The raw session UUID transits over loopback** between the two local
  processes. Accepted, and identical to what the OTLP receiver already does. To be
  reconsidered if the two processes ever stop sharing the machine.

## Out of scope

- Any correlation with `brain_sessions`: the table is not a liveness
  source (see "Existing ground").
- Row persistence: the registry is in memory, bounded, and loses everything on
  restart. An aggregated history is a separate effort.
- The red-monitor Claude Code usage dashboard
  (`internal/claudeusage`, daily aggregates from the JSONL transcripts):
  retrospective by nature, unrelated to real time.
- Removing `activeConvs` from the payload, deferred to the panel switch-over.
- Application authentication of the sidecar, out of scope here.
