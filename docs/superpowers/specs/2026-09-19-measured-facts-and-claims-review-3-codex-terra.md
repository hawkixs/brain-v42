VERDICT:

| Decision | Status |
|---|---|
| Implementation of lot A may start | PATCH_THEN_SHIP |
| Lot B model may be frozen for a migration | REWORK |

SCOPE:

- `docs/superpowers/specs/2026-09-19-measured-facts-and-claims-design.md`: 1–868
- `docs/superpowers/specs/2026-09-19-measured-facts-and-claims-review-2-codex-terra.md`: 1–91
- `plan_index_repair_store.py`: 222–298; `delivery_hashes.py`: 25–83
- `pg_graph_ledger.py`: 150–170; `collector.py`: 580–745
- `dream_run_service.py`: 29–165; `release.py`: 1–128
- `033_graph_relation_ledger.py`: 246–283, 559–658

FINDINGS OF REVISION 2:

| Finding | Status | Section | What is missing |
|---|---|---|---|
| P0-1 self-confirming target identity | partially lifted | §5.3 rule 4 | Independent configuration is now present, but database OID plus optional address can still accept a wrong endpoint. |
| P0-2 one read-only snapshot | lifted | §5.3, §7.1 | — |
| P1-3 unverifiable inline probe | lifted | §6.1 | — |
| P1-4 project/anchor divergence | lifted | §6.1 | — |
| P1-5 mutable descriptor affects history | lifted | §§6.1, 6.4 | — for verdict evaluation. Historical definition data is still missing for lot D. |
| P1-6 empty claims bypasses CAS | lifted | §6.5 | — |
| P1-7 unreadable replay comparison | partially lifted | §6.2 | The fingerprint includes a newly produced measurement, so a retry cannot compute the prior fingerprint before re-measuring. |
| P2-8 unbounded ClaimInput | lifted | §6.5 | — |
| P2-9 unbounded `measure_many` iterable | lifted | §5.4 | — |
| P3-10 incorrect transaction precedent | lifted | §§4, 5.3 | — |

NEW FINDINGS:

1. **P0 — confirmed — §5.3:310–321, §7.1:777–780:** a database OID is a catalog row identifier, not a globally unique instance identity; address comparison is optional. A wrong database can share database name, port, and OID and be accepted as production. [PostgreSQL documents OIDs as internal identifiers with limited uniqueness](https://www.postgresql.org/docs/17/datatype-oid.html). Fix: require an independently declared, mandatory endpoint identity and a collision-resistant cluster marker; fail closed on either mismatch.

2. **P1 — confirmed — §§5.4:308–309, 5.9:531–534, 6.1:570–571:** storing resolved expected values preserves verdict evaluation, but not the historical fact definition required for lot-D nodes keyed by `(fact_name, definition_version)`. The registry retains only one definition per fact name. Fix: add an immutable, canonical fact-definition snapshot/registry table keyed by name and definition version, referenced by claims.

3. **P1 — confirmed — §§6.2:603–604, 6.3:615–628:** `request_fingerprint` is an outcome fingerprint, not a retryable request fingerprint: it requires a fresh measurement and new observation ID before it can be compared. Fix: fingerprint immutable verify-request inputs, look up idempotency before measuring, and retain the outcome fingerprint separately if useful.

4. **P1 — confirmed — §§6.1:576, 6.5:674–677:** mandatory `replaces` permits any retired same-key occurrence, not the immediate prior occurrence. A supersession chain can fork or skip history. Fix: trigger-enforce the latest retired occurrence of that entity/key as the sole permissible predecessor.

ANSWERS TO §9:

1. Use a mandatory, independently declared cluster identity plus endpoint identity; do not use database OID as the instance marker.
2. Preserve `expected_resolved` and `validity_seconds`, but also retain immutable historical fact definitions for lot D.
3. After fixing target identity, require a collision test for a wrong endpoint with matching name/port/OID; other specified probe failures correctly become `unreadable`.

RESIDUAL_RISKS:

- PostgreSQL outbox/lease evidence cannot prove Neo4j content.
- Per-process TTL caching deliberately permits bounded staleness.
- Production identity and migration coverage still need operational read-only proof.

REQUIRED_SIGNATURE: role=independent-spec-reviewer expected_model=gpt-5.6-terra permissions=read-only