# Claim verification (lot B3) implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: use superpowers:subagent-driven-development to execute this plan in this session, or superpowers:executing-plans in a separate session. Steps use checkbox syntax for tracking.

**Goal:** A caller can request verification of an active claim and receive a durable, server-measured, append-only verdict; retries return exactly the same verdict without probing again.

**Architecture:** Extend the existing facts boundary with a pure comparator and a verification coordinator. Keep PostgreSQL persistence in a repository that has no dependency on `facts`. Register a dedicated MCP tool that accepts only a claim UUID and an idempotency key. Use the existing revision 055 tables, verified fact registry, provenance context and masked business-error boundary.

**Tech stack:** Python 3.12, FastMCP, SQLAlchemy async PostgreSQL, pytest; no schema migration.

## Global constraints

- Approved source contract: `docs/superpowers/specs/2026-09-19-measured-facts-and-claims-design.md`, sections 6.2–6.4. B1/B2 are already shipped. This plan delivers explicit verification; measured write provenance and B4 readers follow in the same authorized program.
- Isolated worktree: `.worktrees/codex-claims-verification`, branch `codex/claims-verification`, base `f57f7a84c2cf0ed9cffa61261725b3af6a04ff09`. Preserve the independent embedding-usage work and Claude's plan-indexing work.
- Canonical GitNexus was fully rebuilt at 2026-09-22T10:37:50.397Z, indexed commit equals this base. Use query/context, upstream impact before editing each existing symbol, and `detect_changes` with this explicit worktree before each commit. Do not index a worktree. Report high/critical risks before editing.
- English code, documentation and commits; French user communication. Strict TDD: record meaningful failing assertions before implementation.
- The module-layering graph is acyclic. `facts` already imports `services` (the Alembic probe); do not import `facts` from `services`, `repositories`, or `models`. The verification service therefore lives in `facts/verification.py`, composed by MCP. Repositories expose persistence records and accept primitive payloads, never a `Measurement`.
- A caller cannot supply a measurement, an outcome, an issuer identity, or an emitted timestamp to the public tool. The server produces measurements; the request actor is declared provenance, not proof of the measured target.
- No UPDATE/DELETE on verdicts, no claim lifecycle changes, no graph projection/outbox rows in B3, no ranking changes, no network probe added here.
- Do not touch production or shared test schemas. Tests that commit append-only rows must declare a module-local disposable database and engine as in `tests/integration/db/test_claim_write_path.py`. Use the supported environment resolver at fixture time, not at module import. No TRUNCATE or disabling triggers.
- Local commits only after required gates. No push, merge, deployment, Brain lifecycle/ticket mutation, or service restart.

## Resolved edge cases

1. **A cache observation already verified for this claim:** lock the claim row, perform normal idempotency replay first, then measure with `max_age=validity_seconds`. If `(claim_id, observation_id)` already exists, request exactly one fresh reading with `max_age=0`. If still duplicated (e.g. a shared in-flight read), raise the safe `observation_already_verified` error. Never return another request's verdict without binding the new key. No retry loop or alias-table migration.
2. **Lock ordering and retirement:** verification takes `FOR UPDATE` on the claim occurrence and holds it through lookup, bounded measurement and insert, in one transaction. Concurrent verification of the same claim serializes; concurrent retirement either finishes first and causes refusal or waits until the verdict commits. Different claims do not share this lock. Scope filtering occurs in the authoritative query before a replay or probe.
3. **Fingerprint bounds:** use SHA256 of deterministic canonical JSON of the exact request/outcome fields in the spec (no undocumented domain prefix). The separate envelope serializer has `MAX_ENVELOPE_BYTES = 16_384` and `MAX_ENVELOPE_DEPTH = 12`: room for the independently validated 4096-byte/depth-8 fact value, two wrapper levels and bounded metadata. It does not weaken the value canonicalizer or truncate evidence. Reject non-JSON values/floats, NUL/surrogates, oversized envelopes and invalid issuer/key strings through safe errors. Test exact boundaries and boundary + 1, and metadata-only differences in outcome hashes.
4. **Removed definitions:** if the current catalogue no longer has the fact, prepare server-side `Unreadable(error_code="definition_drift")` with a fresh observation UUID and the claim's immutable metadata, and store `unreadable/definition_changed`. The locked claim snapshot joins the immutable `knowledge_fact_definitions` row by `(fact_name, definition_version)` and carries its original `ttl_seconds`; never derive TTL from the independently chosen claim validity. A currently registered changed version is also unreadable, never falsified. Do not invent a successful measurement or silently compare against a new definition.
5. **Equality and null:** distinguish an absent path from a present null. `exists` is false for both; equality with a present null is meaningful. Preserve nullable scalar equality (`null|int` catalogue values): null versus an integer is unequal. Booleans are not integers. Incompatible non-null scalar kinds and non-scalar comparison operands are unreadable/type_mismatch; ordering is integer-only.
6. **Replay after retirement:** follow the approved ordering: retired claims are refused before idempotency replay. An active replay returns the original row, including unreadable evidence, without rechecking current catalogue availability or running a probe.

### Task 1: Pure comparison, fingerprinting and registry identity access

**Files:**
- Create `src/brain_v42/facts/compare.py` and `src/brain_v42/facts/verdict_fingerprints.py`.
- Add the minimal safe error type to `src/brain_v42/models/claim_verdict.py`.
- Extend `src/brain_v42/facts/registry.py` with `expected_identity(target: FactTarget) -> Identity | None`, returning the immutable independently configured identity without opening a source.
- Create `tests/unit/facts/test_claim_compare.py` and `test_verdict_fingerprints.py`; extend the existing registry tests.

**Interfaces:**
- `Comparison` is an immutable pair of `verdict` (`holds`, `falsified`, `unreadable`) and safe `reason`.
- `compare(expected_resolved: Mapping[str, object], measurement: Measurement) -> Comparison` is pure. An unreadable measurement yields `probe:<error_code>`; identity/version/name guards are the coordinator's responsibility.
- RFC 6901 traversal honors `~0` and `~1`, object keys and canonical nonnegative array indexes. Missing/invalid paths never raise through the comparison boundary. `exists` falsifies absent/null; other absent paths produce unreadable/path_absent. Implement all eight operators and reject malformed operands/operators defensively.
- Request hash fields are exactly `{claim_id, issuer_identity, idempotency_key, expected_resolved, definition_version, validity_seconds}`. Outcome fields are exactly `{verdict, reason, measurement}` with the existing `measurement_to_json` payload, including its `value` field. Do not invent a second measurement transport.
- `ClaimVerificationError` carries only a closed stable code and constant safe detail, never a DB exception or probe exception message.

- [x] Write table-driven comparison tests, including holds/falsified cases for every operator, unreadable, escaped pointers, arrays, missing versus null, nullable equality, bool/int, malformed operators, and immutable inputs.
- [x] Add fingerprint tests for stable mapping order, request independence from measurements/time, changed immutable inputs, full-size valid values in an envelope, and malformed/oversized envelopes.
- [x] Implement the pure functions and identity accessor after its GitNexus check. Original RED evidence was retained only for the registry accessor, not the comparator/fingerprint modules; this is an explicit process evidence gap. Review found non-ASCII array indexes and unbounded integer conversion; both have new verbatim RED and GREEN evidence.
- [x] Run focused tests and module-layering check; run required repository gates before a local task commit. Parent suite: 11,080 passed, 118 skipped; after the localized pointer correction, 84 focused tests passed. Ruff, formatting, mypy and layering passed on the final files; independent review SHIP.

### Task 2: Append-only repository and server verification coordinator

**Files:**
- Create `src/brain_v42/repositories/pg_claim_verdicts.py` and `src/brain_v42/facts/verification.py`.
- Create `tests/unit/facts/test_claim_verification.py` and `tests/integration/db/test_claim_verification.py`.

**Interfaces and behavior:**
- Repository dataclasses hold a scoped claim snapshot (including project, retirement state and the historical definition's `ttl_seconds`) and a complete verdict row (server-generated id, seq and recorded_at included). Join the immutable definition, locking only the claim row with `FOR UPDATE OF knowledge_claims`.
- Repository functions operate on a caller-owned `AsyncSession`: locked scoped claim lookup, request-key lookup, observation lookup, append with RETURNING, and a small conversion function. SQL owns row IDs/order; no implicit commits. Both uniqueness constraints remain authoritative.
- `ClaimVerificationService(registry, session_factory, clock=UTC_now)` exposes `verify(claim_id, issuer_identity, issuer_kind, idempotency_key, project_key=None, session=None)`. With no session it owns exactly one transaction; with a session the caller owns commit/rollback. Do not read the live descriptor before replay.
- Validate bounded issuer/key and kind before opening a transaction. Load the claim with scope + row lock; refuse missing/out-of-scope identically and retired explicitly. Compute the request hash from the persisted immutable fields; exact replay returns the stored row, mismatch raises idempotency_conflict before any probe.
- Measure through `FactRegistry` only. Check fact name, definition version, target and independent expected identity before comparison. Mismatches become unreadable with the spec's reason. An `Unreadable` has no source identity: validate its name/version/target, then preserve its error reason.
- Enforce `measured_at <= clock()+60s` before writing and retain exact serialized measurement and its digest (null for unreadable). Resolve duplicate observations with the one-refresh rule above. Insert a complete verdict and return it.
- Expected safe errors: invalid_argument, claim_not_found, claim_retired, idempotency_conflict, observation_already_verified, invalid_emitted_at. Do not catch cancellation or turn programming/DB faults into falsification.

- [ ] RED unit tests for guards, probe failures, identity/name/version/target mismatches, future time refusal, replay with no probe, and bounded refresh. Removed/changed definitions with a caller-selected validity different from the original TTL must preserve the historical TTL.
- [ ] RED real-PostgreSQL tests in the module's own disposable database: a real registry probe and a real claim produce durable holds; false values produce falsified; timeout produces replayable unreadable; exact duplicate requests converge to one row and one probe under concurrency; differing keys refresh the already-used observation; rollback removes claim/entry/verdict together; retirement race is serialized; scope refusal does not probe or reveal a replay.
- [ ] Implement, run focused tests and inspect the actual stored records. Verify full-size evidence, server seq order and trigger refusal of UPDATE/DELETE. No skipped DB tests count as successful evidence.
- [ ] Run required gates, `detect_changes` and local task commit.

### Task 3: MCP verification boundary and documentation

**Files:**
- Create `src/brain_v42/mcp/tools/claim_tools.py`.
- Modify `src/brain_v42/mcp/server.py`, `src/brain_v42/mcp/business_errors.py` and catalogue metadata only where required by existing contracts.
- Create `tests/unit/mcp/test_claim_tools.py`; extend existing composition/catalogue/doc tests as needed.
- Update the relevant sections of `docs/MCP_TOOLS.md` and `docs/ARCHITECTURE.md`.

**Public tool:** `brain_claim_verify(claim_id: full UUID, idempotency_key: bounded nonblank string)` returns the full verdict as structured data. Issuer is `mcp:<get_current_actor()>`, kind robot. Unknown/unexpanded actor is refused. A scoped Dream context contributes its trusted `project_key` to the service; no public project override. Lot C will add the verify phase allowlist and run identity; do not enable existing phases here.

**Required C prerequisite:** the current Dream scope carries project, principal and phase, but no trustworthy run ID. Before exposing this tool to the new phase, C must bind a run ID verified against the server/orchestrator to that scope and derive `dream:verify:<run_id>` from it. Never infer a run ID from `X-Brain-Agent` or accept an unverified MCP argument as issuer provenance. The existing capability middleware must continue refusing every Dream phase in B3.

- [ ] RED tests through real FastMCP clients in full and compact profiles. A successful request reaches the coordinator with server-owned issuer/scope; unknown actor is refused; malformed UUID/key is rejected; extra measurement/outcome/timestamp arguments cannot influence the call; safe verification errors survive masking, arbitrary exceptions remain masked.
- [ ] Wire one coordinator from the existing session factory and registry; register the versioned write tool with facts tagging. Mark it idempotent/non-destructive accurately under existing annotation conventions; do not mark it read-only.
- [ ] Update documentation with a concrete declaration/verification/replay example and the fresh-observation behavior. Document that verification does not archive entries or alter ranking.
- [ ] Run focused transport/composition tests, required gates and final real-PG verification tests, then `detect_changes` against main and inspect the entire branch diff before commit.

## Required commit gates and completion evidence

```bash
umask 022
env -u BRAIN_POSTGRES_URL .venv/bin/pytest -q tests/unit
.venv/bin/ruff check src/ tests/
.venv/bin/ruff format --check src/ tests/
.venv/bin/mypy src/
.venv/bin/python scripts/check_module_layering.py --package src/brain_v42
```

Run applicable DB tests using the isolated fixture and a validated test-DB bootstrap URL supplied only through the environment (never print credentials). Record the exact test summary and whether any test skipped. Each task records RED, GREEN, changed files and commit; independent review uses the actual diff from this plan's base. User acceptance of the full program additionally requires measured-provenance writers, B4, C and D; this plan alone cannot complete the goal.

The ignored worktree `.env` contains only a nonconnecting dummy `POSTGRES_URL` for unit settings. Do not export the prefixed alias globally: it overrides tests that intentionally supply their own legacy setting. `umask 022` lets security fixtures create non-group-writable release files; the default host mask 002 otherwise causes two baseline preflight failures. The local restricted sandbox blocks aiohttp binds and SQLite's thread wakeups; the baseline SQLite case times out there and passes in 1.03 s with an authorized unsandboxed test invocation. Run full gates in that proven context, not by removing the timeout guard or skipping these tests.
