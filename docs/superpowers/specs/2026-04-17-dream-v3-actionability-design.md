# Dream v3 — Spec A: Autonomous Actionability (revised)

**Date:** 2026-04-17
**Scope:** brain-v42 only (v1)
**Status:** Design revised post-critique, ready for writing-plans

## Revision history

- **v1 (commit `feeff17`)** — initial design, reviewed by 3 judges.
- **v2 (commit `f01c477`)** — addresses 5 must-fix + 5 should-fix findings from round-1 critique: auto-accept policy, dedicated `dream_promotions` audit table, atomic tool semantics, scope narrowing to brain-v42-only, post-phase validator, pre-computed pool, dry-run mode, advisory lock, calibration step, observability metric.
- **v3 (this document)** — absorbs round-2 critique: fixes the dedup type bug (`"adr"` not `"decision"` in `brain_search`), pins agent to `candidates[0]` strictly, changes `source_learning_id` FK to nullable + `ON DELETE SET NULL` (preserve audit on learning deletion), adds CHECK constraint on `target_type ↔ FK-nullness` coherence, adds partial unique index for DB-level race protection, adds tombstone filter so operator deletion of a bad ADR re-opens the source for reconsideration, names the repo-level method `create_with_promotion()`, pins `flock` path, documents the scope-narrowing trade-off explicitly, and names `promote_validate.py` as the `dream_runs.status='partial'` writer.

---

## 1. Intent

Today the Dream cycle produces insights tagged `dream:agent` that sit in `learnings` forever, accumulating without graduating into durable knowledge (ADR / Runbook). Spec A closes that loop **autonomously**: a new PROMOTE phase, run by Opus inside the nightly dream cycle, picks one mature insight per run and graduates it directly into an **accepted** ADR or a Runbook.

**Graduation means fully accepted.** The phase calls `brain_propose_adr` and **auto-accepts** (via an extended kwarg — see §4.3); runbooks are created in their normal state. An ADR left in `proposed` would just be another inbox; the user's intent is durable knowledge, so we commit to full graduation.

The user explicitly accepts the risk of bad auto-promotions. The safety net is: quality gates (maturity + dedup), a tight cap (1/run), reversibility via supersede/deprecate, a kill-switch env var, AND — added in v2 — an observable audit table, a DRY_RUN rehearsal mode, and a post-phase referential-integrity validator.

## 2. Scope

**In scope:**
- New PROMOTE phase between SYNTH and REORG in the dream pipeline.
- Insight sourcing: **`project_key=brain-v42` only for v1**.

  *User-intent delta (explicit)*: during brainstorm the user asked for `red + brain-v42 + auto-discord` scope. The critique round pushed back on breadth-first rollout; the final narrowing is a deliberate trade — narrow scope for v1 production observation, wider scope deferred to v2-of-spec-A after 4 weeks. Factually aligned with reality: all current `dream:agent` insights are written with `project_key=brain-v42` by SYNTH (`scripts/dream/phase_synth.md:23`), so the broader scope would be aspirational in v1 anyway. v2 will land when (a) 4 weeks of observation data exist, AND (b) SYNTH is updated to write insights under other project_keys.
- Direct materialization via **extended** existing tools: `brain_propose_adr(source_learning_id?, auto_accept?)` and `brain_create_runbook(source_learning_id?)`. No new MCP tools.
- New operational table `dream_promotions` (migration 016) — row-per-attempt audit.
- Post-phase Python validator (called by `dream.sh` after parsing the report).
- DRY_RUN support (matches every existing phase prompt's `{{DRY_RUN}}` convention).
- Advisory lock on `dream.sh` orchestrator startup.
- Prometheus counter + alert on promotion spikes.
- Unit + integration + DRY_RUN rehearsal tests.

**Out of scope (killed):**
- Any red-monitor dashboard integration, SolidJS UI, user accept/reject workflow, preview modals, reject reasons.
- `dream_proposals` staging table from the WIP brainstorm.
- Cross-project sourcing in v1 (deferred to v2).
- Spec B (meta-synthesis) and Spec C (cross-domain linking) — separate future specs.

## 3. Architecture

### 3.1 Pipeline position

```
SCAN → CLEAN → CONNECT → SYNTH → PROMOTE → REORG
```

PROMOTE runs after SYNTH (new insights written) and before REORG (structural reorganization). Freshly synthesized insights are too young to qualify (age ≥7d rule) so SYNTH→PROMOTE has no direct dependency beyond ordering.

### 3.2 Phase runtime

- Model: **Opus** (matches SYNTH / REORG).
- max_turns: **50**.
- Timeout: **10 min**.
- Retry on hard-fail (exit=1): **1×**. Timeouts do NOT retry (matches SCAN/CONNECT per commit `a2bc792`).
- **Advisory lock**: `dream.sh` acquires `flock -n "${XDG_RUNTIME_DIR:-/tmp}/brain-v42-dream.lock"` at orchestrator startup. The path is user-scoped so `/var/run/` root-only-write issues are avoided; `XDG_RUNTIME_DIR` exists on systemd user sessions, `/tmp` is the portable fallback. If lock held, exit 0 with log line "dream cycle already running, skipping". Protects against cron overlap + manual dev runs colliding.
- **Kill-switch**: env var `BRAIN_DREAM_PROMOTE_ENABLED` (default `true` post-rollout; shipped `false` — see §8). When `false`, `dream.sh` skips PROMOTE with a single log line and emits a minimal report (`reason: killswitch_disabled`). Logged in every run's top banner whether on or off.
- **DRY_RUN**: `dream.sh` accepts `--dry-run` and injects `{{DRY_RUN}}=true` into the PROMOTE prompt. In DRY_RUN, the agent simulates the full flow (SQL filter, ranking, classification, dedup check, draft generation) and emits a full report but does **not** call `brain_propose_adr` / `brain_create_runbook` / any mutation. `dream_promotions` still gets a row with `target_type='dry_run'` for audit.

### 3.3 Decision flow

Split between deterministic Python and LLM judgment. Python owns the candidate pool; the LLM picks one and drafts.

**Step 1 — Candidate pool (Python, before LLM invocation):**

```sql
-- Executed by scripts/dream/promote_prepare.py
-- Top N=10 candidates injected into the prompt as JSON
SELECT id, topic, content, tags, metadata, confidence, access_count, created_at
FROM learnings
WHERE age(NOW(), created_at) >= interval '7 days'
  AND access_count >= 3
  AND NOT (confidence = 'low' AND access_count < 5)
  AND project_key = 'brain-v42'
  AND NOT EXISTS (
    SELECT 1 FROM dream_promotions p
    WHERE p.source_learning_id = learnings.id
      AND (
        -- still-alive promotion (target not hard-deleted)
        (p.target_type = 'adr' AND p.target_adr_id IS NOT NULL)
        OR (p.target_type = 'runbook' AND p.target_runbook_id IS NOT NULL)
        -- skipped-dedup is a soft block; kept to avoid re-evaluating
        -- the same noise every night. Operators can lift by deleting
        -- the skipped_dedup row (see §9 reversibility drill).
        OR p.target_type = 'skipped_dedup'
      )
  )
ORDER BY access_count DESC, created_at DESC
LIMIT 10;
```

If the pool is empty: `dream.sh` skips the LLM invocation entirely and writes a report `{ reason: "no_candidates" }` directly. No Opus budget spent on an empty query.

**Step 2 — LLM evaluates candidates[0] and drafts:**

Strict rule: the agent MUST evaluate `candidates[0]` (the first item in the pre-computed pool, i.e. the highest-ranked). It may NOT pick any other candidate. The top-10 injection exists only to give the agent dedup-awareness context (seeing what other candidates are in-flight helps it reason about uniqueness); the drafting work is restricted to index 0. The validator (step 3) enforces this — `source_learning_id` must equal `candidates[0].id`.

```
For candidates[0]:
  a. Agent reads full insight content (candidate fields already in prompt).
  b. Agent classifies target_type:
       ADR if the insight documents a choice between alternatives or a
         durable architectural position.
       Runbook if the insight describes a reproducible procedure with steps.
       If the agent cannot confidently classify, it emits target_type="none"
         and reason="classification_uncertain" and stops.
  c. Agent runs dedup: brain_search(query=<candidate.topic>,
       types=["adr"] if target_type == "adr" else ["runbook"],
       min_score=0.80, limit=5).
     CRITICAL: the search type is "adr" (not "decision"). ADRs live in the
     adrs table; "decision" would search the decisions table (brain_log_decision
     rows) and miss all existing ADRs, silently disabling the dedup gate.
     If brain_search raises EmbeddingUnavailable: agent emits target_type="none"
       and reason="dedup_unavailable" and stops. DO NOT fail open.
     If best result has cosine >= 0.85: agent emits target_type="skipped_dedup"
       with cosine_observed, target_id of the duplicate, and stops.
  d. On OK: agent drafts and calls the extended tool:
     brain_propose_adr(title, context, decision, consequences, project_key,
                      alternatives_considered, tags,
                      source_learning_id=<insight_id>, auto_accept=True)
     or
     brain_create_runbook(title, description, project_key, trigger, steps, ...,
                         source_learning_id=<insight_id>).
     The tool handler wraps the INSERT + learnings metadata UPDATE +
     dream_promotions row in ONE transaction (see §4.3).
  e. Agent emits the report with the real target_id returned by the tool.

Hard rule: the agent evaluates exactly ONE candidate per run. Dedup-skip or
classification-uncertain does NOT trigger iteration — phase ends. If the top
candidate is dedup-noise, the next run will see a different top-candidate
(the dedup-skipped one is recorded in dream_promotions and excluded by the
NOT EXISTS filter).
```

Rationale for "strictly one candidate evaluated per run": prevents the agent from burning max_turns on a dedup-skip loop, keeps the report schema trivial (single target), keeps the cap unambiguous, and makes dry-run rehearsal deterministic.

**Step 3 — Post-phase validator (Python, after parsing the report):**

`dream.sh` runs a validator in `scripts/dream/promote_validate.py` that both enforces referential integrity and records the audit row for non-materializing outcomes:

```
Common checks (all target_types):
  - Assert report JSON parses cleanly against the schema.
  - Assert source_learning_id == candidates[0].id (strict: agent is pinned
    to the top-ranked candidate, not any of top-10). Rejects hallucinated
    insight_ids AND rejects agent picking a non-top candidate on a hunch.

Per target_type:
  adr:
    - Assert row exists in adrs WHERE id=target_id AND status='accepted'.
    - Assert exactly one matching dream_promotions row exists
      (written inside the tool-handler transaction).
  runbook:
    - Assert row exists in runbooks WHERE id=target_id.
    - Assert the dream_promotions row exists (as above).
  skipped_dedup, classification_uncertain, dedup_unavailable, dry_run, none:
    - INSERT a dream_promotions row capturing the outcome
      (these paths never hit the tool handlers, so the validator owns the
      audit insert). cosine_observed populated for skipped_dedup only.

On any integrity failure: `promote_validate.py` itself writes
dream_runs.status='partial' + dream_runs.error_message and exits
non-zero. No separate orchestrator glue needed — the validator module
is the named owner of the partial-status write. The validator does not
undo state: any already-committed adrs/runbooks row stands, flagged
for operator review via the partial status + error_message.
```

### 3.4 Cap rationale

Cap is **1 candidate evaluated per run**, which resolves to either 1 successful promotion OR 1 skipped_dedup OR 1 classification-uncertain OR 0 (empty pool). This is stricter than v1's "1 promotion but iterate on dedup skips" — the revision trades pool coverage for turn-budget safety, report simplicity, and test determinism.

**Acknowledged trade-off (dedup-starvation)**: if the top-ranked candidate is dedup-noise for N consecutive nights, throughput is 1/N promotions. The soft-block `skipped_dedup` row excludes the candidate from future pools, so the next night sees a different top candidate — starvation is bounded, not infinite. An operator can force-advance by deleting stale skip rows. If post-rollout observation shows the dedup-skip rate is >30% of evaluated candidates, we revisit by adding a bounded `max_dedup_checks=3` budget to step 2 (raised from strict cap=1 evaluation to "1 materialization, up to 3 dedup attempts").

If the pool deepens to >5 qualifying candidates consistently, that's a signal to raise maturity thresholds, not the cap.

## 4. Data model

### 4.1 New table: `dream_promotions`

Mirrors the `consolidation_log` precedent (migration 008). Migration `016_dream_promotions.py`:

```sql
CREATE TABLE dream_promotions (
    id                  BIGSERIAL PRIMARY KEY,
    dream_run_id        INTEGER REFERENCES dream_runs(id) ON DELETE SET NULL,
    -- Nullable + SET NULL: preserve audit history if a learning is hard-deleted
    -- (e.g. by CLEAN or by an operator). The ADR/Runbook and the audit trail
    -- survive the learning's deletion; the FK becomes NULL as a tombstone.
    source_learning_id  UUID REFERENCES learnings(id) ON DELETE SET NULL,
    target_type         VARCHAR(20) NOT NULL,
        -- 'adr' | 'runbook' | 'skipped_dedup' | 'dry_run'
        -- | 'classification_uncertain' | 'dedup_unavailable'
    target_adr_id       UUID REFERENCES adrs(id) ON DELETE SET NULL,
    target_runbook_id   UUID REFERENCES runbooks(id) ON DELETE SET NULL,
    cosine_observed     FLOAT,
    skipped_reason      VARCHAR(100),
    created_at          TIMESTAMPTZ DEFAULT NOW(),

    -- Enforce the legal target_type ↔ FK-nullness coherence contract.
    -- Without this CHECK, a hallucinated tool call could write
    -- target_type='adr' with both FKs NULL and PG would accept it.
    CONSTRAINT dream_promotions_target_shape CHECK (
        (target_type = 'adr'
            AND target_adr_id IS NOT NULL AND target_runbook_id IS NULL)
        OR (target_type = 'runbook'
            AND target_runbook_id IS NOT NULL AND target_adr_id IS NULL)
        OR (target_type IN ('skipped_dedup', 'dry_run',
                            'classification_uncertain', 'dedup_unavailable')
            AND target_adr_id IS NULL AND target_runbook_id IS NULL)
    )
);

CREATE INDEX idx_dream_promotions_source
    ON dream_promotions(source_learning_id);
CREATE INDEX idx_dream_promotions_created
    ON dream_promotions(created_at DESC);

-- DB-level idempotency guard against the SELECT-then-INSERT race under
-- READ COMMITTED. Only one materialized promotion per source_learning_id
-- can exist; concurrent attempts get a clean unique-violation error.
-- Skip-path rows are unconstrained (multiple skips on the same insight
-- across different nights are legitimate audit data).
CREATE UNIQUE INDEX idx_dream_promotions_source_materialized
    ON dream_promotions(source_learning_id)
    WHERE target_type IN ('adr', 'runbook')
      AND source_learning_id IS NOT NULL;
```

The FK targets `adrs(id)` — that is the canonical table for ADRs created by `brain_propose_adr` (`src/brain_v42/db/tables.py:264`). `decisions` is a separate table used by `brain_log_decision`; we do NOT promote into it.

### 4.2 No tag-based state

v1's `dream:promoted` and `dream:promote_skipped_dedup` tags are **dropped**. State lives exclusively in `dream_promotions`. The candidate-pool filter uses `NOT EXISTS` against that table (see §3.3 step 1). Rationale:
- REORG's tag-normalization logic cannot silently break idempotency.
- FK integrity: deleting an ADR via `ON DELETE SET NULL` cleanly tombstones the row instead of dangling a text UUID in `metadata`.
- Operational queries like "last 30 days of promotions" or "dedup-skip rate" become trivial SQL.
- `learnings.metadata` still records `target_entity_id` as a denormalized pointer, written in the same transaction (read-only from the learning's perspective).

### 4.3 Extended tool contracts

Two existing MCP tools gain optional kwargs. Backwards compatible — callers that don't pass the new kwargs behave exactly as before.

**`brain_propose_adr(..., source_learning_id: str | None = None, auto_accept: bool = False)`:**
When both kwargs are set, the handler delegates to a new **repo-level method** `PgADRRepo.create_with_promotion()` which owns the single SQL transaction:

```python
# New method signature, added in writing-plans:
async def create_with_promotion(
    self,
    data: ADRCreate,
    embedding: list[float] | None,
    source_learning_id: UUID,
    auto_accept: bool,
    dream_run_id: int | None,
) -> ADR:
    async with self._session_factory() as session:
        async with session.begin():
            adr = await self._insert_adr(session, data,
                                          status="accepted" if auto_accept else "proposed",
                                          decided_at=func.now() if auto_accept else None)
            await self._patch_learning_metadata(session, source_learning_id,
                                                 target_entity_id=adr.id)
            await self._insert_dream_promotion(session,
                                                dream_run_id=dream_run_id,
                                                source_learning_id=source_learning_id,
                                                target_type="adr",
                                                target_adr_id=adr.id)
            return adr
```

Existing `PgADRRepo.create()` is untouched; the new method exists alongside so non-Dream callers are not affected. Same pattern applies to `PgRunbookRepo.create_with_promotion()`.

**Architectural note — service-layer side effects**: `ADRService.create` currently fires graph upsert + auto-link AFTER the repo transaction commits (`src/brain_v42/services/adr_service.py:70-104`). The new `create_with_promotion` path preserves this ordering — graph side-effects remain **post-commit, best-effort**. A Neo4j outage leaves the ADR + learning.metadata + dream_promotions row committed in PG and the graph eventually-consistent via Neo4j's backfill mechanism (matches CLAUDE.md "graceful degradation if Neo4j down" contract).

**Kwarg validation (prevents foot-guns for non-Dream callers):** the tool handler rejects calls where `source_learning_id` is set but `auto_accept` is False (ADR) or where one of the Dream-specific kwargs is set without the other. Returns a clear error rather than silently producing a half-graduated row.

**Idempotency at the DB level:** the partial unique index in §4.1 turns any duplicate `source_learning_id` materialization into a clean unique-violation error from PG. The repo method catches this, rolls back, and raises a typed exception the orchestrator maps to `dream_runs.status='partial'`.

**`brain_create_runbook(..., source_learning_id: str | None = None)`:**
Same pattern via `PgRunbookRepo.create_with_promotion()`. Runbooks don't have a proposed/accepted state — just `INSERT runbook` + UPDATE + INSERT audit in one transaction.

**Skip paths (validator-owned):**
When the agent emits `target_type=skipped_dedup` / `classification_uncertain` / `dedup_unavailable` / `dry_run`, `promote_validate.py` writes the `dream_promotions` row (see §3.3 step 3). The agent does NOT call any MCP tool on these paths. Validator uses `INSERT ... ON CONFLICT DO NOTHING` keyed on `(source_learning_id, dream_run_id, target_type)` to tolerate a re-run of the validator on the same phase output without double-writing.

**Validator failure handling:** if the validator itself crashes before writing the skip row, the next cron cycle would re-evaluate the same top candidate (same pool ranking). That is an acceptable no-op reconsideration — the agent will hit dedup again and produce another skip row attempt. Infinite-loop scenarios are bounded by the fact that a validator crash loud enough to lose the skip write would also be loud enough to surface in monitoring (dream_runs.status='partial' with error_message).

### 4.4 Audit trail extension on `dream_runs`

Unchanged from v1 except in one respect: no JSONB column is added. The per-phase summary line for PROMOTE is emitted to stdout / structured logs only (parsed metrics already capture tokens + cost from the OTEL line). Rich audit data lives in `dream_promotions`, joinable by `dream_run_id`.

## 5. PROMOTE prompt design

### 5.1 Context injection (by `dream.sh`)

- Pre-computed candidate pool (top 10) as JSON.
- `{{DRY_RUN}}` boolean flag.
- Optional: last 10 dream_promotions rows (for the agent to see historical classification patterns). If the agent sees 3 recent ADRs on similar topics, it can recalibrate dedup without burning extra tool calls.

No longer injects `synth.log` / `connect.log` — those were not used in the decision flow in v1 and removing them shrinks the prompt surface.

### 5.2 Prompt skeleton

```
SYSTEM:
You are the PROMOTE phase of the brain-v42 dream cycle. Your single goal:
take ONE mature insight and graduate it into an ACCEPTED ADR or a Runbook.
No human validates your work. You accept the risk, but you MUST respect
the referential-integrity contract at the end.

DRY_RUN: {{DRY_RUN}}
  If true: simulate the full flow but DO NOT call brain_propose_adr or
  brain_create_runbook. Emit the draft in the report instead of a tool call.

SCOPE: project_key = brain-v42 only (v1).

CANDIDATE POOL (top 10, ranked by access_count DESC, created_at DESC):
  <pre-computed JSON array from scripts/dream/promote_prepare.py>

RECENT PROMOTION HISTORY (last 10 dream_promotions rows):
  <injected for calibration awareness>

TOOLS ALLOWED:
  brain_get (read full insight content if pool excerpt is truncated)
  brain_search (dedup check; REQUIRED before any materialization)
  brain_propose_adr (materialize ADR; pass source_learning_id + auto_accept=true)
  brain_create_runbook (materialize runbook; pass source_learning_id)

TOOLS NOT ALLOWED:
  brain_update (tag/metadata are written by the tool handlers, not you)
  brain_accept_adr (use auto_accept=true on brain_propose_adr)

STEPS (cap=1 candidate):
  <expanded from §3.3 step 2>

OUTPUT (exact format, parsed by dream.sh):
  === PROMOTE REPORT ===
  {
    "dry_run": <bool>,
    "candidate_id": "<uuid>",
    "candidate_topic": "...",
    "target_type": "adr" | "runbook" | "skipped_dedup" | "classification_uncertain" | "dedup_unavailable" | "none",
    "target_id": "<uuid or null>",
    "cosine_observed": <float or null>,
    "draft_title": "...",           // always populated; even on skip for audit
    "reason": "..."                 // human-readable explanation
  }
  === END ===
```

### 5.3 Classification guidance

Plain-English guidance in the prompt, no keyword rules:
- **ADR** when the insight documents a choice between alternatives, a durable position, or a trade-off analysis. Requires: alternatives considered, consequences.
- **Runbook** when the insight describes a reproducible procedure with concrete, sequential steps. Requires: trigger condition, at least 2 ordered steps.

If the candidate fits neither cleanly → `target_type=classification_uncertain`. This is **not a failure** — just a pass, and the next run picks a different top candidate.

## 6. Guardrails summary (revised)

| Guardrail | Enforcement layer |
|-----------|-------------------|
| Cap 1 candidate/run | Prompt + parser assertion |
| Maturity (age ≥7d, access ≥3, confidence-aware) | SQL WHERE clause |
| Scope: project_key=brain-v42 only | SQL WHERE clause |
| Dedup cosine ≥ 0.85 | Agent step 2c; calibration §8.3 |
| Dedup fails closed on embedding outage | Agent step 2c + validator |
| Atomic materialization | Tool handler transaction (§4.3) |
| No hallucinated target_id | Post-phase validator (§3.3 step 3) |
| No hallucinated source_learning_id | Validator matches against pre-computed pool |
| Kill-switch `BRAIN_DREAM_PROMOTE_ENABLED` | `dream.sh` pre-check, logged every run |
| Concurrent-run protection | `flock` advisory lock |
| Retry 1× hard-fail, no retry timeout | `dream.sh` wrapper (commit a2bc792) |
| Idempotency | `NOT EXISTS` against `dream_promotions` |
| DRY_RUN rehearsal mode | Prompt branch + `target_type='dry_run'` audit row |
| Observability | prometheus counter + alert (§8.4) |

## 7. Testing strategy

### 7.1 Unit tests

- `dream_parser.extract_promote_report()` — happy path, malformed JSON, missing markers, dry_run=true, each target_type value.
- `promote_prepare.build_candidate_sql()` — snapshot test of the SQL for all filter branches (including `NOT EXISTS` subquery shape).
- `promote_prepare.fetch_candidates()` — SQL fixture with 8 insights covering: too young, too few accesses, low-confidence with insufficient accesses, already-promoted in `dream_promotions`, already-skipped-dedup, wrong project_key, matching candidate. Assert exact 1 candidate returned with correct ranking.
- `promote_validate.validate_report()` — tests for: valid adr, valid runbook, hallucinated target_id, source_learning_id not in pool, missing `dream_promotions` row, skipped_dedup with missing cosine.

### 7.2 Integration tests (pytest, real PG, stubbed LLM)

- **Happy path ADR**: fixture pool of 3 insights → agent promotes top one as ADR. Assert: adrs row exists with status='accepted', learnings.metadata has target_entity_id, dream_promotions row exists, dream_runs.status='done'.
- **Happy path Runbook**: same but runbook target.
- **Dedup skip**: candidate has a ≥0.85 cosine match against a seeded ADR. Assert: no new adrs/runbooks row, `dream_promotions` row with target_type='skipped_dedup' and cosine_observed, dream_runs.status='done'.
- **Empty pool**: no matching insights. Assert: LLM not invoked, dream_runs.status='done', `reason: no_candidates`.
- **Idempotency**: run twice. Second run's pool excludes the first run's candidate (via NOT EXISTS).
- **Retry 1×**: LLM first exit=1, second succeeds. 2 invocations, final state = successful.
- **Kill-switch**: env=false → LLM not invoked, dream_runs.status='done', `reason: killswitch_disabled`.
- **DRY_RUN**: `--dry-run` flag → no adrs/runbooks row, `dream_promotions` row with target_type='dry_run', report `dry_run: true`.
- **Atomic-write crash simulation**: monkey-patch `brain_propose_adr` tool handler to raise AFTER the adrs INSERT but BEFORE committing. Assert: transaction rolls back, no adrs row, no `dream_promotions` row, agent sees error, phase reports failure.
- **Hallucinated target_id**: stub LLM returns a plausible but fake UUID. Validator catches it. Assert: dream_runs.status='partial', dream_runs.error_message set.
- **Hallucinated source_learning_id**: stub LLM returns a UUID not in the pool. Validator catches. Same assertions.
- **Embedding down during dedup**: `brain_search` raises EmbeddingUnavailable. Assert: no materialization, `dream_promotions` row with target_type='dedup_unavailable', reason included.
- **Advisory lock**: hold `/var/run/dream.lock` in a separate process, run `dream.sh`. Assert: exit 0, log line, no phase execution.

### 7.3 Rehearsal gate (DRY_RUN days)

Explicitly required before the first live flip (see §8 step 5). Not an automated test — an operator step. DRY_RUN for 3 nights on prod, operator manually reviews each draft for correctness and target_type appropriateness before enabling live writes.

## 8. Rollout

1. **Schema**: ship migration `016_dream_promotions.py`. Extend `brain_propose_adr` and `brain_create_runbook` tool contracts (backward-compatible kwargs).
2. **Wrapper code**: `scripts/dream/promote_prepare.py`, `scripts/dream/promote_validate.py`, PROMOTE prompt file, `dream.sh` changes (advisory lock, DRY_RUN flag, killswitch banner, validator invocation).
3. **Observability**: prometheus metric `brain_dream_promotions_total{project_key, target_type}` wired into the existing metrics sidecar. Alert rule: "more than 3 'adr' promotions in a 7-day window for project_key=brain-v42" pages operator.
4. **Calibration step (REQUIRED before first live run)**: compute pairwise cosine similarity for the current corpus of ADRs (192 decisions) + runbooks. Plot the distribution. Confirm 0.85 is at or above the 95th percentile of unrelated-pair cosines. If not, raise to whatever the 95th percentile is. Document the chosen threshold in the prompt as a reviewable constant.
5. **DRY_RUN rehearsal**: `BRAIN_DREAM_PROMOTE_ENABLED=true`, `--dry-run` flag in cron for 3 nights. Operator reviews every draft daily. Any classification error or bad draft → halt rollout, revise prompt, restart rehearsal.
6. **Live dev**: flip `--dry-run` off on dev for 7 nights. Operator reviews daily. `dream_promotions` + metrics visible.
7. **Live prod**: flip on prod. Weekly operator review of `dream_promotions` for 4 weeks.
8. **v2 evaluation (4 weeks in)**: based on metric data (pool depth, dedup-skip rate, classification-uncertain rate, supersede rate of auto-promoted ADRs), decide whether to widen scope to `project_group=red` and raise/lower thresholds.

## 9. Follow-ups and known gaps

- **Sub-partition scope gotcha** (brain learning `b360b341`, red-shrik) — preserved verbatim from v1 §9; v1 scope reconciliation removes the immediate concern but the gotcha still applies if/when v2 widens.
- **Spec B (meta-synthesis)** and **Spec C (cross-domain linking)** — separate future specs.
- **Reversibility drill**: after the first 5 real-world promotions, manually run `brain_supersede_decision` on one and `brain_deprecate_adr` + runbook deprecation on another. Confirm `dream_promotions.target_adr_id` `ON DELETE SET NULL` behaves as intended and that the source learning can be re-promoted if the target is hard-deleted (tombstone pattern).
- **Cosine threshold self-tuning** (v3 follow-up): log `cosine_observed` for every dedup-skip. After 3 months of data, consider auto-adjusting the threshold based on the observed distribution of true-duplicate vs false-positive cosines.
- **Promoted-ADR access telemetry**: future work — track whether auto-promoted ADRs get accessed / linked / superseded after creation. This is the actual correctness-check (per the false-green meta-fix in brain learning `24e26a97`). Not in v1 — v1 trusts that operators notice bad ADRs via weekly review. Expect to add in v2.
- **Promote-on-demand CLI**: an ops escape hatch (`dream.sh promote --once --candidate-id=X`) is not included in v1. If the operator wants to force-promote a specific insight out-of-cycle, they can run it manually once it's implemented. Defer unless actually needed.
