# Classification Orphan Project-Key Bind Fix

**Date:** 2026-07-27
**Status:** Approved for implementation
**Scope:** `PgGraphLedgerRepo.list_active_classification_orphans()`

## Problem

The canonical orphan query uses the same untyped bind parameter in an `IS NULL` check and a text comparison:

```sql
(:project_key IS NULL OR candidate.project_key = :project_key)
```

PostgreSQL cannot infer `$1`'s type while asyncpg prepares this statement. Every scoped call fails with `AmbiguousParameterError` before the query returns a row. The failure reproduces on clean `main` under Python 3.12 and blocks the full test suite.

## Selected design

Cast the bind parameter only where PostgreSQL needs an explicit type:

```sql
(CAST(:project_key AS VARCHAR) IS NULL OR candidate.project_key = :project_key)
```

This matches the established `_ROADMAP_SQL` pattern. It preserves the method signature, parameter values, project filtering, unscoped `None` behavior, ordering, limit, and read-only transaction semantics.

## Alternatives considered

1. Attach a SQLAlchemy `String` type with `bindparams()`. This would work, but the type contract would sit outside the SQL text and be easier to lose during query edits.
2. Build separate scoped and unscoped SQL strings. This avoids nullable binds but duplicates query construction for one predicate.
3. Cast the parameter in the SQL text. This is the smallest change and follows an existing PostgreSQL query in the repository. This is the selected approach.

## Tests

- Keep the existing PostgreSQL integration test as the behavioral RED: it currently fails on the first scoped call.
- Strengthen the repository unit contract to require `CAST(:project_key AS VARCHAR) IS NULL`.
- After the change, run the targeted unit and integration tests, graph-ledger and durable-service regressions, Ruff, formatting, and `git diff --check`.
- Run the complete suite against the disposable `brain_test` PostgreSQL database before integrating either this fix or the AV1 proof.

## Non-goals

- Change orphan-selection semantics or public interfaces.
- Refactor the raw query into SQLAlchemy expressions.
- Modify migrations, Neo4j, Dream status handling, or production data.
- Bundle the AV1 proof into this branch.

## Success criteria

- The scoped integration call returns the expected active orphan instead of raising `AmbiguousParameterError`.
- Project isolation, deterministic ordering, relation exclusions, and limit behavior remain unchanged.
- The complete suite has no failure in `test_graph_classification_orphans.py`.
