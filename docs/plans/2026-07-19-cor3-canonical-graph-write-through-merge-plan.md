---
title: "COR3 — Canonical graph write-through merge"
status: completed
completed_at: "2026-07-19"
deployed_at: "2026-07-19"
summary: "Move entity merge orchestration out of the MCP handler, commit PostgreSQL state and audit atomically, then attempt the canonical Neo4j MERGED_INTO write-through observably."
tags:
  - sol-ultra
  - cor3
  - consolidation
  - neo4j
  - tdd
---

# COR3 — Canonical graph write-through merge

## Goal

Make `ConsolidationJob.merge` the sole merge orchestration used by `brain_merge_entities`.
PostgreSQL remains authoritative; the normal path immediately writes `MERGED_INTO` to Neo4j,
while reconciliation remains a recovery mechanism.

## Starting evidence

- The MCP handler currently locks and mutates PostgreSQL rows directly and commits before
  calling the graph helper.
- Consolidation audit logging uses a separate transaction.
- Admin merges do not write the graph edge, and a missing consolidation job can still mutate
  PostgreSQL.

## Acceptance criteria

1. The MCP handler validates/parses, delegates once to `ConsolidationJob.merge`, and formats
   the existing response; it opens no SQL session and fails closed when the job is absent.
2. The job validates the five mergeable types, locks source and target deterministically,
   applies project predicates when scoped, merges tags, updates both rows, and writes the
   consolidation log through an injected-repository `log_action_in_session(...)` seam in one
   PostgreSQL transaction.
3. Missing, foreign-scoped, same-ID, unknown-type, or audit-write failures leave no partial
   PostgreSQL change and do not call Neo4j.
4. After the PostgreSQL commit, admin and scoped merges both attempt the canonical
   `MERGED_INTO` graph write. `authorization` remains an internal optional keyword-only
   argument: the handler passes it explicitly, the job never rereads the request ContextVar,
   and an admin call ignores any accidentally bound scope.
5. Graph absence, error, or missing-node outcomes leave PostgreSQL authoritative and emit a
   bounded observable degradation without exposing protected identifiers.
6. Scoped authorization failures retain the existing fail-closed security contract.
   `DreamProjectAuthorizationError` crosses the job without wrapping or secondary logging;
   no identifier, Cypher, content, or exception detail is emitted. If graph revalidation
   rejects after the authoritative PostgreSQL commit, PostgreSQL remains committed under the
   existing SEC1b contract; this is not reported as a rollback. Technical Neo4j failures are
   degradable, authorization denials are not.
7. Public MCP parameters and database schema remain unchanged; plans do not become mergeable.
8. Isolated PostgreSQL/Neo4j integration proofs skip safely when dedicated test endpoints are
   unavailable and never fall back to live services.

## TDD sequence

1. Add non-skippable handler tests proving delegation, a bomb session factory that forbids
   direct SQL/table/audit-repository
   knowledge, explicit scope forwarding, admin scope isolation, and missing-job fail-closed
   before any session opens. Invalid/same-ID requests never call the job.
2. Add non-skippable job/repository tests for sorted locks, scoped predicates, two row updates
   plus audit inside one transaction, and no graph call on audit failure. A SQL ownership
   denial before commit leaves every row and the audit log untouched.
3. Add an isolated PostgreSQL failure-injection test that raises in the audit seam after real
   updates and proves source, target, and audit are unchanged from a new session.
4. Add non-skippable admin and scoped MCP → real job → fake graph tests. A shared timeline
   proves transaction exit/commit precedes exact `source -> target MERGED_INTO` creation and
   that scoped authorization is forwarded unchanged. Replace the regression that currently
   asserts admin merge has no graph effect.
5. Add non-skippable tests for absent graph, exceptions, and missing nodes: PostgreSQL remains
   committed, authorized MCP output remains successful, warning fields are bounded, and no
   protected UUID, SQL, exception detail, or traceback leaks. Scoped authorization denial is
   non-degradable: PostgreSQL remains committed after post-commit graph revalidation, no edge
   is created, and the authorization exception propagates without a secondary log.
6. Implement the smallest orchestration and repository seam; keep graph reconciliation
   unchanged.
7. Add endpoint-gated PostgreSQL/Neo4j tests proving the edge is visible immediately for both
   admin and scoped calls, then run targeted, integration, unit, lint, format, and type gates.

## Boundaries

No generic unit-of-work layer, migration, graph-as-authority change, reconciliation rewrite,
deployment, or new mergeable entity type belongs to COR3.

## Delivery gate

Unavailable dedicated PostgreSQL or Neo4j endpoints leave COR3 `active` with code complete
and runtime proof pending; it cannot be marked `done` from mocks alone. Completion additionally
requires documented recovery through reconciliation, secret-safe degradation evidence,
`gitnexus_detect_changes()` before commit, a final whole-diff `SHIP` review, and a Brain update
carrying the real evidence. Merge and push required explicit user authorization; it was granted
and the gate was satisfied on 2026-07-19.

## Delivery evidence

- Local commit `cd49067` on `codex/sol-ultra-cor123`.
- Six real integrations validate PostgreSQL rollback/scoping and immediate Neo4j
  `MERGED_INTO` write-through for both admin and project-scoped calls.
- A real failing `GraphService` proves COR3 logs no UUID, Cypher, exception detail, traceback,
  or secret; the historical path remains unchanged by default.
- Two independent reviews approve the diff after the P1/P3 findings were resolved.
- GitLab MR `!66` merged the work into `main` at `75c6b05`; MR pipeline `4145` and main
  pipeline `4146`, including the registry build, both passed. No migration was required.
- A live production MCP merge atomically archived the PostgreSQL source, unioned target tags,
  wrote one consolidation audit row, and exposed one immediate Neo4j `MERGED_INTO` edge. Both
  PostgreSQL and Neo4j fixtures were removed afterward.
