VERDICT: Lot A implementation = PATCH_THEN_SHIP; Lot B migration-model freeze = PATCH_THEN_SHIP

SCOPE: revision 4 1–920; revision-3 review 1–53; `plan_index_repair_store.py` 222–298; `delivery_hashes.py` 25–83; `pg_graph_ledger.py` 27–48, 162–180; `collector.py` 581–758; `dream_run_service.py` 29–169; `release.py` 1–128; `033_graph_relation_ledger.py` 16–24, 246–283, 559–658; `pg_delivery_attestations.py` 150–176.

FINDINGS OF REVISION 3:

| Finding | Status | Section | What is missing |
|---|---|---|---|
| P0 target identity | partially lifted | §5.3 rule 4 | Four fields are mandatory, but §5.1 still defines PostgreSQL `SourceIdentity` as database/address/port/Alembic revision, conflicting with §5.3’s system identifier/database/address/port. |
| P1 historical fact definitions | partially lifted | §§6.0–6.1 | The immutable table and FK exist in the design, but the canonical definition payload/digest and descriptor-provided `value_keys` are not specified. |
| P1 input-based replay fingerprint | lifted | §§6.2–6.3 | — |
| P1 latest retired predecessor | lifted | §§6.1, 6.5 | — |

NEW FINDINGS:

1. **P1 — confirmed — §§5.1, 5.3 rule 4:** the two PostgreSQL identity contracts disagree; the colon-delimited setting is also ambiguous for IPv6 addresses. Define one typed `SourceIdentity`, exclude Alembic from target identity, and use canonical JSON or four separately validated settings.

2. **P1 — confirmed — §6.0, §5.3 `Probe`:** `knowledge_fact_definitions.digest` has no versioned canonical payload definition, and `Probe` exposes no value-shape metadata from which `value_keys` can be created without measuring. Add an immutable descriptor snapshot, including value schema, canonical digest recipe/domain, and an explicit exclusion of `registered_at`.

3. **P2 — confirmed — §5.3:294–297; `plan_index_repair_store.py:222–225`:** the cited precedent uses `SET TRANSACTION READ ONLY`, not `REPEATABLE READ`; it is not “exactly” the proposed transaction shape. Retain explicit repeatable-read design, but correct the citation and add the new SQL statement explicitly.

4. **P3 — confirmed — §4; `033_graph_relation_ledger.py:16–24`:** migration 033 backfills seven knowledge entity kinds, not five. State that claims intentionally exclude `feature` and `plan`, or extend the allowed entity types.

ANSWERS TO §9:

1. Keep the fail-closed redeclaration after address change; do not pin Compose IPs solely to avoid it.
2. Start the service, but disable only the drifting fact and reject its claims/verdicts; emit a high-severity journal event.
3. Treat a physically cloned cluster that assumes the same address as a residual root-of-trust limit; document it and require an external deployment-control check if it must be detected.

RESIDUAL_RISKS:

- PostgreSQL outbox/lease evidence still cannot prove Neo4j content.
- A physical clone retaining all four identity fields can be indistinguishable after endpoint takeover.
- Per-process TTL caching deliberately permits bounded staleness.

REQUIRED_SIGNATURE: role=independent-spec-reviewer expected_model=gpt-5.6-terra permissions=read-only