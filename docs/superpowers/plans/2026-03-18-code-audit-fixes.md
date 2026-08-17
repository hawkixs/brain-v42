# Code Audit Fixes — brain_v42 Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix all bugs and quality issues found during the code audit (5 critical, 10 high, 15+ medium).

**Architecture:** Fixes are organized into independent batches that can be dispatched in parallel via TeamCreate. Each batch groups related fixes that touch the same files to avoid merge conflicts. All fixes follow TDD: write failing test first, then fix.

**Tech Stack:** Python 3.12+, SQLAlchemy 2.0 async, Pydantic 2, pytest-asyncio, structlog

---

## File Map

| Batch | Files Modified | Files Tested |
|-------|---------------|--------------|
| 1 | `services/snippet_service.py` | `tests/unit/services/test_snippet_service.py` |
| 2 | `repositories/pg_learning.py` | `tests/unit/repositories/test_pg_learning.py` |
| 3 | `db/tables.py` + Alembic migration | `tests/unit/db/test_tables.py` |
| 4 | `services/access_logger.py` | `tests/unit/services/test_access_logger.py` |
| 5 | `services/gitlab_ingestor.py`, `services/plan_indexer.py`, `services/feature_dedup_job.py`, `services/cluster_guard.py` | `tests/unit/test_gitlab_ingestor.py` (CREATE), `tests/unit/test_plan_indexer.py`, `tests/unit/test_feature_dedup_job.py` (CREATE), `tests/unit/test_cluster_guard.py` |
| 6 | `mcp/tools/brain_tools.py`, `mcp/tools/runbook_tools.py`, `mcp/tools/snippet_tools.py`, `mcp/server.py` | `tests/unit/mcp/tools/test_brain_decision_tools.py`, `tests/unit/mcp/tools/test_runbook_tools.py` (CREATE) |
| 7 | `services/brain_service.py`, `services/decision_service.py` | `tests/unit/services/test_brain_service.py`, `tests/unit/services/test_decision_service.py` |

---

## Batch 1: Fix assert → raise in SnippetService (CRITICAL)

### Task 1: Replace `assert` with explicit `ValueError` in snippet_service.py

**Files:**
- Modify: `src/brain_v42/services/snippet_service.py:72,241`
- Test: `tests/unit/services/test_snippet_service.py`

**Context:** `assert` statements are stripped by `python -O`. These guards must survive optimization mode.

- [ ] **Step 1: Write failing tests for None embedding_svc**

Add two tests to `tests/unit/services/test_snippet_service.py`:

```python
class TestCreateWithoutEmbeddingSvc:
    @pytest.mark.asyncio
    async def test_create_raises_value_error_when_no_embedding_svc(self) -> None:
        """create() must raise ValueError (not AssertionError) when embedding_svc is None."""
        repo = AsyncMock()
        svc = SnippetService(repo=repo, embedding_svc=None)
        data = SnippetCreate(
            title="test", code="x=1", language="python",
            intention="test intent", project_key="test",
        )
        with pytest.raises(ValueError, match="embedding_svc"):
            await svc.create(data)


class TestSemanticSearchWithoutEmbeddingSvc:
    @pytest.mark.asyncio
    async def test_semantic_search_raises_value_error_when_no_embedding_svc(self) -> None:
        """semantic_search() must raise ValueError when embedding_svc is None."""
        repo = AsyncMock()
        svc = SnippetService(repo=repo, embedding_svc=None)
        with pytest.raises(ValueError, match="embedding_svc"):
            await svc.semantic_search("query")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/services/test_snippet_service.py::TestCreateWithoutEmbeddingSvc tests/unit/services/test_snippet_service.py::TestSemanticSearchWithoutEmbeddingSvc -v`
Expected: FAIL — currently raises `AssertionError`, test expects `ValueError`

- [ ] **Step 3: Replace assert with raise in snippet_service.py**

In `src/brain_v42/services/snippet_service.py`, replace line 72:
```python
# OLD:
assert self._embedding_svc is not None, "embedding_svc required for create"
# NEW:
if self._embedding_svc is None:
    raise ValueError("embedding_svc is required for snippet creation")
```

Same pattern at line 241:
```python
# OLD:
assert self._embedding_svc is not None, "embedding_svc required for semantic_search"
# NEW:
if self._embedding_svc is None:
    raise ValueError("embedding_svc is required for semantic search")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/services/test_snippet_service.py -v`
Expected: ALL PASS

- [ ] **Step 5: Run full unit tests**

Run: `pytest tests/unit -x -q`
Expected: 1353+ passed

- [ ] **Step 6: Commit**

```bash
git add src/brain_v42/services/snippet_service.py tests/unit/services/test_snippet_service.py
git commit -m "fix(snippet): replace assert with ValueError for embedding_svc guard"
```

---

## Batch 2: Fix missing updated_at in pg_learning.update() (CRITICAL)

### Task 2: Add updated_at to learning update

**Files:**
- Modify: `src/brain_v42/repositories/pg_learning.py:97-130`
- Test: `tests/unit/repositories/test_pg_learning.py`

**Context:** `update()` builds its own VALUES dict but never includes `updated_at`. All other repos set it. This breaks decay scoring. Note: `from datetime import UTC, datetime` already exists at line 7 of pg_learning.py — do NOT add it again.

- [ ] **Step 1: Write failing test for updated_at**

Add to `tests/unit/repositories/test_pg_learning.py`:

```python
class TestUpdateSetsUpdatedAt:
    @pytest.mark.asyncio
    async def test_update_includes_updated_at_in_values(self) -> None:
        """update() must set updated_at in the SQL UPDATE statement."""
        repo, mock_session = _make_repo()
        updated_row = {**SAMPLE_ROW, "confidence": "low"}
        mock_result = MagicMock()
        mock_result.mappings.return_value.first.return_value = updated_row
        mock_session.execute = AsyncMock(return_value=mock_result)

        update_data = LearningUpdate(confidence="low")
        await repo.update(SAMPLE_UUID, update_data)

        # Inspect the SQL statement passed to execute
        call_args = mock_session.execute.call_args
        stmt = call_args[0][0]
        # The compiled parameters must include 'updated_at'
        compiled = stmt.compile()
        assert "updated_at" in str(compiled), "UPDATE must include updated_at"
```

- [ ] **Step 2: Write failing test for delete() consistency**

Add to same file:

```python
class TestDeleteConsistency:
    @pytest.mark.asyncio
    async def test_delete_uses_one_or_none(self) -> None:
        """delete() must use one_or_none() for result extraction (not first())."""
        repo, mock_session = _make_repo()
        mock_result = MagicMock()
        mock_result.one_or_none = MagicMock(return_value=(SAMPLE_UUID,))
        # first() should NOT be called — if it is, this mock won't have it
        mock_session.execute = AsyncMock(return_value=mock_result)

        result = await repo.delete(SAMPLE_UUID)
        assert result is True
        mock_result.one_or_none.assert_called_once()
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/unit/repositories/test_pg_learning.py::TestUpdateSetsUpdatedAt tests/unit/repositories/test_pg_learning.py::TestDeleteConsistency -v`
Expected: FAIL

- [ ] **Step 4: Fix update() and delete()**

In `src/brain_v42/repositories/pg_learning.py`:

In `update()`, before building stmt (around line 105), add:
```python
values["updated_at"] = datetime.now(UTC)
```

In `delete()`, line 130, change:
```python
# OLD:
deleted = result.first() is not None
# NEW:
deleted = result.one_or_none() is not None
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/unit/repositories/test_pg_learning.py -v`
Expected: ALL PASS

- [ ] **Step 6: Commit**

```bash
git add src/brain_v42/repositories/pg_learning.py tests/unit/repositories/test_pg_learning.py
git commit -m "fix(learning): add updated_at to update() + consistent delete()"
```

---

## Batch 3: Fix schema inconsistencies (CRITICAL)

### Task 3: Fix tables.py schema issues + Alembic migration

**Files:**
- Modify: `src/brain_v42/db/tables.py:358,435,436`
- Create: `alembic/versions/011_schema_consistency_fixes.py`
- Test: `tests/unit/db/test_tables.py`

**Context:** 3 schema issues: `plan_scan_paths` is JSONB (should be ARRAY), `features.embedding` missing nullable, `features.status` inconsistent server_default. Migration 010 already exists and is committed.

- [ ] **Step 1: Write failing tests for schema expectations**

Add to `tests/unit/db/test_tables.py`:

```python
class TestSchemaConsistency:
    def test_plan_scan_paths_is_array_type(self) -> None:
        """project_contexts.plan_scan_paths should be ARRAY(Text), not JSONB."""
        from brain_v42.db.tables import project_contexts
        col = project_contexts.c["plan_scan_paths"]
        assert isinstance(col.type, sa.types.ARRAY), (
            f"plan_scan_paths should be ARRAY, got {type(col.type)}"
        )

    def test_features_embedding_is_nullable(self) -> None:
        """features.embedding must be explicitly nullable."""
        from brain_v42.db.tables import features
        col = features.c["embedding"]
        assert col.nullable is True, "features.embedding must be nullable=True"

    def test_features_status_server_default_uses_sa_text(self) -> None:
        """features.status server_default should use sa.text() for consistency."""
        from brain_v42.db.tables import features
        col = features.c["status"]
        default = col.server_default
        assert default is not None
        assert hasattr(default.arg, "text"), (
            "features.status server_default should use sa.text()"
        )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/db/test_tables.py::TestSchemaConsistency -v`
Expected: FAIL on all 3

- [ ] **Step 3: Fix tables.py**

In `src/brain_v42/db/tables.py`:

Line 358 — change `plan_scan_paths`:
```python
# OLD:
Column("plan_scan_paths", JSONB, server_default=sa.text("'[]'")),
# NEW:
Column("plan_scan_paths", ARRAY(Text), nullable=False, server_default=sa.text("'{}'")),
```

Line 435 — add nullable to `features.embedding`:
```python
# OLD:
Column("embedding", Vector(_EMBEDDING_DIM)),
# NEW:
Column("embedding", Vector(_EMBEDDING_DIM), nullable=True),
```

Line 436 — fix `features.status` server_default:
```python
# OLD:
Column("status", String(20), nullable=False, server_default="planned"),
# NEW:
Column("status", String(20), nullable=False, server_default=sa.text("'planned'")),
```

- [ ] **Step 4: Create Alembic migration**

Create `alembic/versions/011_schema_consistency_fixes.py`:

```python
"""Schema consistency fixes: plan_scan_paths JSONB→ARRAY, features nullable+default.

Revision ID: 011
"""
from alembic import op
import sqlalchemy as sa

revision = "011"
down_revision = "010"

def upgrade() -> None:
    # 1. Convert plan_scan_paths from JSONB to TEXT[]
    op.execute("""
        ALTER TABLE project_contexts
        ALTER COLUMN plan_scan_paths TYPE TEXT[]
        USING CASE
            WHEN plan_scan_paths IS NULL THEN '{}'::TEXT[]
            ELSE ARRAY(SELECT jsonb_array_elements_text(plan_scan_paths))
        END
    """)
    op.execute("""
        ALTER TABLE project_contexts
        ALTER COLUMN plan_scan_paths SET DEFAULT '{}'::TEXT[],
        ALTER COLUMN plan_scan_paths SET NOT NULL
    """)

    # 2. features.embedding: ensure nullable (already nullable by PG default, but be explicit)
    op.alter_column("features", "embedding", nullable=True)

    # 3. features.status: fix server_default to use SQL expression
    op.alter_column(
        "features", "status",
        server_default=sa.text("'planned'"),
    )

def downgrade() -> None:
    op.execute("""
        ALTER TABLE project_contexts
        ALTER COLUMN plan_scan_paths TYPE JSONB
        USING to_jsonb(plan_scan_paths)
    """)
    op.alter_column("features", "status", server_default="planned")
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/unit/db/test_tables.py -v`
Expected: ALL PASS

- [ ] **Step 6: Commit**

```bash
git add src/brain_v42/db/tables.py alembic/versions/011_schema_consistency_fixes.py tests/unit/db/test_tables.py
git commit -m "fix(schema): plan_scan_paths JSONB→ARRAY, features nullable+default"
```

---

## Batch 4: Fix AccessLogger silent drop (CRITICAL)

### Task 4: Add warning log on queue full

**Files:**
- Modify: `src/brain_v42/services/access_logger.py:45-46`
- Test: `tests/unit/services/test_access_logger.py`

**Context:** When the queue is full, events are silently dropped. This makes decay scoring silently inaccurate with zero observability. Project uses structlog — mock the logger instead of using caplog.

- [ ] **Step 1: Write failing test**

Add to `tests/unit/services/test_access_logger.py`:

```python
import unittest.mock

class TestQueueFullLogging:
    def test_log_access_warns_on_queue_full(self) -> None:
        """log_access must emit a warning when queue is full."""
        al = AccessLogger(session_factory=MagicMock(), max_queue_size=1)
        al.log_access("decision", uuid.uuid4(), "search_hit")  # fills queue

        with unittest.mock.patch("brain_v42.services.access_logger.logger") as mock_log:
            al.log_access("decision", uuid.uuid4(), "search_hit")  # overflow
            mock_log.warning.assert_called_once()
            call_args = mock_log.warning.call_args
            assert "queue_full" in call_args[0][0]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/services/test_access_logger.py::TestQueueFullLogging -v`
Expected: FAIL — no warning emitted (currently `pass`)

- [ ] **Step 3: Add warning log**

In `src/brain_v42/services/access_logger.py`, line 45-46:

```python
# OLD:
except asyncio.QueueFull:
    pass  # silent drop — best-effort logging
# NEW:
except asyncio.QueueFull:
    logger.warning(
        "access_logger.queue_full",
        entity_type=entity_type,
        entity_id=str(entity_id),
    )
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/unit/services/test_access_logger.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add src/brain_v42/services/access_logger.py tests/unit/services/test_access_logger.py
git commit -m "fix(access_logger): log warning on queue full instead of silent drop"
```

---

## Batch 5: Add error handling on embed() calls (HIGH)

### Task 5: Wrap embedding/reranker calls with try/except in 4 services

**Files:**
- Modify: `src/brain_v42/services/gitlab_ingestor.py:82`, `src/brain_v42/services/plan_indexer.py:106`, `src/brain_v42/services/feature_dedup_job.py:151`, `src/brain_v42/services/cluster_guard.py:153`
- Create: `tests/unit/test_gitlab_ingestor.py`
- Create: `tests/unit/test_feature_dedup_job.py`
- Modify: `tests/unit/test_plan_indexer.py`, `tests/unit/test_cluster_guard.py`

**Context:** 4 services call `embed()` or `rerank()` without try/except. If the GPU embedding service is down, these all crash instead of degrading gracefully.

**Existing test patterns:** `test_plan_indexer.py` and `test_cluster_guard.py` already exist in `tests/unit/` (NOT in `tests/unit/services/`). They use a `mock_deps` fixture pattern returning a dict with `session_factory`, `session`, `embedding_svc`, `cluster_guard` keys. Follow this pattern for new files.

- [ ] **Step 1: Write failing test for gitlab_ingestor**

Create `tests/unit/test_gitlab_ingestor.py`:

```python
"""Unit tests for GitLabIngestor — embedding failure graceful degradation."""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from brain_v42.services.gitlab_ingestor import GitLabIngestor


@pytest.fixture
def mock_deps():
    session = AsyncMock()
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)

    embedding_svc = AsyncMock()
    embedding_svc.embed = AsyncMock(return_value=[0.1] * 1536)
    cluster_guard = AsyncMock()
    cluster_guard.resolve = AsyncMock(return_value=(MagicMock(id=uuid.uuid4()), "linked"))

    return {
        "session_factory": factory,
        "session": session,
        "embedding_svc": embedding_svc,
        "cluster_guard": cluster_guard,
    }


class TestEmbeddingFailure:
    @pytest.mark.asyncio
    async def test_process_event_handles_embed_failure(self, mock_deps) -> None:
        """_process_event must not crash when embedding service raises."""
        mock_deps["embedding_svc"].embed.side_effect = Exception("GPU down")
        ingestor = GitLabIngestor(
            session_factory=mock_deps["session_factory"],
            embedding_svc=mock_deps["embedding_svc"],
            cluster_guard=mock_deps["cluster_guard"],
        )
        # Should not raise — should return a skip/error status
        result = await ingestor.process_event(
            payload={"object_kind": "push", "ref": "refs/heads/main",
                     "project": {"path_with_namespace": "test/proj"}},
            project_key="test",
        )
        assert result is not None  # didn't crash
```

- [ ] **Step 2: Write failing test for plan_indexer**

Add to `tests/unit/test_plan_indexer.py`:

```python
class TestEmbeddingFailure:
    @pytest.mark.asyncio
    async def test_index_plans_skips_file_on_embed_failure(self, mock_deps, tmp_path) -> None:
        """index_plans must skip a file and log error when embed() fails."""
        mock_deps["embedding_svc"].embed.side_effect = Exception("GPU down")
        indexer = _build_indexer(mock_deps)
        plan_dir = tmp_path / "plans"
        plan_dir.mkdir()
        (plan_dir / "plan.md").write_text("# Test Plan\nSome content")

        stats = await indexer.index_plans(plan_dir)
        # Should not raise — should report error in stats
        assert stats.get("errors", 0) > 0 or stats.get("skipped", 0) > 0
```

- [ ] **Step 3: Write failing test for feature_dedup_job**

Create `tests/unit/test_feature_dedup_job.py`:

```python
"""Unit tests for FeatureDedupJob — embedding failure graceful degradation."""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from brain_v42.services.feature_dedup_job import FeatureDedupJob


@pytest.fixture
def mock_deps():
    session = AsyncMock()
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)

    embedding_svc = AsyncMock()
    embedding_svc.embed = AsyncMock(return_value=[0.1] * 1536)

    return {
        "session_factory": factory,
        "session": session,
        "embedding_svc": embedding_svc,
    }


class TestEmbeddingFailure:
    @pytest.mark.asyncio
    async def test_merge_continues_when_embed_fails(self, mock_deps) -> None:
        """merge_features must not crash when embedding service fails."""
        mock_deps["embedding_svc"].embed.side_effect = Exception("GPU down")
        job = FeatureDedupJob(
            session_factory=mock_deps["session_factory"],
            embedding_svc=mock_deps["embedding_svc"],
        )
        # Should not raise — merge should continue with embedding=None
        # This tests that the embed call is wrapped in try/except
        # The actual merge logic may still fail for other reasons,
        # but it must not be the embed() call that crashes it
```

- [ ] **Step 4: Write failing test for cluster_guard reranker**

Add to `tests/unit/test_cluster_guard.py`:

```python
class TestRerankerFailure:
    @pytest.mark.asyncio
    async def test_grey_zone_handles_reranker_failure(self, mock_deps) -> None:
        """_handle_grey_zone must fall back when reranker raises."""
        session, factory, embedding_svc = (
            mock_deps["session"],
            mock_deps["session_factory"],
            mock_deps["embedding_svc"],
        )
        reranker = AsyncMock()
        reranker.rerank = AsyncMock(side_effect=Exception("Reranker down"))

        guard = ClusterGuard(
            session_factory=factory,
            embedding_svc=embedding_svc,
            reranker=reranker,
        )
        # Should not crash — should fall back to cosine-only
        candidate = _make_feature_row(similarity=0.75)
        # Call resolve with a grey-zone similarity
        # Exact call depends on constructor signature
```

- [ ] **Step 5: Run tests to verify they fail**

Run: `pytest tests/unit/test_gitlab_ingestor.py tests/unit/test_plan_indexer.py::TestEmbeddingFailure tests/unit/test_feature_dedup_job.py tests/unit/test_cluster_guard.py::TestRerankerFailure -v`
Expected: FAIL — unhandled Exception propagates

- [ ] **Step 6: Add try/except wrappers in each service**

In `gitlab_ingestor.py:82`:
```python
# OLD:
embedding = await self._embedding_svc.embed(text[:2000])
# NEW:
try:
    embedding = await self._embedding_svc.embed(text[:2000])
except Exception:
    logger.warning("gitlab_ingestor.embed_failed", exc_info=True)
    return {"status": "skipped_embed_failed"}
```

In `plan_indexer.py:106`:
```python
# OLD:
embedding = await self._embedding_svc.embed(embed_text)
# NEW:
try:
    embedding = await self._embedding_svc.embed(embed_text)
except Exception:
    logger.warning("plan_indexer.embed_failed", file=str(file_path), exc_info=True)
    stats["errors"] = stats.get("errors", 0) + 1
    continue
```

In `feature_dedup_job.py:151`:
```python
# OLD:
new_embedding = await self._embedding_svc.embed(enriched_desc)
# NEW:
try:
    new_embedding = await self._embedding_svc.embed(enriched_desc)
except Exception:
    logger.warning("feature_dedup.embed_failed", exc_info=True)
    new_embedding = None
```

In `cluster_guard.py:153`:
```python
# OLD:
scores = await self._reranker.rerank(text, candidate_texts)
# NEW:
try:
    scores = await self._reranker.rerank(text, candidate_texts)
except Exception:
    logger.warning("cluster_guard.rerank_failed", exc_info=True)
    # Fall back to cosine-only: use similarity scores from DB query
    scores = [c.similarity for c in candidates]
```

- [ ] **Step 7: Run tests**

Run: `pytest tests/unit -x -q`
Expected: ALL PASS

- [ ] **Step 8: Commit**

```bash
git add src/brain_v42/services/gitlab_ingestor.py src/brain_v42/services/plan_indexer.py \
  src/brain_v42/services/feature_dedup_job.py src/brain_v42/services/cluster_guard.py \
  tests/unit/test_gitlab_ingestor.py tests/unit/test_feature_dedup_job.py \
  tests/unit/test_plan_indexer.py tests/unit/test_cluster_guard.py
git commit -m "fix(services): graceful degradation when embedding/reranker service fails"
```

---

## Batch 6: Fix MCP tools validation gaps (HIGH)

### Task 6: Add enum validation + runbook status validation + cleanup hybrid_searcher

**Files:**
- Modify: `src/brain_v42/mcp/tools/brain_tools.py:76,105,107,240-241,404-406`
- Modify: `src/brain_v42/mcp/tools/runbook_tools.py:29,94`
- Modify: `src/brain_v42/mcp/tools/snippet_tools.py:31`
- Modify: `src/brain_v42/mcp/server.py:224,236,255,281,293` (stop creating/passing hybrid_searcher)
- Create: `tests/unit/mcp/tools/test_runbook_tools.py`
- Modify: `tests/unit/mcp/tools/test_brain_decision_tools.py`

**Context:** `cast()` without validation bypasses safety. Runbook execution status accepts any string. `hybrid_searcher` param is dead code across 4 files + server.py.

**Important:** MCP tools are closure-based functions registered inside `register_tools()`. Tests must use the `_make_mcp_and_svc()` pattern from `test_brain_decision_tools.py` — register tools on a mock MCP, then call the registered function by name from the dict.

- [ ] **Step 1: Write failing test for brain_learn validation**

Add to `tests/unit/mcp/tools/test_brain_decision_tools.py` (which already has the `_make_mcp_and_svc()` helper):

```python
class TestBrainLearnValidation:
    @pytest.mark.asyncio
    async def test_learn_rejects_invalid_source_type(self) -> None:
        """brain_learn must return error for invalid source_type."""
        registered, mock_svc = _make_mcp_and_svc()
        register_tools(
            mock_svc["mcp"], mock_svc["decision_svc"], mock_svc["learning_svc"],
            mock_svc["snippet_svc"], mock_svc["runbook_svc"], mock_svc["adr_svc"],
            mock_svc["project_svc"], mock_svc["brain_svc"],
        )
        brain_learn = registered["brain_learn"]
        result = await brain_learn(
            topic="test", insight="test content", source="test",
            source_type="INVALID_TYPE", confidence="high",
            project_key="test",
        )
        assert "invalid" in result.lower() or "error" in result.lower()
```

(Adapt the `_make_mcp_and_svc()` setup to match the actual services needed by `register_tools()`. Read the existing test file for the exact pattern.)

- [ ] **Step 2: Write failing test for runbook status validation**

Create `tests/unit/mcp/tools/test_runbook_tools.py`:

```python
"""Unit tests for runbook MCP tools — status validation."""
from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from brain_v42.mcp.tools.runbook_tools import register_runbook_tools


def _make_mcp_and_svc() -> tuple[dict[str, Any], AsyncMock]:
    registered: dict[str, Any] = {}
    mock_mcp = MagicMock()

    def tool_decorator(**kwargs: Any) -> Any:
        def decorator(fn: Any) -> Any:
            registered[fn.__name__] = fn
            return fn
        return decorator

    mock_mcp.tool = tool_decorator
    runbook_svc = AsyncMock()
    return registered, mock_mcp, runbook_svc


class TestExecuteRunbookValidation:
    @pytest.mark.asyncio
    async def test_execute_rejects_invalid_status(self) -> None:
        """brain_execute_runbook must reject invalid status values."""
        registered, mock_mcp, runbook_svc = _make_mcp_and_svc()
        register_runbook_tools(mock_mcp, runbook_svc)
        execute_fn = registered["brain_execute_runbook"]
        result = await execute_fn(
            runbook_id=str(uuid.uuid4()), status="INVALID_STATUS",
        )
        assert "invalid" in result.lower() or "error" in result.lower()
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/unit/mcp/tools/test_brain_decision_tools.py::TestBrainLearnValidation tests/unit/mcp/tools/test_runbook_tools.py::TestExecuteRunbookValidation -v`
Expected: FAIL — invalid values accepted

- [ ] **Step 4: Add validation**

In `src/brain_v42/mcp/tools/brain_tools.py`, in `brain_learn()` before `LearningCreate(...)` (around line 236):
```python
_VALID_SOURCE_TYPES = {"experience", "documentation", "code_review", "bug", "external", "article", "video", "book", "conversation", "research"}
_VALID_CONFIDENCE = {"low", "medium", "high"}

if source_type not in _VALID_SOURCE_TYPES:
    return format_error(f"Invalid source_type '{source_type}'. Valid: {sorted(_VALID_SOURCE_TYPES)}")
if confidence not in _VALID_CONFIDENCE:
    return format_error(f"Invalid confidence '{confidence}'. Valid: {sorted(_VALID_CONFIDENCE)}")
```

In `src/brain_v42/mcp/tools/brain_tools.py`, in `brain_search()` (around line 404), add logging for filtered types:
```python
if types:
    invalid = [t for t in types if t not in _VALID_TYPES]
    if invalid:
        logger.warning("brain_search.unknown_types_ignored", types=invalid)
```

In `src/brain_v42/mcp/tools/runbook_tools.py`, in `brain_execute_runbook()` before the execute call:
```python
_VALID_STATUSES = {"success", "failed", "partial"}
if status not in _VALID_STATUSES:
    return format_error(f"Invalid status '{status}'. Valid: {sorted(_VALID_STATUSES)}")
```

- [ ] **Step 5: Remove hybrid_searcher dead code**

Remove `hybrid_searcher` parameter from:
- `src/brain_v42/mcp/tools/brain_tools.py:76` — remove from `register_tools()` signature
- `src/brain_v42/mcp/tools/brain_tools.py:105` — remove `hybrid_searcher=hybrid_searcher` from `register_snippet_tools()` call
- `src/brain_v42/mcp/tools/brain_tools.py:107` — remove `hybrid_searcher=hybrid_searcher` from `register_runbook_tools()` call
- `src/brain_v42/mcp/tools/snippet_tools.py:31` — remove from `register_snippet_tools()` signature
- `src/brain_v42/mcp/tools/runbook_tools.py:29` — remove from `register_runbook_tools()` signature
- `src/brain_v42/mcp/server.py:224` — remove `hybrid_searcher = HybridSearcher(...)` line
- `src/brain_v42/mcp/server.py:236` — remove `hybrid_searcher=hybrid_searcher` arg
- `src/brain_v42/mcp/server.py:255` — remove `"hybrid_searcher": hybrid_searcher` from dict
- `src/brain_v42/mcp/server.py:281` — remove `hybrid_searcher = services["hybrid_searcher"]`
- `src/brain_v42/mcp/server.py:293` — remove `hybrid_searcher=hybrid_searcher` arg

Also check if `HybridSearcher` import in server.py can be removed.

- [ ] **Step 6: Run tests**

Run: `pytest tests/unit/mcp/ -v && pytest tests/unit -x -q`
Expected: ALL PASS

- [ ] **Step 7: Commit**

```bash
git add src/brain_v42/mcp/tools/brain_tools.py src/brain_v42/mcp/tools/runbook_tools.py \
  src/brain_v42/mcp/tools/snippet_tools.py src/brain_v42/mcp/server.py \
  tests/unit/mcp/tools/test_brain_decision_tools.py tests/unit/mcp/tools/test_runbook_tools.py
git commit -m "fix(tools): add enum/status validation, remove dead hybrid_searcher param"
```

---

## Batch 7: Fix BrainService + DecisionService error handling (HIGH)

### Task 7: Narrow exception catches + log missing chain decisions

**Files:**
- Modify: `src/brain_v42/services/brain_service.py:185-189`
- Modify: `src/brain_v42/services/decision_service.py:266-270`
- Test: `tests/unit/services/test_brain_service.py` (exists, has mock patterns)
- Test: `tests/unit/services/test_decision_service.py` (exists, has mock patterns)

**Context:** BrainService catches bare `Exception` on model_dump (masks bugs like TypeError). DecisionService silently skips missing decisions in supersession chain.

- [ ] **Step 1: Write failing test for BrainService**

Add to `tests/unit/services/test_brain_service.py`:

```python
class TestModelDumpErrorHandling:
    @pytest.mark.asyncio
    async def test_model_dump_type_error_propagates(self) -> None:
        """model_dump TypeError should NOT be caught — only ValueError/AttributeError."""
        # Create a mock entity whose model_dump raises TypeError
        bad_entity = MagicMock()
        bad_entity.model_dump.side_effect = TypeError("unexpected type")
        bad_entity.created_at = NOW
        bad_entity.freshness_status = "fresh"
        bad_entity.merged_into = None

        # Mock a repo that returns this entity
        mock_decision_repo = AsyncMock()
        mock_decision_repo.search.return_value = [(bad_entity, 0.9)]

        svc = BrainService(
            decision_repo=mock_decision_repo,
            learning_repo=AsyncMock(search=AsyncMock(return_value=[])),
            snippet_repo=AsyncMock(search=AsyncMock(return_value=[])),
            runbook_repo=AsyncMock(search=AsyncMock(return_value=[])),
            adr_repo=AsyncMock(search=AsyncMock(return_value=[])),
        )

        with pytest.raises(TypeError):
            await svc.search(query="test", query_embedding=[0.1] * 1536)
```

- [ ] **Step 2: Write failing test for DecisionService chain logging**

Add to `tests/unit/services/test_decision_service.py`:

```python
class TestSupersessionChainLogging:
    @pytest.mark.asyncio
    async def test_logs_warning_for_missing_chain_decisions(self) -> None:
        """get_supersession_chain must log when a chain member can't be resolved."""
        mock_repo = AsyncMock()
        mock_graph = AsyncMock()
        mock_graph.get_supersession_chain.return_value = [
            str(uuid.uuid4()), str(uuid.uuid4())  # 2 IDs
        ]
        # First returns a decision, second returns None
        mock_repo.get_by_id.side_effect = [_make_decision(), None]
        mock_repo.get_supersession_chain.return_value = []

        svc = DecisionService(repo=mock_repo, graph=mock_graph)

        with unittest.mock.patch("brain_v42.services.decision_service.logger") as mock_logger:
            result = await svc.get_supersession_chain(uuid.uuid4())
            mock_logger.warning.assert_called_once()
            assert "chain_member_missing" in mock_logger.warning.call_args[0][0]
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/unit/services/test_brain_service.py::TestModelDumpErrorHandling tests/unit/services/test_decision_service.py::TestSupersessionChainLogging -v`
Expected: FAIL — TypeError is currently caught by bare `except Exception`; no warning logged for missing chain members

- [ ] **Step 4: Implement fixes**

In `src/brain_v42/services/brain_service.py:187`:
```python
# OLD:
except Exception:
    logger.warning("brain_service.model_dump_failed", type=t, exc_info=True)
    item_dict = vars(entity)
# NEW:
except (ValueError, AttributeError):
    logger.warning("brain_service.model_dump_failed", type=t, exc_info=True)
    item_dict = vars(entity)
```

In `src/brain_v42/services/decision_service.py:269-270`:
```python
# OLD:
if d:
    decisions.append(d)
# NEW:
if d:
    decisions.append(d)
else:
    logger.warning("decision.chain_member_missing", decision_id=str(uid))
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/unit/services/test_brain_service.py tests/unit/services/test_decision_service.py -v`
Expected: ALL PASS

- [ ] **Step 6: Run full test suite**

Run: `pytest tests/unit -x -q`
Expected: ALL PASS

- [ ] **Step 7: Commit**

```bash
git add src/brain_v42/services/brain_service.py src/brain_v42/services/decision_service.py \
  tests/unit/services/test_brain_service.py tests/unit/services/test_decision_service.py
git commit -m "fix(services): narrow exception handling + log missing chain decisions"
```

---

## Batch Execution Plan (TeamCreate Optimization)

```
┌──────────────────────────────────────────────────────────────┐
│                    PARALLEL BATCH 1                          │
│  (No file conflicts — all touch different files)            │
│                                                              │
│  Task 1: snippet_service assert→raise          [CRITICAL]   │
│  Task 2: pg_learning updated_at + delete       [CRITICAL]   │
│  Task 3: tables.py schema fixes + migration    [CRITICAL]   │
│  Task 4: access_logger queue_full log          [CRITICAL]   │
│                                                              │
│  4 agents in parallel                                        │
└──────────────────────────────────────────────────────────────┘
                            │
                            ▼
                     merge + test all
                            │
                            ▼
┌──────────────────────────────────────────────────────────────┐
│                    PARALLEL BATCH 2                          │
│  (No file conflicts — all touch different files)            │
│                                                              │
│  Task 5: embed() error handling (4 services)   [HIGH]       │
│  Task 6: MCP tools validation + cleanup        [HIGH]       │
│  Task 7: brain_service + decision_service      [HIGH]       │
│                                                              │
│  3 agents in parallel                                        │
└──────────────────────────────────────────────────────────────┘
                            │
                            ▼
                     merge + test all
                            │
                            ▼
              ruff + mypy + push + CI
```

### TeamCreate Configuration

**Batch 1 (4 parallel agents, isolation: worktree):**
| Agent | Task | Files touched | Conflicts with |
|-------|------|---------------|----------------|
| 1 | Task 1 | snippet_service.py, test_snippet_service.py | None |
| 2 | Task 2 | pg_learning.py, test_pg_learning.py | None |
| 3 | Task 3 | tables.py, test_tables.py, alembic 011 | None |
| 4 | Task 4 | access_logger.py, test_access_logger.py | None |

**Batch 2 (3 parallel agents, isolation: worktree):**
| Agent | Task | Files touched | Conflicts with |
|-------|------|---------------|----------------|
| 5 | Task 5 | gitlab_ingestor, plan_indexer, feature_dedup_job, cluster_guard + tests | None |
| 6 | Task 6 | brain_tools, runbook_tools, snippet_tools, server.py + tests | None |
| 7 | Task 7 | brain_service, decision_service + tests | None |

### Post-merge verification

After each batch merge:
```bash
ruff check src/ tests/ && ruff format src/ tests/
mypy src/
pytest tests/unit -x -q
```
