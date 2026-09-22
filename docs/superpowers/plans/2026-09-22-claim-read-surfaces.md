# Claim history, validity and read surfaces (lot B4)

> For agentic workers: use superpowers:subagent-driven-development to execute bounded tasks, with independent review and parent-owned final verification.

**Goal:** Readers can discover a knowledge entry's claims, inspect their append-only history and distinguish current evidence from stale or failed observations. Existing search selection and ranking stay unchanged.

**Prerequisite:** B3 verification repository/coordinator and MCP tool complete. Measured-provenance writers remain a separate required part of lot B, not a reason to omit these reads.

**Architecture:** A repository reads immutable claim and verdict records; a leaf read model computes validity at a supplied UTC instant; a read service batches entry lookups; MCP adds bounded read tools and optional rendering suffixes. No dependency from services/repositories/models to facts. No schema migration, source probe or verdict write on a read.

## Contracts and constraints

- Approved spec: docs/superpowers/specs/2026-09-19-measured-facts-and-claims-design.md sections 6.4–6.6. Existing SQL view knowledge_claim_current has no clock and exposes recorded_at, not emitted_at. Join its verdict seq pointers back to the verdict table, or use equivalent indexed latest-row queries, to recover the observation timestamps.
- Canonical index belongs only to the canonical repository. Use upstream GitNexus impact before editing existing symbols and detect_changes with the explicit worktree before commits. New branch symbols need an announced native-inspection fallback. Preserve Claude's plan-indexing work and the separate usage commit.
- One read response uses one injected UTC now. Validity expires when now >= conclusive.emitted_at + validity_seconds. Do not use recorded_at to age cached evidence. An allowed small future clock skew does not create negative rendered ages.
- Latest attempt and last conclusive are separate. State is unverified when no verdict exists, unreadable when there is no conclusive result and the latest attempt failed, holds/falsified for fresh conclusive evidence, and stale for expired conclusive evidence. Keep the previous conclusive kind/time and any newer unreadable reason visible.
- Active entry suffixes exclude retired occurrences. History remains readable after retirement; show retirement explicitly. Every lookup enforces the caller's trusted project scope before revealing an occurrence or its history. Unscoped ordinary clients retain existing Brain access semantics. No old Dream phase gains a new tool.
- Claims readers are SELECT-only. No cache refresh, fact measurement, access-log entry for a claim, verdict insert, lifecycle mutation, ranking adjustment or archive.
- No active claims means byte-identical existing rendering. A suffix lookup failure should preserve the knowledge result while visibly saying claim state is unavailable; do not silently equate unavailable with absent. Propagate cancellation. Log no DB exception text or evidence payload.
- Strict TDD with original RED output for every new module's behavior, then GREEN. PostgreSQL tests with committed append-only data own a disposable database module-locally as tests/integration/db/test_claim_write_path.py does. Never migrate/truncate shared brain_test.

### Task 1: Batched repository, read model and service

**Create:** repositories/pg_claim_reads.py, models/claim_read.py, services/claim_read_service.py; focused unit tests and tests/integration/db/test_claim_reads.py.

- [ ] Specify immutable read records carrying occurrence/entry identifiers, project/type, declared provenance, statement, expectation, fact version/target, validity and retirement, plus latest and conclusive verdict metadata. Reuse B3 verdict-row conversion where helpful without creating import cycles or a second wire schema.
- [ ] RED table tests for state calculation, exact expiry boundary, observation-vs-insertion age, seq-based order despite reversed clocks, later unreadable with a fresh or stale conclusive result, no verdict, retired history and no mutation of source records.
- [ ] Implement a bounded batch lookup by authoritative brain_entities.source_uuid plus entity type, with an optional trusted project constraint, using one SQL query for the result batch. Query only requested entry IDs; return only active occurrences. Avoid a query per entry or per claim.
- [ ] Add occurrence listing with project/optional entry filters, include_retired=False, limit 1–100 and explicit after_seq cursor (nonnegative signed bigint); order by occurrence seq ascending and return next_after_seq only when another page exists. Every page independently applies all filters and scope.
- [ ] Add one-claim history by claim UUID, after_seq and limit, ordered by verdict seq ascending, with claim metadata and state. Missing and out-of-scope occurrences share the same safe refusal. Empty history for an existing claim is a valid result.
- [ ] Read service computes structured as_of/valid_until and exposes batch summaries without facts imports. Define only the error codes necessary for these tools; preserve masking of unexpected faults.
- [ ] Real-PG RED/GREEN: two projects, multiple entries, distinct claims and retirement; latest unreadable vs conclusive joins; stored emitted_at older than recorded_at; list/history page boundaries; authoritative scope refusal including empty history; reads leave ledger counts unchanged.
- [ ] Required parent unit/static/layering gates, independent review, detect_changes, local commit.

### Task 2: MCP tools and compact knowledge suffixes

**Modify:** mcp/tools/claim_tools.py, mcp/server.py, mcp/business_errors.py only as necessary; mcp/tools/brain_tools.py, crud_tools.py, session_tools.py, formatters.py. Add a small dedicated claim renderer if that keeps existing formatters readable. Add focused MCP/composition/rendering tests and documentation.

- [ ] RED full and compact FastMCP tests for brain_claim_list(project_key, entity_id=None, include_retired=False, after_seq=0, limit=50) and brain_claim_history(claim_id, after_seq=0, limit=50). Full UUIDs only, strict bounded integer arguments, read-only annotations, facts tag, safe errors and existing Dream-phase denial. Scope is server-owned where present; no public override can escape it.
- [ ] Wire one ClaimReadService at the composition root, inject it into claim tools and knowledge readers. Optional dependency defaults preserve standalone formatter/registration test seams; real composition always supplies it.
- [ ] French bounded suffixes distinguish fresh holds, fresh FALSIFIÉ, stale former outcomes, unverified declarations and unreadable latest attempts. If a newer attempt is unreadable after a conclusive verdict, retain both facts. Show a bounded fact/expected/observed detail for falsification; full evidence is available through history. Escape control characters and cap text lengths without altering stored evidence.
- [ ] brain_get appends the suffix after existing non-plan entity resolution and project authorization. Preserve the plan branch byte-for-byte and normal access logging.
- [ ] brain_search adds suffixes for both flat and group_by_type modes, full and compact text, with one batch fetch per result set. Pass a suffix mapping to format_search_results / format_knowledge_by_type; never mutate SearchResult.score, item, score_kind, ordering or filtering.
- [ ] Session start/resume share the existing briefing loader. Fetch summaries for the displayed recap decisions/learnings once and pass the mapping to _section_recap via _format_session_briefing. No extra Brain session lifecycle call. Preserve the no-claims briefing fixture.
- [ ] Test no N+1 behavior, no probe/write calls, unchanged ranked and grouped selection, unchanged no-claim output, scoped get/list/history and visible read failure without losing the knowledge response.
- [ ] Update docs/MCP_TOOLS.md and docs/ARCHITECTURE.md with a declaration → verification → history example, exact freshness semantics and the unchanged ranking behavior.
- [ ] Run focused integration/transport tests, all required gates, final independent material-change audit and detect_changes before the local commit. No push, merge, deployment or live probe.

## Required gates

Use the already validated isolated test environment: worktree .env contains only a dummy legacy POSTGRES_URL, umask 022, no exported production aliases. Full unit tests need the authorized unsandboxed invocation because sandboxed aiohttp binding and SQLite thread wakeups fail on the unchanged baseline.

```
umask 022
env -u BRAIN_POSTGRES_URL -u POSTGRES_URL -u BRAIN_V42_TEST_DB_URL .venv/bin/pytest -q tests/unit
.venv/bin/ruff check src/ tests/
.venv/bin/ruff format --check src/ tests/
.venv/bin/mypy src/
.venv/bin/python scripts/check_module_layering.py --package src/brain_v42
```

Real PostgreSQL tests run on a disposable database (`tests/integration/disposable_db.py`). Skipped DB tests are not passing integration evidence. Record exact summaries, review findings and local commit IDs in the durable program handoff. This plan alone cannot complete the broader Usage + B + C + D goal.
