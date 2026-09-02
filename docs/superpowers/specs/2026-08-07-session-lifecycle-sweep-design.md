# Session lifecycle — draining ghost sessions

**Date**: 2026-08-07
**Status**: design validated by the operator, implementation plan to be written
**Brain ticket**: `2bd14b24-ccfe-4372-adf2-245b00304402` (idea, opened by red-shrik)
**Neighboring tickets**: `7ffe0e8a` (auto-heartbeat), `2dfbb83d` (identity), `d04dc588` (checkpoint)
**Scope**: `brain_v42` alone, across all projects

## The problem, measured

Recorded on 2026-08-06 on production, not assumed:

```
21 sessions status='open'   ·   17 STALE   ·   4 active the same day
```

The 17 dead ones span 2026-07-13 to 2026-07-27, i.e. **10 to 24 days**. Their
`client_key` say where they come from: `codex-factory-28aeb338-*`,
`codex-github-migration-20260719-*`, `red-session-orchestrator-plan-*`,
`auto-discord-worktree-*`. One per dispatched agent, none closed.

They span **nine projects** — auto-discord, red-lab, red-arena, red-codex,
red-story, red-watcher, red-viewer, red-monitor, claude-dev-pc. This isn't a defect
of brain-v42, it's a regime of the whole ecosystem.

The original ticket reported 39 sessions manually cleaned up the day before. **17
had already re-accumulated.** Without a mechanism, manual cleanup is a perpetual
chore.

### The two lies of `last_heartbeat_at`

On the same day, the signal lied in both directions:

- **False-dead** — the purge would have abandoned a LIVE session (`9b6f7e18`,
  mid-work) because the heartbeat only moves on explicit command.
- **False-alive** — 17 sessions dead for weeks look open, because **nothing closes
  them**.

These two modes have distinct causes. The false-dead comes from the declarative
heartbeat; the false-alive comes from the absence of an automatic terminal state.
The 17 were correctly detected as `stale` — they simply stayed `open`.

### What a technical guard can't do

`is_human_actor` was evaluated as an opening guard, and ruled out on the evidence:

| Observed actor | Classified |
|---|---|
| `brain-v42`, `red-shrik`, `codex` | human |
| `dream-codex-*` | machine |
| `${PWD}` unexpanded | `_unexpanded` → machine |

It separates Dream from the rest — what it was written for — but **not an operator
from an agent**, nor a parent from its subagent: a subagent inherits its parent's
MCP config, hence the same `X-Brain-Agent`. A guard of "only humans open a session"
would refuse Codex and let every Claude subagent through.

The three coexisting identity schemas confirm that none of them designates a
session:

```
.mcp.json (project)      X-Brain-Agent = "brain-v42"    a project key
~/.codex/config.toml     X-Brain-Agent = "codex"         a client identity
~/.claude.json (global)  X-Brain-Agent = "${PWD}"        a template, expansion unproven
```

## Decisions

### D1 — A session is a unit of work, not an actor

A session belongs to the actor that has both the lifetime **and** the mandate to
close it. Subagents open none; their traceability goes through per-artifact
provenance, which exists and works.

Foundation adopted by the operator: **if a subagent consults the brain, it is for
the operator.** A subagent's activity IS the operator's work.

This framing changes the nature of the attribution problem. What looked like an
irreducible ambiguity — a subagent indistinguishable from its parent — becomes the
correct semantics: attributing to the pair *(project, actor)* designates the
operator's session, and that is the intended result.

D1 is **not technically enforceable**, and that isn't an oversight. It's a doctrine.
Since it touches nine projects, it belongs to a separate ecosystem ticket, out of
scope for this spec.

### D2 — The terminal state of a dead session is `abandoned`, never `ended`

`abandon` is already the exact semantics: it doesn't touch the focus, doesn't
require a summary, and preserves captures and their exclusivity.

A session nobody closed has, precisely, **no judgment to preserve**. Auto-abandon
honestly names what happened, whereas a server-derived summary would pass off
recopied measurable state as judgment — which the focus doctrine explicitly forbids.

### D3 — No dependency on auto-heartbeat

The original ticket mandated "`7ffe0e8a` first", on the grounds that staleness needs
to be trustworthy before closing on it. The measurement makes this dependency
unnecessary:

| Population | Age |
|---|---|
| Most recent ghost | 10 days |
| Oldest live session | 0 days |

**A ten-day gap separates the two populations.** A 7-day threshold applied to
`last_heartbeat_at` as it works today — explicit only — catches the 17 ghosts and
spares the 4 live ones, with margin on both sides.

Accepted cost: a chantier running past 7 days must call `brain_session_heartbeat`
once. That's a real but bounded cost, against a dependency on an identity brick
whose only known carrier (`X-Brain-Session`) is measured non-functional.

### D4 — The sweep is a Dream phase, DRY first

Dream already provides nightly scheduling, the killswitch idiom, and a traceable
record in `dream_runs`. Above all, it provides the **DRY/WET** idiom proven by the
`reorg`, `extract` and `roadmap` phases.

Accepted particularity: this phase is **deterministic and calls no model**.
`dream_runs.model` will be `NULL` — a shape already admitted, observed on `extract`
and on the 2026-08-05 `roadmap` run.

## Mechanism

| Element | Value |
|---|---|
| Killswitches | `BRAIN_DREAM_SWEEP_ENABLED`, `BRAIN_DREAM_SWEEP_DRY_RUN` |
| Predicate | `status = 'open' AND last_heartbeat_at < now() - interval '7 days'` |
| Action | transition to `abandoned` |
| `abandonment_reason` | `auto_stale_7d` |
| Scope | all projects |
| Trace | one `dream_runs` row, phase `sweep`, `model` NULL |

The distinctive `abandonment_reason` isn't decorative: it keeps automatic
abandonment and manual abandonment **distinguishable forever**, hence separately
auditable. A manual abandon carries the reason the operator wrote; this one carries
a recognizable constant.

In DRY mode, the phase logs exactly what it **would have** abandoned and writes
**nothing** to `brain_sessions`.

## Safety and rollout

1. **DRY for several nights.** Read what the phase would have abandoned and verify
   it targets only ghosts.
2. **Re-measure the gap before the WET flip.** Never copy this spec's numbers: they
   date from 2026-08-06 and go stale. The measurement query is part of the
   procedure, not the documentation.
3. **Irreversibility.** `brain_session_resume` requires `status='open'`: an abandon
   is terminal. Three mitigations — a generous threshold, a prior DRY period, and
   capture preservation guaranteed by the existing contract.
4. **No `unabandon`.** As long as DRY has produced no false positives, building an
   undo would be solving a problem that hasn't been observed.

## Doctrinal amendment

`CLAUDE.md` today carries a categorical prohibition:

> No hook, auto-close, work delivery or end of response closes a session.

This spec would contradict it unless explicitly amended. The original intent
targeted **the agent and the client**: preventing a process from closing a live
session and destroying the closing ritual, the sole moment where non-derivable
judgment is written.

The amendment must therefore be narrow and stated, not slipped in:

- the prohibition remains complete for the agent and the client — `start`, `resume`,
  `end` and `abandon` remain explicit operator commands;
- the **server** may abandon a session with no sign of life for 7 days;
- this automatic abandon produces neither a summary nor a `next_focus`, and never
  touches the project's focus.

## Tests

- **Unit, predicate boundary**: at N−1 day the session is not touched, at N+1 it is
  abandoned. The exact boundary, not a middle case.
- **Unit, DRY writes nothing**: in DRY mode, no `brain_sessions` row is modified,
  even though the phase reports candidates.
- **Integration, invariants preserved**: the sweep alters neither `current_focus`,
  nor `focus_revision`, nor `attributed_knowledge_ids`.
- **Integration, origin distinction**: an automatic abandon carries
  `abandonment_reason = 'auto_stale_7d'`, a manual abandon keeps its own.

## Out of scope

Explicitly, and each for a reason:

- **Auto-heartbeat (`7ffe0e8a`)** — not needed here (D3). Note however that D1's
  principle unblocks it conceptually: attribution by *(project, actor)* is
  sufficient, `X-Brain-Session` was never indispensable. `is_human_actor` regains a
  useful role there — excluding Dream's writes from any automatic attribution, which
  the ticket requires.
- **Session identity (`2dfbb83d`)** — measured non-functional, and made non-blocking
  by D3.
- **Semantic checkpoint (`d04dc588`)** — judged BLOCKED by its own audit, a separate
  spec is required.
- **Doctrine "subagents don't open sessions"** — touches nine projects, hence a
  separate ecosystem ticket.
- **Manual cleanup of the 17 ghosts** — DRY will list them, the WET flip will
  process them. That's the best demonstration of the mechanism, and skipping it
  would make success unverifiable.

## Known limits

- D1 is not technically enforceable and won't be with the current bricks. A future
  drift is possible without any guard raising an alarm; only the count of open
  sessions will reveal it.
- The 7-day threshold is calibrated on a single measurement. A change in work regime
  — longer chantiers, deliberately persistent sessions — would invalidate the margin
  and require re-measuring.
- `last_heartbeat_at` stays declarative until `7ffe0e8a`. A session that is alive
  but silent for more than 7 days will be wrongly abandoned; that's the compromise
  accepted in D3, mitigated by the fact that abandonment preserves captures.
