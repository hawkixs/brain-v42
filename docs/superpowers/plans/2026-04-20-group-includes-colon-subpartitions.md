# Fix: `get_keys_by_group()` includes colon-sub-partitions

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `BrainService.search(project_group=X)` return knowledge written under colon-sub-partition keys (e.g. `red-shrik:agent`) whose base key (`red-shrik`) belongs to group `X`, even when the sub-partition itself was never registered in `project_contexts`.

**Root cause:** `PgProjectContextRepo.get_keys_by_group()` at `src/brain_v42/repositories/pg_project_context.py:152` returns only rows present in `project_contexts` with `project_group=X`. Agents that write knowledge under `red-shrik:agent` never call `brain_set_project_context`, so the sub-partition row is absent — the knowledge exists in `decisions`/`learnings`/`snippets`/`runbooks`/`adrs` but is invisible to group-scoped search.

**Fix:** Extend the SQL to UNION the base-key result with distinct `project_key` values scanned from the 5 knowledge tables, filtered to rows whose `split_part(project_key, ':', 1)` matches one of the group's base keys and whose key actually contains `:`.

**Tech Stack:** SQLAlchemy 2.0 async core (no ORM), asyncpg, PostgreSQL 16. Read-only SQL change, no migration. TDD via pytest-asyncio (auto mode — do NOT decorate tests) + real PG. Fixtures `project_context_repo` and `learning_repo` are class-scoped in `tests/integration/test_recent_patches.py:153-169` (NOT in `conftest.py`) — the new test file must replicate that fixture pattern, including the `monkeypatch.setattr("brain_v42.repositories.pg_base.get_session_factory", ...)` trick for the project_context_repo. Module-level `pytestmark = pytest.mark.integration` is mandatory (runs under `-m integration` selector). Cleanup in `conftest.py` only purges keys matching `integ_%` / `integ-%` — helpers must use `integ-<hex>` prefix to stay clean.

**Non-goals:**
- No schema migration (no new column, no backfill).
- No change to `get_or_create` to auto-register colon-partitions (option F from design — deferred).
- No update to red-shrik wrapper; consumer cleanup tracked via superseded decision `a5512212`.
- No performance tuning beyond the single CTE; current group sizes (<100 base keys, <100k knowledge rows) make this trivially fast.

**Reference decisions:**
- `a5512212` — red-shrik fan-out workaround (to supersede after merge).
- `265cfc47` — project_group design decision (still valid, this fix aligns the implementation with the intent).

---

## File Structure

- Modify: `src/brain_v42/repositories/pg_project_context.py` — rewrite `get_keys_by_group()` method body (lines 152-161) to UNION base keys with scanned colon-sub-partition keys.
- Create: `tests/integration/test_project_context_colon_partitions.py` — new integration test file, 3 tests. Follows the pattern of `tests/integration/test_recent_patches.py` (real PG + unique keys per test).
- No other files touched.

---

### Task 1: RED — write failing integration tests

**Files:**
- Create: `tests/integration/test_project_context_colon_partitions.py`

- [ ] **Step 1: Write the test file**

Create the file with this exact content:

```python
"""Integration tests: get_keys_by_group() returns colon-sub-partitions too.

Root issue: agents writing knowledge under colon-prefixed keys like
"red-shrik:agent" never register a project_contexts row, so group-scoped
search misses their entries. After fix, get_keys_by_group() must scan the
5 knowledge tables and include any colon-sub-partition whose base key
belongs to the group.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brain_v42.models.learning import LearningCreate
from brain_v42.models.project_context import ProjectContextCreate
from brain_v42.repositories.pg_learning import PgLearningRepo
from brain_v42.repositories.pg_project_context import PgProjectContextRepo

pytestmark = pytest.mark.integration


def _unique_key() -> str:
    # "integ-" prefix is required for conftest.py cleanup fixture.
    return f"integ-{uuid.uuid4().hex[:8]}"


def _unique_group() -> str:
    return f"integ-group-{uuid.uuid4().hex[:6]}"


class TestGetKeysByGroupColonSubPartitions:
    """Group scan must include colon-sub-partitions even when unregistered."""

    @pytest_asyncio.fixture
    async def project_context_repo(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> PgProjectContextRepo:
        monkeypatch.setattr(
            "brain_v42.repositories.pg_base.get_session_factory",
            lambda: session_factory,
        )
        return PgProjectContextRepo()

    @pytest_asyncio.fixture
    async def learning_repo(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> PgLearningRepo:
        return PgLearningRepo(session_factory=session_factory)

    async def test_base_key_returned_when_registered(
        self,
        project_context_repo: PgProjectContextRepo,
    ) -> None:
        """Regression: a plain base key registered with a group still returns."""
        group = _unique_group()
        base = _unique_key()
        await project_context_repo.get_or_create(
            ProjectContextCreate(project_key=base, name=base, project_group=group)
        )
        keys = await project_context_repo.get_keys_by_group(group)
        assert set(keys) == {base}

    async def test_colon_subpartition_returned_when_base_has_group(
        self,
        project_context_repo: PgProjectContextRepo,
        learning_repo: PgLearningRepo,
    ) -> None:
        """Core fix: an orphan colon-sub-partition is included via knowledge scan."""
        group = _unique_group()
        base = _unique_key()
        sub = f"{base}:agent"

        # base registered with the group
        await project_context_repo.get_or_create(
            ProjectContextCreate(project_key=base, name=base, project_group=group)
        )
        # sub-partition NEVER calls get_or_create — only writes a learning
        await learning_repo.create(
            LearningCreate(
                topic="orphan colon-partition learning",
                insight="scanned from learnings table",
                project_key=sub,
            )
        )

        keys = await project_context_repo.get_keys_by_group(group)
        assert set(keys) == {base, sub}

    async def test_colon_subpartition_isolated_per_group(
        self,
        project_context_repo: PgProjectContextRepo,
        learning_repo: PgLearningRepo,
    ) -> None:
        """Colon-sub-partitions don't leak across groups (negative + positive assertion)."""
        group_a = _unique_group()
        group_b = _unique_group()
        base_a = _unique_key()
        base_b = _unique_key()
        sub_b = f"{base_b}:agent"

        await project_context_repo.get_or_create(
            ProjectContextCreate(project_key=base_a, name=base_a, project_group=group_a)
        )
        await project_context_repo.get_or_create(
            ProjectContextCreate(project_key=base_b, name=base_b, project_group=group_b)
        )
        # Learning written under sub_b — base_b is in group_b, not group_a.
        await learning_repo.create(
            LearningCreate(
                topic="group-isolated sub",
                insight="belongs to group_b only",
                project_key=sub_b,
            )
        )

        # Negative: sub_b not in group_a results
        keys_a = await project_context_repo.get_keys_by_group(group_a)
        assert set(keys_a) == {base_a}
        assert sub_b not in keys_a

        # Positive: sub_b IS in group_b results
        keys_b = await project_context_repo.get_keys_by_group(group_b)
        assert set(keys_b) == {base_b, sub_b}

    async def test_empty_group_returns_empty(
        self,
        project_context_repo: PgProjectContextRepo,
        learning_repo: PgLearningRepo,
    ) -> None:
        """An unknown group with zero base keys returns [] — never leaks colon keys."""
        empty_group = _unique_group()

        # Write a learning under SOME colon key — it must NOT leak to empty_group.
        await learning_repo.create(
            LearningCreate(
                topic="orphan with no registered base",
                insight="must not leak",
                project_key=f"{_unique_key()}:agent",
            )
        )

        keys = await project_context_repo.get_keys_by_group(empty_group)
        assert keys == []

    async def test_colon_key_with_existing_project_contexts_row_dedups(
        self,
        project_context_repo: PgProjectContextRepo,
        learning_repo: PgLearningRepo,
    ) -> None:
        """A colon-sub-partition that IS registered in project_contexts appears exactly once."""
        group = _unique_group()
        base = _unique_key()
        sub = f"{base}:agent"

        # base registered
        await project_context_repo.get_or_create(
            ProjectContextCreate(project_key=base, name=base, project_group=group)
        )
        # sub ALSO registered in project_contexts (colon-key with group set)
        await project_context_repo.get_or_create(
            ProjectContextCreate(project_key=sub, name=sub, project_group=group)
        )
        # sub also writes knowledge
        await learning_repo.create(
            LearningCreate(
                topic="dedup test",
                insight="should appear once",
                project_key=sub,
            )
        )

        keys = await project_context_repo.get_keys_by_group(group)
        # Exactly 2 keys, no duplicates — UNION dedups across base + sub paths.
        assert sorted(keys) == sorted([base, sub])
        assert len(keys) == 2
```

- [ ] **Step 2: Confirm RED**

Run:
```bash
cd /home/hawixs/hawkixs_infra/git_repo/brain_v42
pytest tests/integration/test_project_context_colon_partitions.py -v
```

Expected (before fix):
- `test_base_key_returned_when_registered` ✅ PASS
- `test_colon_subpartition_returned_when_base_has_group` ❌ FAIL (core bug)
- `test_colon_subpartition_isolated_per_group` ❌ FAIL on the `keys_b == {base_b, sub_b}` assertion (core bug, other direction)
- `test_empty_group_returns_empty` ✅ PASS (over-restrictive code accidentally correct)
- `test_colon_key_with_existing_project_contexts_row_dedups` ✅ PASS (sub IS registered with group, so base query alone returns both)

If the 2nd or 3rd test passes without the fix, stop — the premise is wrong.

- [ ] **Step 3: Commit**

```bash
git add tests/integration/test_project_context_colon_partitions.py
git commit -m "test(repo): RED tests for colon-subpartition group scan

Failing before fix: a project_key like 'red-shrik:agent' that writes
knowledge but never registers a project_contexts row is invisible to
get_keys_by_group(base_group), breaking group-scoped brain_search.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: GREEN — implement the UNION SQL

**Files:**
- Modify: `src/brain_v42/repositories/pg_project_context.py`

- [ ] **Step 1: Rewrite `get_keys_by_group()`**

Replace the method body (lines 152-161) with:

```python
    async def get_keys_by_group(self, project_group: str) -> list[str]:
        """Return all project_keys that belong to the given group.

        Includes base keys registered in ``project_contexts`` AND colon-
        sub-partitions scanned from knowledge tables whose base is in the
        group. See docs/superpowers/plans/2026-04-20-group-includes-colon-
        subpartitions.md for rationale.
        """
        base_keys = (
            sa.select(project_contexts.c.project_key)
            .where(project_contexts.c.project_group == project_group)
        ).subquery()

        knowledge_keys = sa.union_all(
            sa.select(decisions.c.project_key),
            sa.select(learnings.c.project_key),
            sa.select(snippets.c.project_key),
            sa.select(runbooks.c.project_key),
            sa.select(adrs.c.project_key),
        ).subquery()

        # split_part(key, ':', 1) returns the prefix, or the full key if no ':'
        # Typed literal for the position arg — PG requires integer type.
        base_prefix = sa.func.split_part(
            knowledge_keys.c.project_key, ":", sa.literal(1, sa.Integer)
        )

        sub_query = (
            sa.select(knowledge_keys.c.project_key)
            .where(knowledge_keys.c.project_key.is_not(None))
            # key contains a colon (prefix != full key)
            .where(base_prefix != knowledge_keys.c.project_key)
            # and the prefix is a base key of the target group
            .where(base_prefix.in_(sa.select(base_keys.c.project_key)))
            .distinct()
        )

        stmt = sa.union(
            sa.select(base_keys.c.project_key),
            sub_query,
        ).order_by(sa.column("project_key"))

        async with self.get_session() as session:
            result = await session.execute(stmt)
            return [row[0] for row in result.fetchall()]
```

**Notes on the SQL:**
- `split_part(col, ':', 1) != col` is the sargable way to say "contains a colon" — no separate POSITION/LIKE call, and the result of `split_part` is reused by the next predicate.
- `sa.literal(1, sa.Integer)` is explicit-typed — PG's `split_part(text, text, integer)` rejects an untyped bind parameter.
- Outer `sa.union` (not `union_all`) dedups when a colon-key is ALSO registered in `project_contexts`.
- `sa.column("project_key")` on `.order_by()` is a proper column reference — not a fragile string literal.
- Scan cost: 5 seq scans on the knowledge tables (expr `split_part(...)` is not indexable on existing indexes). For current data volume (<100k rows total) this is <50ms. If >1M someday, add `CREATE INDEX idx_<tbl>_project_base ON <tbl> ((split_part(project_key, ':', 1)))` — deferred, not in scope.
- PG-only — `split_part` is not standard SQL, but the file already uses `pg_insert` / `on_conflict_do_update`, so this is consistent.

- [ ] **Step 2: Confirm GREEN**

Run:
```bash
pytest tests/integration/test_project_context_colon_partitions.py -v
```

Expected: all 5 tests PASS.

- [ ] **Step 3: Full regression**

```bash
pytest tests/unit tests/integration -x --timeout=120
```

Expected: everything green. The change is additive (UNION), nothing existing should break.

If any previously-green test fails, STOP and analyze — do not patch tests to compensate.

- [ ] **Step 4: Commit**

```bash
git add src/brain_v42/repositories/pg_project_context.py
git commit -m "fix(repo): group scan includes colon-subpartitions

get_keys_by_group() now returns both base keys (from project_contexts)
and colon-sub-partitions (scanned from the 5 knowledge tables) whose
base belongs to the group. Fixes brain_search(project_group=...) miss
for orphan colon-partitions like 'red-shrik:agent'.

Obsoletes the red-shrik fan-out workaround (decision a5512212).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Brain update

- [ ] **Step 1: Supersede decision `a5512212`**

Call `mcp__brain-v42__brain_supersede_decision` with **flat kwargs** (not a nested `new_data` dict):
```
old_decision_id: "a5512212-29e9-40e7-8838-7808bb2ed426"
title: "brain-v42 get_keys_by_group() fixed upstream — red-shrik fan-out obsolete"
context: "Feature branch feat/group-includes-colon-subpartitions merged on brain-v42 main. get_keys_by_group('red') now natively includes colon-sub-partitions via UNION scan of the 5 knowledge tables."
decision_made: "The red-shrik fan-out wrapper (2 MCP calls in src/shrik/tools/knowledge.py:35-50) is no longer necessary. Consumers should collapse to a single brain_search(project_group='red') call when they have time — the old workaround remains functional but redundant."
reasoning: "The upstream fix resolves the root cause (colon-sub-partitions never register a project_contexts row). Fan-out at the consumer level was a valid workaround when brain-v42 was owned by someone else; now that the repo is on this host, fixing upstream beats maintaining N client wrappers."
tags: ["brain", "phase3", "wrapper", "fan-out", "partition", "resolved"]
project_key: "brain-v42"
```

- [ ] **Step 2: Log the new learning**

Call `mcp__brain-v42__brain_learn` with:
```
topic: "brain-v42 get_keys_by_group() now scans knowledge tables for colon-sub-partitions"
insight: |
  As of feat/group-includes-colon-subpartitions (branch merged 2026-04-20),
  get_keys_by_group(group) returns:
    - All project_keys in project_contexts with project_group=group.
    - All distinct project_keys from (decisions, learnings, snippets,
      runbooks, adrs) that contain ':' and whose prefix (before first ':')
      matches one of the base keys above.

  Impact: agents that write under colon-sub-partitions like 'red-shrik:agent'
  no longer need a client-side fan-out wrapper. brain_search(project_group='red')
  sees their knowledge natively.

  Backward compatible: base keys without colon still returned unchanged.
  No migration, purely SQL-level fix.
confidence: "high"
tags: ["brain-v42", "project-groups", "colon-partition", "fix"]
project_key: "brain-v42"
```

---

## Self-Review

**Spec coverage:**
- Failing test for core bug → Task 1 test 2 ✅
- SQL UNION fix → Task 2 ✅
- No schema migration → respected ✅
- TDD → Task 1 RED, Task 2 GREEN ✅
- 5 tests covering: regression (base), core fix (orphan colon), group isolation (negative+positive), empty group, dedup → Task 1 ✅
- Brain supersession (flat kwargs) + new learning → Task 3 ✅

**Post-critique revisions applied (3 parallel judges):**
- Fixtures `project_context_repo` / `learning_repo` now replicated inline from `test_recent_patches.py:153-169` (were NOT in conftest).
- Helpers renamed `_unique_key` / `_unique_group` with `integ-` prefix (matches conftest cleanup).
- `pytestmark = pytest.mark.integration` at module level.
- Removed `@pytest.mark.asyncio` decorator (pyproject has `asyncio_mode = "auto"`).
- SQL `POSITION(':' IN col)` → `split_part(col, ':', 1) != col` (sargable, no malformed operator).
- `sa.literal(1, sa.Integer)` typed for `split_part`'s integer arg.
- `.order_by(sa.column("project_key"))` instead of bare string.
- CTE → plain subquery (single-use, simpler).
- `.distinct()` moved to `sa.select(...)` level (was on column — different semantics).
- Removed hypothetical SQL fallback (dead path).
- Added 2 tests: empty group, dedup when colon-key is also registered.
- Strengthened group isolation test with positive + negative assertions.
- Commit scopes aligned with recent history: `fix(repo)`, `test(repo)`.
- Task 3 `brain_supersede_decision` call shown as flat kwargs, not nested `new_data`.

**Placeholder scan:** no TBD / TODO / "implement later" / vague error handling left.

**Type / argument consistency:**
- All imports present at `pg_project_context.py:16`. No new ones needed.
- `sa.func.split_part` is built into SQLAlchemy's PG dialect.
- `sa.union_all` / `sa.union` / `.subquery()` / `sa.column` / `sa.literal` all standard.

**Rollback:** single branch, 2 commits (test + fix). `git checkout main && git branch -D feat/group-includes-colon-subpartitions` cleans everything.

No gaps detected.
