# Classification Orphan Project-Key Bind Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore canonical classification-orphan reads by giving asyncpg an explicit PostgreSQL type for the nullable `project_key` bind.

**Architecture:** Keep the existing repository method and raw SQL query. Add the same explicit `VARCHAR` cast already used by `RoadmapService`, strengthen the unit SQL contract, and use the existing PostgreSQL integration test as the behavioral proof.

**Tech Stack:** Python 3.12+, SQLAlchemy 2.x, asyncpg, PostgreSQL 16, pytest/pytest-asyncio, Ruff.

## Global Constraints

- Modify only the orphan query, its repository unit test, and this plan.
- Preserve the method signature, bound values, result shape, project scoping, ordering, limit, and read-only behavior.
- Run database tests only with both `BRAIN_V42_TEST_DB_URL` and `POSTGRES_URL` set to the disposable `brain_test` database on `127.0.0.1:55432`; never use database `brain` or port `5433`.
- Do not modify migrations, Neo4j, Dream status handling, AV1 files, or production data.
- Run GitNexus change detection before committing.

---

### Task 1: Type the Nullable Project-Key Bind

**Files:**
- Modify: `src/brain_v42/repositories/pg_graph_ledger.py:189-232`
- Modify: `tests/unit/repositories/test_pg_graph_ledger.py:1020-1052`
- Test: `tests/integration/db/test_graph_classification_orphans.py`

**Interfaces:**
- Consumes: `PgGraphLedgerRepo.list_active_classification_orphans(*, limit: int = 20, project_key: str | None = None) -> list[dict[str, Any]]`.
- Produces: the same method contract with a PostgreSQL-preparable nullable `project_key` predicate.

- [ ] **Step 1: Reproduce the behavioral RED on PostgreSQL**

Run:

```bash
BRAIN_V42_TEST_DB_URL=postgresql+asyncpg://brain_test:brain_test@127.0.0.1:55432/brain_test \
POSTGRES_URL=postgresql+asyncpg://brain_test:brain_test@127.0.0.1:55432/brain_test \
.venv/bin/python -m pytest \
tests/integration/db/test_graph_classification_orphans.py::test_lists_only_active_canonical_orphans_before_limit -q
```

Expected: FAIL on the first repository call with `asyncpg.exceptions.AmbiguousParameterError: could not determine data type of parameter $1`. A skip, connection error, or another failure is not an acceptable RED.

- [ ] **Step 2: Strengthen the unit SQL contract**

In `test_list_active_classification_orphans_uses_canonical_filters`, add this assertion after normalizing `sql`:

```python
assert "cast(:project_key as varchar) is null" in sql
```

Keep the existing parameter equality assertion unchanged so the test proves that callers still bind `{"limit": 7, "project_key": "brain-v42"}`.

- [ ] **Step 3: Verify the unit contract is RED**

Run:

```bash
.venv/bin/python -m pytest \
tests/unit/repositories/test_pg_graph_ledger.py::test_list_active_classification_orphans_uses_canonical_filters -q
```

Expected: FAIL because the normalized SQL still contains `:project_key is null` without the required cast.

- [ ] **Step 4: Implement the minimal query fix**

In `PgGraphLedgerRepo.list_active_classification_orphans`, replace only the nullable predicate:

```sql
AND (CAST(:project_key AS VARCHAR) IS NULL OR candidate.project_key = :project_key)
```

Do not change parameter construction or any other predicate.

- [ ] **Step 5: Verify targeted GREEN behavior**

Run:

```bash
.venv/bin/python -m pytest \
tests/unit/repositories/test_pg_graph_ledger.py::test_list_active_classification_orphans_uses_canonical_filters -q

BRAIN_V42_TEST_DB_URL=postgresql+asyncpg://brain_test:brain_test@127.0.0.1:55432/brain_test \
POSTGRES_URL=postgresql+asyncpg://brain_test:brain_test@127.0.0.1:55432/brain_test \
.venv/bin/python -m pytest \
tests/integration/db/test_graph_classification_orphans.py::test_lists_only_active_canonical_orphans_before_limit -q
```

Expected: both commands PASS.

- [ ] **Step 6: Run focused regressions and static checks**

Run:

```bash
.venv/bin/python -m pytest \
tests/unit/repositories/test_pg_graph_ledger.py \
tests/unit/services/test_durable_graph_service.py -q

.venv/bin/ruff check \
src/brain_v42/repositories/pg_graph_ledger.py \
tests/unit/repositories/test_pg_graph_ledger.py

.venv/bin/ruff format --check \
src/brain_v42/repositories/pg_graph_ledger.py \
tests/unit/repositories/test_pg_graph_ledger.py

git diff --check
```

Expected: all tests and checks exit zero.

- [ ] **Step 7: Prove the test detects a regression**

Temporarily restore the old untyped predicate, rerun the targeted unit and integration commands from Step 5, and verify the unit assertion fails while PostgreSQL raises `AmbiguousParameterError`. Restore the cast and rerun both commands to green. Do not commit the temporary mutation.

- [ ] **Step 8: Detect scope and commit**

Run GitNexus change detection for all worktree changes. Confirm it reports no unexpected production symbol or process beyond the orphan repository method and its test coverage.

Then run:

```bash
git add \
docs/superpowers/plans/2026-07-27-orphan-project-key-bind-fix.md \
src/brain_v42/repositories/pg_graph_ledger.py \
tests/unit/repositories/test_pg_graph_ledger.py

git commit -m "🐛 fix: type orphan project-key bind"
```

Record exact RED, GREEN, mutation, GitNexus, and commit evidence in the SDD task report.
