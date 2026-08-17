---
title: "COR1 — Decay fed by every usage signal"
status: completed
completed_at: "2026-07-19"
deployed_at: "2026-07-19"
summary: "Route successful plan reads, generic gets, snippet uses, and runbook reads/executions through the canonical access log so real use refreshes the correct entity."
tags:
  - sol-ultra
  - cor1
  - decay
  - tdd
---

# COR1 — Decay fed by every usage signal

## Goal

Close the Sol Ultra COR1 gap without changing public MCP schemas. A successful, authorized
read or execution must produce one bounded usage signal for the canonical parent entity, and
the decay flusher must apply that signal instead of silently discarding it.

## Starting evidence

- Plan search results expose `parent_id`, but `BrainService` currently logs the chunk UUID.
- `DecayFlusher` has no `indexed_plans` target, so plan events are consumed without updating
  an entity.
- The decay calculator has no explicit plan profile.
- `brain_get`, `brain_use_snippet`, and runbook detail/execution paths do not feed the access
  logger.
- Existing unit tests cover the components separately but not the complete usage-to-freshness
  path.

## Acceptance criteria

1. Plan search and knowledge-summary hits log the parent plan UUID, never the chunk UUID, and
   duplicate chunks from one parent produce one signal per response.
2. `indexed_plans` participates in decay flushing and uses an explicit durable-document decay
   profile equal to the current runbook profile: age half-life 365 days, access half-life 180
   days, weights `0.2/0.3/0.3/0.2`, and frequency baseline 5. A recently used old plan can
   return from stale to fresh.
3. Successful authorized operations log only after their durable work succeeds:
   `brain_get` as `get_by_id`, `brain_use_snippet` as `use`, runbook detail as `get_by_id`, and
   runbook execution as `execute`.
4. Invalid, missing, or failed operations emit no usage signal. Generic scoped `brain_get`
   logs only after its point-of-use ownership check succeeds. The specialized snippet and
   runbook tools remain admin/STDIO-only under the current SEC1b policy; COR1 does not widen
   their Dream capability surface.
5. Logger injection is optional and internal; MCP names and input schemas stay unchanged.
   Logging stays best-effort and non-blocking: an absent logger, full queue, or consumer
   failure never changes an MCP response.
6. Decay status/refresh uses a dedicated six-type registry that includes `indexed_plans`.
   Consolidation uses a separate five-type registry and must never accept plans.
7. A dedicated PostgreSQL integration test, when an isolated test DSN is available, proves
   search/tool → queue → access log → flusher → parent entity fresh. It skips safely otherwise.
8. Access logger calls retain the UUID contract; plan parent identifiers are normalized to
   `UUID` before enqueue rather than broadening the logger API to strings.

## TDD sequence

1. Add non-skippable failing tests for both `search` and `what_do_i_know_about`: two chunks
   for one plan plus one chunk for another must emit exactly two parent UUID signals and no
   chunk UUID. `what_do_i_know_about` must construct the missing parent relationship too.
2. Add non-skippable calculator/flusher tests for the explicit plan profile, the
   `plan -> indexed_plans` mapping, parent-row updates rather than chunk-row updates, and a
   frozen or threshold-distant clock.
3. Add non-skippable tool matrices for generic `brain_get` (knowledge and plan, admin and
   scoped), snippet use, runbook detail by ID, and runbook execution. Invalid/ambiguous UUID,
   missing/foreign rows, and failed writes must emit zero signals. Composition tests assert
   one optional logger instance and unchanged MCP schemas.
4. Implement the smallest parent-target, decay-registry, and logger-plumbing changes.
5. Add the isolated PostgreSQL end-to-end proof using real `BrainService`, `AccessLogger`,
   `PgAccessLogRepo`, and `DecayFlusher` around a stale parent with duplicate chunks. After
   queue and decay flushes, assert exactly one access, advanced `last_accessed_at`, fresh
   parent state, no chunk update, and no residual access-log row. Search/embedding/graph
   dependencies may be deterministic fakes; PostgreSQL is real.
6. Run targeted, unit, lint, format, and type gates.

## Boundaries

No migration, queue durability redesign, deployment, live configuration change, or plan
consolidation support belongs to COR1.

## Delivery gate

An unavailable isolated PostgreSQL DSN leaves COR1 `active` with code complete and runtime
proof pending; it cannot be marked `done` from mocks alone. Completion additionally requires
documented rollback/recovery behavior, secret-safe outputs, `gitnexus_detect_changes()` before
commit, a final whole-diff `SHIP` review, and a Brain update carrying the real evidence.
The gate was satisfied on 2026-07-19; production rollout evidence is recorded below.

## Delivery evidence

- Local commit `0bf272a` on `codex/sol-ultra-cor123`.
- Three real PostgreSQL E2Es prove canonical-parent refresh, rejection of cross-project
  parent/child mismatches, and archived-parent filtering before top-K selection.
- The required reads and executions feed the access log without widening MCP schemas.
- Final review approved; Ruff, format, mypy, and the combined COR1/COR2/COR3 suite pass.
- GitLab MR `!66` merged the work into `main` at `75c6b05`; MR pipeline `4145` and main
  pipeline `4146`, including the registry build, both passed.
- Production `brain-mcp-http` and `brain-metrics` were restarted on the merged checkout.
  A live MCP → PostgreSQL proof recorded one parent-plan usage signal, refreshed the stale
  parent, left both chunk counters unchanged, and removed its fixture afterward.
