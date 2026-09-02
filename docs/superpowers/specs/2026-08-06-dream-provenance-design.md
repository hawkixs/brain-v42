# Corpus provenance — telling dream metabolism apart from human activity

**Date**: 2026-08-06
**Status**: design validated, implementation plan to be written
**Scope**: workstream A of a batch of four (see "Out of scope")

## The problem, measured

Three failures observed in production on 2026-08-06:

1. **PROMOTE has been looping for 23 nights.** Learning `1d1037e8`
   ("brain-v42 architecture overview") has been evaluated every night since
   2026-07-07 and returns the same verdict, `classification_uncertain`. 22 lines in
   `dream_promotions`. Across all PROMOTE reports: 64
   `classification_uncertain` against 18 actual promotions.

2. **The preflight gate no longer fires.** Built to spare ~40% of
   deep-phase nights, it has returned **48 RUN for 2 SKIP**. Last SKIP on
   2026-07-09.

3. **`access_count` is contaminated.** PROMOTE's maturity filter
   selects on `access_count >= 3`, and the dream itself increments this
   counter by paginating the corpus every night (REORG: 63 to 106 tool calls).

### Single root cause

`promote_prepare.py` carries an anti-rejudgment cache that excludes a learning already
judged uncertain on its current version, via `u.created_at >= l.updated_at`. This
cache cannot structurally hold:

```
verdict PROMOTE       2026-08-06 04:06:13 UTC
learnings.updated_at  2026-08-06 04:08:51 UTC   ← later, so readmitted
```

`trg_learnings_updated` is an **unconditional** `BEFORE UPDATE`, and
`decay_flusher` writes `UPDATE learnings SET access_count=…, last_accessed_at=…`,
a pure counter write. **Merely reading an entity is therefore enough to refresh its
`updated_at`** and invalidate the verdict returned two minutes earlier.

The repo already knows about this defect and has worked around it in exactly one place —
`repositories/pg_learning.py:8`: *"validate() stays local: it must update
validated_at WITHOUT bumping updated_at"*. Migration 040 solved the same
problem for `project_contexts` with `focus_updated_at`. The concept is understood,
it has just never been laid down at the entity schema level.

## Model: two orthogonal axes

The system must answer two distinct questions, which `updated_at` conflates:

- "**Who** touched this row?" → *actor* axis
- "Did the **content** change, or only a counter?" → *nature of the write*
  axis

| Consumer | Real question | Axis |
|---|---|---|
| PROMOTE anti-rejudgment cache | has the content changed since my verdict? | nature |
| PROMOTE maturity filter | are **humans** reading this learning? | actor |
| Preflight gate | has the corpus moved for a reason **other than me**? | actor |

## Existing ground

The caller's identity **already reaches the server** and simply goes
unwritten:

- `scripts/dream/codex_runner.py:262` sends `X-Brain-Agent: dream-codex-<phase>`.
- Interactive Claude Code sessions send `${PWD}`.
- `metrics/instrument.py:_normalize_agent()` already normalizes these values
  (`/home/u/git/red-lab` → `red-lab`, unexpanded `${…}` → `_unexpanded`,
  empty → `unknown`).

This signal serves only the metrics collector today. `access_log` has no actor
column; `decay_flusher` increments `access_count` without knowing who read it.

**Provenance doesn't need to be built, it needs to be wired in.**

Caveat: the header is client-declared, hence forgeable
(`metrics/collector.py:100` says so and caps cardinality). Same stance as
session's `client_key`: *declared, not proven*. A hygiene signal, not a
security boundary.

## §1 — Middleware: replacing the monkey-patch

`brain_tools.py:106-122` reassigns `mcp.tool` to wrap the metrics
tools. Three flaws:

- gated on `metrics_collector is not None` — with metrics disabled, no
  tool is instrumented anymore;
- order-dependent: only tools declared after line 122 are
  covered;
- mutation of a third-party object's method, fragile across version bumps.

FastMCP 3.4.2 (installed) exposes the intended extension point:
`FastMCP.add_middleware()` with an `on_call_tool` hook. A **single middleware**
carries both concerns:

```
on_call_tool:
    set the actor ContextVar      ← unconditional, always
    if collector active: measure  ← conditional, inside
```

The coupling to metrics disappears by construction, so does the
order dependency, and one monkey-patch is removed instead of adding a second.

Constraints: preserve exactly the `AuthorizationError` capture and the
latency measurement of `instrument_tool`. Do not touch `instrument_embedding`
or `instrument_reranker`, which are not tools.

**First task of the plan**: prove that `get_http_headers()` is reachable
from `on_call_tool`. If it isn't, the header read stays where
it is and the middleware only propagates the value.

**Detachable** step: it touches no schema. If it goes sideways, it can be
removed without losing the rest of the workstream.

## §2 — Migration 041

No backfill. `0` and `NULL` mean "never measured", a discipline inherited
from 040.

**a)** `access_log.actor VARCHAR(64) NOT NULL DEFAULT 'unknown'`, fed by
`_normalize_agent()`.

**b)** `access_count_human INTEGER NOT NULL DEFAULT 0` on `learnings`,
`decisions`, `snippets`, `runbooks`, `adrs`. `access_count` remains the total and its
semantics don't change: no existing consumer breaks.

**c)** `content_updated_at TIMESTAMPTZ NULL` on the same five tables. The
migration creates a `stamp_content_updated_at()` function and **one trigger per
table** (five in total), each **conditional on value change** and
carrying the column list specific to its table. Example for `learnings`:

```sql
CREATE TRIGGER trg_learnings_content_updated
  BEFORE UPDATE OF topic, insight ON learnings
  FOR EACH ROW
  WHEN (OLD.topic   IS DISTINCT FROM NEW.topic
     OR OLD.insight IS DISTINCT FROM NEW.insight)
  EXECUTE FUNCTION stamp_content_updated_at();
```

Content columns per table (measured):

| Table | Content columns |
|---|---|
| `learnings` | `topic, insight` |
| `decisions` | `title, description, reasoning, consequences` |
| `snippets` | `title, code` |
| `runbooks` | `title, description, trigger, steps` |
| `adrs` | `title, context, decision, consequences` |

`tags`, `project_key`, `freshness_status` and the counters are **outside** this
set: REORG normalizing a tag doesn't refresh the content. Intended
behavior.

### Assumed divergence from migration 040

040 writes `focus_updated_at` from application code, "never by a
trigger". This design does the opposite, for three reasons:

1. The `WHEN … IS DISTINCT FROM` clause gives exactly the value semantics
   040 was after: rewriting the same text refreshes nothing, a copy-over
   stays visible. That was the argument against the trigger; it falls away.
2. The focus has **one** writer. Entity content has many:
   `brain_learn`, `brain_update`, REORG, CLEAN merges, the
   backfill scripts. An application-level discipline over N writers will be
   forgotten by writer N+1 — which is literally what happened here.
3. A trigger is verified with one query; an application-level discipline is
   verified by rereading all the code.

## §3 — Actor classification

Single pure function, `brain_v42/provenance.py : is_human_actor(actor) -> bool`,
covered by a unit test enumerating the cases:

| Actor | Human? | Reason |
|---|---|---|
| `dream-codex-<phase>` | no | the dream declares itself |
| `unknown` | no | fail-closed: an unidentified caller unlocks nothing |
| `_unexpanded` | no | daemon session without `PWD` |
| everything else | yes | interactive session → basename of `PWD` |

## §4 — Data path and aggregation

### Where the actor is read — a trap to avoid

The actor must be read **at enqueue time**, in
`AccessLogger.log_access()`, and stored in the queued event:

```
middleware on_call_tool   → sets the ContextVar (request context)
log_access()              → READS the ContextVar, attaches it to the event   ← here
_flush_batch()            → inserts the event, ContextVar out of scope
```

`_flush_batch()` runs in a background task (`_run_loop`, every 5 s),
**outside the request context**: reading the ContextVar there would yield `unknown` for
everyone. The 6 call sites of `log_access` stay unchanged — only
the method's implementation changes.

### Aggregation

`decay_flusher` today aggregates `access_log` by
`(entity_type, entity_id)` → `count` + `max(accessed_at)`. It now also
aggregates `count_human`, and writes both counters:

- `access_count += count` (unchanged)
- `access_count_human += count_human`

## §5 — Rewiring the three consumers

### a) Anti-rejudgment cache — `promote_prepare.py`

```sql
u.created_at >= COALESCE(l.content_updated_at, l.created_at)
```

The fallback to `created_at` — **not** to `updated_at` — is the delicate point.
Without backfill, `content_updated_at` is `NULL` everywhere; a fallback to
`updated_at` would reproduce the bug identically. `created_at` states the one thing
that's known: *the content has never been observed to change, so it has the age of the
row.* That's a measured fact, not a fabricated value.

Immediate effect: 2026-08-06 verdict ≥ `created_at` of 2026-03-23, so
`1d1037e8` exits the pool on the very first night.

**Assumed blind spot**: a row whose content actually changed *before* the
migration but *after* its last verdict will be wrongly excluded. It self-corrects
on the next edit.

### b) Maturity filter — `promote_prepare.py`

`l.access_count >= 3` becomes `l.access_count_human >= 3`.

**Consequence to announce**: PROMOTE's pool will be empty for a while, the
time it takes for human reads to accumulate from zero. This is not a
regression but the correct outcome — who read what before the
migration is unknown. Nothing is lost: PROMOTE has produced nothing for 23 nights. Anyway,
workstream C will replace this gate with the review verdict.

### c) Preflight gate — `scripts/dream/dream_preflight.py`

Two changes:

1. `greatest(created_at, updated_at)` → `greatest(created_at, content_updated_at)`,
   which eliminates counter-write noise;
2. exclude entities tagged `dream:generated` from the mutation signal. Without
   this, SYNTH guarantees by creating 3 insights that the following night will synthesize
   on top of its own output — scheduler-level echo-drift.
   Verified: all five tables carry a `tags` column.

The gate's fail-safe nature is preserved: any error or uncertainty prints
`RUN`.

## §6 — Rollout and verification

Strict TDD (CLAUDE.md requirement): red test first at each step.

Three independent steps, in this order:

1. **Middleware** — no schema touched, detachable.
2. **Migration 041** — columns and triggers, no backfill.
3. **Rewiring** of the three consumers.

### Acceptance criteria

| What is proven | How |
|---|---|
| The actor arrives | `select actor, count(*) from access_log group by 1` after one night → `dream-codex-*` present |
| The loop stops | `1d1037e8` absent from `promote_prepare`'s output |
| Content no longer moves for nothing | after a night of REORG, `content_updated_at` unchanged on rows where only tags moved |
| The gate lives again | SKIP rate over 2 weeks, against the baseline **2/50** |

### Guardrails

- **No dream killswitch or environment variable is modified.** The
  pipeline keeps its exact configuration, so as not to confuse the effect of this
  change with something else.
- 041 does no backfill: its downgrade is a plain loss of
  columns, with no fail-closed arbitration to design.

## Assumed limits

- **`access_log` is purged after aggregation.** If the human/system
  classification rule changes later, `access_count_human` cannot be
  recomputed. That's the price of denormalization, accepted knowingly;
  the durable log is the subject of a deferred ticket.
- **The `X-Brain-Agent` header is client-declared.** Provenance is a
  hygiene signal, not a security boundary.
- **No backfill**: existing entities start at
  `access_count_human = 0` and `content_updated_at = NULL`.

## Out of scope

Workstreams from the same batch, to be specified separately:

- **B** — review surface for the 86 SYNTH insights: a three-state verdict
  table (keep / reject / promote), a dedicated tool, a `### To review` section
  in the briefing.
- **C** — PROMOTE's gate: the review verdict replaces ADR #4's original
  filter. The anti-echo-drift guardrail isn't lifted but replaced with a
  version that measures what it was aiming for — human endorsement rather
  than origin.
- **D** — SYNTH project routing: remove the hardcoded `project_key="brain-v42"` in
  `phase_synth.md`. All 86 insights sit under `brain-v42` even though
  several concern other projects; the `87389e6d` commit's guard already
  rejects unknown keys. Prerequisite for per-project review sessions.

Deferred ticket to open: **durable access log** (`access_log` retention
with actor, counters derived on demand), to measure actual corpus
usage — in particular "has a human ever read these insights?".
