VERDICT:

| Decision | Status |
|---|---|
| Implementation of lot A may start | PATCH_THEN_SHIP |
| Lot B model may be frozen for a migration | REWORK |

SCOPE:

| File | Lines read |
|---|---|
| `docs/superpowers/specs/2026-09-19-measured-facts-and-claims-design.md` | 1–807 |
| `docs/superpowers/specs/2026-09-19-measured-facts-and-claims-review-codex-astra.md` | 1–169 |
| `src/brain_v42/maintenance/plan_index_repair_store.py` | 220–298 |
| `src/brain_v42/models/delivery_hashes.py` | 20–83 |
| `src/brain_v42/repositories/pg_graph_ledger.py` | 18–48, 130–180, 764–800 |
| `src/brain_v42/metrics/collector.py` | 581–595, 668–744 |
| `src/brain_v42/services/dream_run_service.py` | 29–155 |
| `src/brain_v42/release.py` | 1–128 |
| `src/brain_v42/mcp/server.py` | 195–285 |
| `src/brain_v42/mcp/provenance_middleware.py` | 72–110 |
| `alembic/versions/033_graph_relation_ledger.py` | 20–76, 246–283, 559–658, 1591–1635 |

FINDINGS OF REVISION 1:

| Finding | Status | Revision 2 section | What is missing |
|---|---|---|---|
| 1. Declared target does not prove the queried target | partially lifted | §§5.3, 5.5, 6.3 | Expected production identity is derived from the same `settings.postgres_url` that opens the connection (§5.3). A wrong DSN can therefore self-confirm. Compare the full independently declared identity, including address, port, and database. |
| 2. “Current verdict” could remain false | lifted | §§6.2, 6.4 | Server `seq`, future-date refusal, distinct history/latest/current reads, and read-time expiry settle the original defect. |
| 3. Retired claims cannot be reasserted / no supersession | partially lifted | §§6.1, 6.5 | Key versus occurrence, active-only uniqueness, and CAS exist, but §6.1 promises a replacement link while §6.5 makes `replaces` optional. Reassertion must either require a predecessor or explicitly exclude itself from supersession semantics. |
| 4. Claim history lacks durable protection | partially lifted | §§6.1, 6.2, 6.7 | FK, SQL immutability triggers, and grants are specified. Claim-occurrence idempotency and concurrent create/delete behaviour are still unspecified; the verdict idempotency rule is not sufficient. |
| 5. Shared-task isolation, shutdown, and deadlines | partially lifted | §§5.3–5.4 | Registry-owned shielded tasks, global producer limit, and shutdown sequencing are good. “Read-only autocommit” plus `SET TRANSACTION READ ONLY` cannot provide the claimed shared read-only snapshot; require one explicit read-only transaction covering probe and identity. |
| 6. Cache freshness contradicts its promise | lifted | §5.4 | Monotonic timing, zero-age bypass, negative-age refusal, and negative-cache semantics are explicit. |
| 7. Mutable measurement / incompatible digest | lifted | §5.2 | Canonical JSON text, fresh parsed values, typed states, versioned prefix, no floats, and bounds address the defect. |
| 8. Existing readers cannot be used as strict probes | lifted | §§4, 5.6 | Strict adapters are specified; fallback-bearing readers are not reused, and model liveness is deferred. |
| 9. Authority to turn a declaration into evidence | lifted | §§6.2–6.4 | No tool accepts measurement JSON; the server measures and persists verdict reasons. |
| 10. “Projection up to date” overclaims | lifted | §§5.5, 5.7 | The specification now limits proof to PostgreSQL outbox/lease state, preserves exhausted rows, and explicitly excludes Neo4j-content proof. |
| 11. Canary/tests did not cover wired behaviour | lifted | §§7.1–7.2 | The canary forces a probe before cache validation; briefing uses the shared loader; native and compact middleware paths are covered; probe-only read-only scope is explicit. |

REQUIRED GATES OF REVISION 1:

R1 contains six bullets at lines 149–154, rather than five; all are assessed to avoid omitting one.

| Gate | Status | Revision 2 section | What is missing |
|---|---|---|---|
| Settle lot-A contracts | partially lifted | §§5.2–5.4 | Independent target identity and an actual shared read-only transaction/snapshot remain unresolved. |
| Complete §7.1 acceptance scenarios | partially lifted | §7.1 | Most concurrency, cache, target, and projection cases are present. Add start/resume coverage and Dream-denial assertions; prove atomic probe-plus-identity handling. |
| Freeze lot-B invariants before migration | not lifted | §§6.1–6.8 | Inline probes, project scoping, historical descriptor/policy resolution, mandatory supersession, destructive CAS, bounded claim JSON, and full idempotency remain incomplete. |
| Make the B/D boundary explicit | lifted | §§3, 5.9, 6.7 | No claim/fact outbox events are emitted before lot D extends the ledger; source confirms the current catalogue rejects those kinds. |
| Make migration and rollback concrete | partially lifted | §6.8 | Clone/rollback intent and pin inventory are described, but the inventory is deferred to a future PR and `brain_entities` coverage counts are not verifiable from this commit. Require a dated read-only anti-join/count proof before applying the FK. |
| Correct canary and read-only scope | partially lifted | §§5.3, 5.8, 7.1–7.2 | Canary order and transport scope are corrected, but target identity and read-only transaction semantics still permit misleading evidence. |

NEW FINDINGS:

1. **P0 — confirmed — target verification is self-confirming.** Spec §§5.3, 6.3, and §9 question 1; `src/brain_v42/db/engine.py:37-47`. The expected identity comes from the same DSN used to connect, and comparison expressly names only database and port although the measured identity includes server address. A wrongly configured production DSN can yield a “measured production” verdict. Fix: deploy an independently controlled, non-secret expected cluster identity and compare all fields; fail closed.

2. **P0 — confirmed — “read-only autocommit” cannot provide the promised one-moment identity/value observation.** Spec §5.3:262–268; `plan_index_repair_store.py:222-225`. The cited precedent uses `session.begin()` before `SET TRANSACTION READ ONLY`; revision 2 instead specifies autocommit and separate queries. Fix: require one explicit PostgreSQL read-only transaction with stated isolation covering probe, identity, and Alembic read; test a real rejected `INSERT`.

3. **P1 — confirmed — a valid lot-B `probe` claim cannot be verified.** Spec §§6.1, 6.3. The schema permits `probe` with `fact_name=NULL`, but verification always calls the registry by fact and compares `measurement.fact == claim.fact_name`. Fix: defer inline probes from lot B, or define a server-resolved, versioned, identity-bound descriptor for every permitted probe kind.

4. **P1 — confirmed — claim project scope can diverge from its durable entity anchor.** Spec §6.1; `033_graph_relation_ledger.py:246-270`. The proposed trigger checks only `entity_type`; `project_key` has an independent FK and can name another project. Fix: trigger-enforce both `entity_type` and project/scope equality, including NULL/global semantics.

5. **P1 — confirmed — historical claim evaluation depends on a mutable current descriptor.** Spec §§5.1, 5.4, 6.1, 6.4. Claims retain only a definition-version integer, while the registry retains one current registration and policies/TTL resolve from that current descriptor. A changed descriptor can alter or make impossible historical policy resolution. Fix: retain immutable descriptor snapshots by `(fact_name, definition_version)`, or store the resolved policy and validity bound on each occurrence.

6. **P1 — confirmed — `claims: []` bypasses compare-and-swap.** Spec §6.5. Controlled replacement requires `expected_active_claim_ids`, but empty-list retirement does not; it can retire a concurrent replacement. Fix: require the expected active set for every destructive claims mutation, including `[]`.

7. **P1 — confirmed — verdict idempotency is undefined for unreadable measurements.** Spec §6.2. Replays compare `measurement_digest`, but this is NULL for unreadable measurements. Fix: define and store a canonical full-measurement/request fingerprint and compare it on every idempotency replay.

8. **P2 — confirmed — claim JSON and JSON Pointer processing are unbounded.** Spec §§6.1, 6.5. `expected`, `params`, and `probe` have no byte, depth, pointer-segment, or operator-domain limits. Fix: introduce a closed `ClaimInput` validator with explicit bounds and refusal tests before writes.

9. **P2 — confirmed — `measure_many()` accepts an unbounded iterable.** Spec §5.4. A closed catalogue does not bound a caller-supplied iterable containing repeated known names; scheduling/materialisation can grow without bound. Fix: accept a bounded unique sequence, cap its cardinality before creating tasks, and restrict briefing calls to registered briefing names.

10. **P3 — confirmed — one cited source fact is misstated.** Spec §4:136; `plan_index_repair_store.py:222-225,291-298`. The precedent reads identity inside an explicit read-only snapshot transaction, not a mutation transaction. Fix: correct the citation and use its transaction shape as the contract precedent.

ANSWERS TO §9:

1. Use an independently declared expected production identity, not one derived from the probe DSN.
2. Keep a per-fact, per-process bucket and add a server-wide target budget; do not key enforcement to spoofable caller headers.
3. Keep denormalised `entity_type`, but enforce both type and project equality against `brain_entities` by trigger.
4. Treat stale `holds` as neither holding nor falsified; allow it to request re-measurement, never to drive archiving.
5. Keep active-set CAS, but require it for `claims: []` as well as replacements; an extra five-table revision counter is unnecessary.
6. Fail closed on independent-identity mismatch and on any inability to obtain one explicit read-only observation transaction.

RESIDUAL_RISKS:

- PostgreSQL outbox/lease evidence still cannot attest Neo4j content.
- A per-process cache deliberately permits staleness within the declared TTL.
- Production coverage counts, expected target identity, and migration-clone proof require operational read-only evidence.
- Lots C/D remain deferred; they must consume immutable fact-definition history rather than a mutable current descriptor.

REQUIRED_SIGNATURE: role=independent-spec-reviewer expected_model=gpt-5.6-terra permissions=read-only