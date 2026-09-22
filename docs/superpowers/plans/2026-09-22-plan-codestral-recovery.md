# Plan index and Codestral recovery implementation plan

> **For agentic workers:** Use superpowers:subagent-driven-development for the bounded corrections below. The coordinator owns integration and operational execution.

**Goal:** Recover plan search after an incomplete embedding provider migration, preserving all stored knowledge and preventing a false successful verification.

**Architecture:** Retain the work in PRs172–174. Harden its verification and repair boundaries, then execute a measured recovery. Parent and chunk identity, project ownership and vector consistency are one operational unit.

**Tech Stack:** Python3.12, asyncpg, SQLAlchemy, pgvector, pytest.

## Global Constraints

- Preserve stored last copies of plans and their chunks. Ownership and vector
  repair do not delete those rows. A normal reindex may replace derived chunks
  from a current source file only after a complete before snapshot; verify that
  phase separately. No automatic duplicate deletion.
- Never alter unrelated project context fields, Brain session lifecycle, or the paused B/C/D worktrees.
- Use failure-first tests for behavioral changes and the existing embedding factory/composers.
- GitNexus upstream impact before symbol edits; UNKNOWN branch symbols are degraded evidence and require native reference inspection.
- Work only in the recovery worktree. The coordinator owns Git staging/commits while the integration merge is pending; workers run targeted tests, not the full suite.
- Secrets never enter output or versioned artifacts. Recovery data is private, exclusive-create and persisted before mutations.
- Periodic indexing remains disabled until recovery and coverage verification are complete.

### Task 1: Make embedding drift verification a gate for each type

**Files:** `src/brain_v42/services/embedding_drift.py`, `scripts/check_embedding_model_drift.py`, their unit tests, `tests/unit/services/test_embedding_text.py`, `docs/runbooks/2026-09-21-embedding-provider-switch.md`.

**Interfaces:** Keep `classify_drift`, `final_exit_code` and the canonical embedding composers. Add only `--exclude-gitlab-events` (default false) for the explicitly retired, partly unreproducible table. JSON and human reports must name this exclusion. Default continues attempting every table and cannot silently pass unmeasurable data.

- [x] Reproduce six matching types plus drifting plans/chunks; assert DRIFT and CLI exit1 even when the global median exceeds0.95.
  ```python
  samples = [SampleComparison(kind, str(i), similarity)
             for kind, similarity in [("learning", .999), ("decision", .999),
                                      ("snippet", .999), ("runbook", .999),
                                      ("adr", .999), ("feature", .999),
                                      ("plan", .01), ("plan_chunk", .01)]
             for i in range(8)]
  assert classify_drift(samples).verdict is DriftVerdict.DRIFT
  ```
- [x] Choose MATCH only if every sampled type's median meets the threshold. Preserve the empty/unmeasurable and collection-error contracts, and toleration of minority stale rows within one type.
- [x] Test truncated GitLab text: default exit2; explicit exclusion allows MATCH for the eight reproducible types and reports the exclusion. Provider/DB failure for an included type remains nonzero.
- [x] Exercise real chunker → persisted plan/chunk columns → reproducible composer for frontmatter/H1, noH2, fenced headings, long preambles/chunks. Derive expected embedding text independently; no live provider calls.
- [x] Correct runbook cutover gates and contradictory six/nine-table claims, making the GitLab exception explicit. Run focused drift/CLI/composer tests and lint. Record RED/GREEN and report for review.

### Task 2: Make inventory repair trustworthy under concurrency and ambiguous ownership

**Files:** `scripts/plan_index_inventory.py`, `src/brain_v42/maintenance/plan_index_inventory.py`, their tests; narrowly scoped config/refresher tests; inventory runbook.

- [x] Reject cross-project claims on the same canonical file, and reject incomplete/unresolvable scan inventories for apply instead of archiving from missing evidence.
- [x] Match the runtime's file eligibility rules (no symlink file, no escape from the canonical scan root). Verification compares actual path/project coverage, not only equal counts.
- [x] Make apply validate observed row state under locks in the transaction before any mutation. Refuse concurrent content, owner, path, freshness or chunk changes. Save every field changed by repair and affected dependent state in recovery before mutation; preserve recovery exclusivity. Snapshot includes freshness_source, updated_at, content_hash/indexed_at concurrency evidence, chunk IDs/project keys and complete affected feature edges. Perform locked revalidation before saving/applying, with a bounded lock_timeout; fail closed on missing rows or changed before values. No retry that silently refreshes the approved before-state.
- [x] Cross-project feature links cannot silently survive reattribution. Default apply refuses them. An explicit `--detach-cross-project-links` detaches only links for the touched plan IDs whose feature project differs from the final parent project; capture full edge fields first, never auto-create a replacement feature, never delete a parent, chunk or feature. Pre-existing foreign links outside the selected mutations remain out of scope. Require plan writers quiescent during operational apply because feature linking uses a later separate transaction.
- [x] Test invalid refresh intervals zero and negative, then enforce a strictly positive interval.
- [x] Run behavior tests and a disposable-PostgreSQL transaction test for rollback/conflict; review the correction before operational use.

### Task 3: Refresh plan vectors without a filesystem scan or knowledge deletion

**Files:** create `scripts/refresh_plan_embeddings.py`, focused unit and disposable-PostgreSQL tests; add a small explicit maintenance option in `services/plan_indexer.py` and its tests; add `docs/runbooks/2026-09-22-plan-codestral-recovery.md`.

**Interfaces:** The new CLI is read-only by default; `--apply --recovery-file ABSOLUTE_PATH --expected-plans N --expected-chunks N --expected-database NAME --expected-model MODEL` explicitly selects the write operation. Use Settings and build_embedding_service, and reproducible_embedding_text for both types. No raw endpoint client, no source-tree scanning in this CLI. Batch size1–100, default2 (Codestral: two inputs that each fit its8k window remain within the16k request budget used by the official cookbook). It refreshes all selected plan parents AND their chunks as one cohort, including archived plans (never silently skip the last stored copy). An optional project filter scopes parents and their chunks together.

- [x] RED: a real parent/chunk vector refresh changes both embeddings but preserves parent/chunk IDs, all text, project keys, freshness/indexing timestamps and feature links. Parent `updated_at` is a maintenance timestamp and advances through the existing database trigger; do not disable that trigger or claim it stays unchanged. Read-only default makes no provider call or write. Counts must equal the expected cohort, not a hardcoded production measurement.
- [x] RED: malformed cardinality, wrong dimensions, nonfinite/zero vectors, embedding failure, changed database/model identity and concurrent source/cohort changes refuse without partial writes. A fault between parent and chunk updates rolls back both in disposable PostgreSQL. Include a concurrent vector-only update in the CAS tests, and test recovery collision refusal/mode0600 under permissive umask.
- [x] Read a consistent snapshot (selected IDs, parent relationships, source fields and stored vectors). Validate limits and compose inputs. Embed outside the write transaction, then save an absolute-path private recovery JSON (O_CREAT|O_EXCL, mode0600, reject symlinks/nonregular paths and symlinked parents, fsync file and parent directory) containing original vectors and source/cohort identity before any mutation. No secrets or full plan bodies in logs/errors.
- [x] Under a bounded write transaction, lock both vector tables against concurrent writes, re-read and compare the selected snapshot (including exact parent/chunk ID sets, project agreement, every source field and each original embedding including NULL), then update ONLY `embedding` fields. No DELETE, INSERT, ClusterGuard, stale marking or updated_at writes. Database changes cancel the run; never refresh its before-state implicitly. Return exact parent/chunk updated counts and safe model/config information.
- [x] CLI closes its provider and database on every path, returns nonzero on refusal/error, and never prints a DSN or provider exception body. Document operator writer quiescence, recovery fields, and that vector refresh does not pretend the source file was freshly indexed.
- [x] Add keyword-only `link_features: bool = True` to PlanIndexer; when false, it indexes valid files normally but never resolves/creates/merges or links roadmap features. Permit `cluster_guard=None` only with this explicit false value; default behavior remains unchanged. Tests exercise a new file and a changed file with suppression and verify stored index results plus zero roadmap side effects. This option supports filling missing plans during recovery without generating pseudo-features.
- [x] Run focused tests, real PostgreSQL atomicity/conflict tests and static checks for owned files. Parent owns full-suite gates and commit after review.

### Task 4: Prepare and execute the measured recovery

The coordinator owns this task. Reuse existing boundaries where safe; do not turn an incident into a new maintenance framework.

- [x] Resolve each configured scan root against actual repositories; retain evidence and reject guesses about missing/ambiguous paths. Simulate the corrected configuration and inventory before applying it.
- [x] Prepare a private recovery snapshot and bounded conditional SQL for scan paths and index repair. Preserve project context outside `plan_scan_paths`, preserve feature relationships unless explicitly repaired, and keep parent/chunk project keys consistent.
- [ ] Prepare a plan/chunk vector refresh using the common embedding factory and composers, avoiding `dedupe_plans`. Preserve IDs, archived data and links. Re-encode the complete selected cohort including fresh plans; an unchanged file hash is not evidence of the current model.
- [ ] Verify snapshot/preconditions, per-project canonical coverage, no parent/chunk ownership divergence, complete vector dimensions and drift independently for plan and plan_chunk. Separate sampling evidence from full-cohort write accounting.
- [x] Run full unit suite and required static gates on the stable combined tree; independent final review. Commit the completed correction.
- [x] Prepare concrete integration/release/preflight/rollback steps. Publication and rollout follow the user's authorization and repository PR gates; do not claim live recovery from a local commit.
- [ ] Record measured before/after evidence and update the relevant Brain ticket. Keep B/C/D paused until this incident is resolved.
