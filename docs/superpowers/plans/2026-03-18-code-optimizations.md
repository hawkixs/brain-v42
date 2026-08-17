# Code Optimizations — brain_v42 Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Apply 15 validated optimizations from the code audit debate — eliminating 5x redundant GPU calls, N+1 queries, ~105 lines of duplication, dead code, and a data loss bug.

**Architecture:** Fixes organized in 3 parallel batches by file ownership. No file conflicts between tasks in the same batch. Each task follows TDD. Counter-proposal recommendations applied (standalone functions over mixins, simplified embed fix).

**Tech Stack:** Python 3.12+, SQLAlchemy 2.0 async, asyncpg, httpx, pytest-asyncio, structlog

**Test command:** `source .venv/bin/activate && pytest tests/unit -x -q`

**Lint command:** `source .venv/bin/activate && ruff check src/ tests/ && ruff format src/ tests/`

---

## File Map

| Batch | Task | Files Modified | Files Tested |
|-------|------|---------------|--------------|
| 1 | T1: Embed once | 6 service files + brain_service | Existing tests (signature backward-compat) |
| 1 | T2: GPU retry | gpu_embedding_service.py | test_gpu_embedding_service.py |
| 1 | T3: Quick wins (7) | 7 files (server, pg_adr, pg_runbook, formatters, brain_service, plan_indexer, pg_base) | Various existing test files |
| 2 | T4: Graph helpers | graph_service.py + 5 service files | test_graph_service.py + existing service tests |
| 2 | T5: Decision _row_to_model | pg_decision.py | test_pg_decision.py |
| 2 | T6: Decay batch + column prune | decay_flusher.py | test_decay_flusher.py |
| 3 | T7: Access log snapshot fix | pg_access_log.py | test_pg_access_log.py |
| 3 | T8: Runbook model_dump | pg_runbook.py | test_pg_runbook.py |

---

## Batch 1: Performance + Quick Wins (4 agents, no conflicts)

### Task 1: Embed query once in brain_search (HIGH — 5x GPU reduction)

**Files:**
- Modify: `src/brain_v42/services/brain_service.py` — pre-compute embedding in `_fan_out()`, fix `ensure_future` → `create_task`
- Modify: `src/brain_v42/services/decision_service.py` — add `embedding` kwarg to `semantic_search()`
- Modify: `src/brain_v42/services/learning_service.py` — same
- Modify: `src/brain_v42/services/snippet_service.py` — same
- Modify: `src/brain_v42/services/runbook_service.py` — same
- Modify: `src/brain_v42/services/adr_service.py` — same
- Modify: `src/brain_v42/services/search/hybrid.py` — add `embedding` param, pass to `vector_search_fn`

**Context:** `BrainService._fan_out()` calls each service's `semantic_search(query, ...)` which independently calls `embed(query)`. Same query embedded 5 times = 5 HTTP calls to GPU. The docstring at `brain_service.py:10-13` describes the intended fix but it was never implemented.

- [ ] **Step 1: Write test asserting embed called once**

Add to `tests/unit/services/test_brain_service.py`:

```python
class TestEmbedCalledOnce:
    @pytest.mark.asyncio
    async def test_fan_out_embeds_query_once(self) -> None:
        """_fan_out must call embedding_svc.embed() exactly once, not per-service."""
        # Build BrainService with mock embedding_svc
        mock_embedding_svc = AsyncMock()
        mock_embedding_svc.embed = AsyncMock(return_value=[0.1] * 1536)

        # Mock all 5 repos with empty search results
        mock_repos = {}
        for t in ["decision", "learning", "snippet", "runbook", "adr"]:
            repo = AsyncMock()
            repo.search_vector = AsyncMock(return_value=[])
            repo.search = AsyncMock(return_value=[])
            mock_repos[t] = repo

        # Build mock services that accept embedding kwarg
        mock_services = {}
        for t in ["decision", "learning", "snippet", "runbook", "adr"]:
            svc = AsyncMock()
            svc.semantic_search = AsyncMock(return_value=[])
            mock_services[t] = svc

        svc = BrainService(
            decision_repo=mock_repos["decision"],
            learning_repo=mock_repos["learning"],
            snippet_repo=mock_repos["snippet"],
            runbook_repo=mock_repos["runbook"],
            adr_repo=mock_repos["adr"],
            embedding_svc=mock_embedding_svc,
        )
        # Override the internal services dict
        svc._services = mock_services

        await svc.search(query="test query", query_embedding=[0.1] * 1536)

        # embed() should be called exactly once (by _fan_out), not 5 times
        mock_embedding_svc.embed.assert_awaited_once()
```

NOTE: `BrainService.__init__` takes `decision_svc=`, `learning_svc=`, `snippet_svc=`, `runbook_svc=`, `adr_svc=`, `embedding_svc=`, `hybrid_searcher=` (all `Any`). The `_services` dict maps type names to these service instances. Read `tests/unit/services/test_brain_service.py` for existing mock setup patterns.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/services/test_brain_service.py::TestEmbedCalledOnce -v`
Expected: FAIL — embed is currently called 5 times (once per service)

- [ ] **Step 3: Add `embedding` kwarg to each service's `semantic_search()` AND HybridSearcher**

For each of the 5 services, same 2-line change pattern. Example for `decision_service.py`:

```python
# BEFORE:
async def semantic_search(self, query: str, *, project_key=None, limit=20):
    embedding = await self._embedding_svc.embed(query)
    return await self._repo.search_vector(embedding, ...)

# AFTER:
async def semantic_search(self, query: str, *, project_key=None, limit=20, embedding: list[float] | None = None):
    if embedding is None:
        embedding = await self._embedding_svc.embed(query)
    return await self._repo.search_vector(embedding, ...)
```

For `snippet_service.py`, also update the ValueError guard:
```python
# BEFORE:
if self._embedding_svc is None:
    raise ValueError("embedding_svc is required for semantic search")
# AFTER:
if embedding is None and self._embedding_svc is None:
    raise ValueError("embedding_svc is required for semantic search")
```

- [ ] **Step 4: Pre-compute embedding in `_fan_out()`**

In `brain_service.py`, at the start of `_fan_out()`:

```python
# Pre-compute embedding ONCE for all services
shared_embedding: list[float] | None = None
if self._embedding_svc is not None:
    shared_embedding = await self._embedding_svc.embed(query)
```

Then in the loop building coroutines, pass it to BOTH paths:
```python
if self._hybrid_searcher:
    coro = self._hybrid_searcher.search(
        query=query,
        fts_search_fn=svc.search,
        vector_search_fn=svc.semantic_search,
        text_extractor=_TEXT_EXTRACTORS[t],
        limit=limit,
        project_key=project_key,
        embedding=shared_embedding,  # NEW — pass pre-computed embedding
    )
else:
    coro = svc.semantic_search(query, project_key=project_key, limit=limit, embedding=shared_embedding)
tasks.append(asyncio.create_task(coro))  # ALSO: ensure_future → create_task
```

Also modify `src/brain_v42/services/search/hybrid.py` to accept and forward the embedding:
```python
async def search(self, query, fts_search_fn, vector_search_fn, text_extractor,
                 limit=10, project_key=None, embedding=None):  # ADD embedding param
    ...
    fts_raw, vec_raw = await asyncio.gather(
        fts_search_fn(query=query, limit=50, **common_kwargs),
        vector_search_fn(query=query, limit=50, embedding=embedding, **common_kwargs),  # PASS embedding
    )
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/unit/services/ -x -q`
Expected: ALL PASS (new kwarg is backward-compat)

- [ ] **Step 6: Commit**

```bash
git add src/brain_v42/services/brain_service.py src/brain_v42/services/decision_service.py \
  src/brain_v42/services/learning_service.py src/brain_v42/services/snippet_service.py \
  src/brain_v42/services/runbook_service.py src/brain_v42/services/adr_service.py \
  tests/unit/services/test_brain_service.py
git commit -m "perf(search): embed query once in brain_search instead of 5x GPU calls"
```

---

### Task 2: Fix GPU embedding retry (MED — TimeoutException + 5xx)

**Files:**
- Modify: `src/brain_v42/services/gpu_embedding_service.py:109`
- Test: `tests/unit/services/test_gpu_embedding_service.py`

**Context:** `_request_with_retry` only catches `httpx.ConnectError`. `TimeoutException` and HTTP 5xx propagate immediately without retry.

- [ ] **Step 1: Write failing tests**

Add to `tests/unit/services/test_gpu_embedding_service.py`:

```python
class TestRetryOnTimeout:
    @pytest.mark.asyncio
    async def test_retries_on_timeout(self) -> None:
        """embed() must retry on TimeoutException, not fail immediately."""
        # Mock httpx client that raises TimeoutException once then succeeds
        # Verify embed() succeeds after retry

class TestRetryOn5xx:
    @pytest.mark.asyncio
    async def test_retries_on_server_error(self) -> None:
        """embed() must retry on HTTP 5xx, not fail immediately."""
        # Mock httpx client that returns 500 once then 200
        # Verify embed() succeeds after retry

    @pytest.mark.asyncio
    async def test_no_retry_on_4xx(self) -> None:
        """embed() must NOT retry on HTTP 4xx (client errors)."""
        # Mock httpx client that returns 400
        # Verify HTTPStatusError propagates immediately
```

- [ ] **Step 2: Run tests to verify they fail**

- [ ] **Step 3: Extend retry clause**

In `gpu_embedding_service.py`, the except clause around line 109:

```python
# BEFORE:
except httpx.ConnectError as exc:
    last_error = exc
    ...

# AFTER:
except (httpx.ConnectError, httpx.TimeoutException) as exc:
    last_error = exc
    if attempt < self._max_retries:
        backoff = 0.5 * (2 ** attempt)
        logger.warning("gpu_embedding_service.retry", attempt=attempt + 1, ...)
        await asyncio.sleep(backoff)
except httpx.HTTPStatusError as exc:
    last_error = exc
    if exc.response.status_code >= 500 and attempt < self._max_retries:
        backoff = 0.5 * (2 ** attempt)
        logger.warning("gpu_embedding_service.retry", attempt=attempt + 1, ...)
        await asyncio.sleep(backoff)
    else:
        raise  # 4xx — propagate immediately
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/unit/services/test_gpu_embedding_service.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add src/brain_v42/services/gpu_embedding_service.py tests/unit/services/test_gpu_embedding_service.py
git commit -m "fix(gpu): retry on TimeoutException and HTTP 5xx, not just ConnectError"
```

---

### Task 3: Quick wins batch (7 trivial fixes)

**Files:**
- Modify: `src/brain_v42/mcp/server.py` — remove dead `needs_async` branch
- Modify: `src/brain_v42/repositories/pg_adr.py` — deduplicate `plainto_tsquery`
- Modify: `src/brain_v42/mcp/tools/formatters.py` — fix `short_id` docstring
- Modify: `src/brain_v42/services/plan_indexer.py` — wrap `read_text()` with UnicodeDecodeError handler
- NOTE: `ensure_future → create_task` in brain_service.py is handled by T1 (do NOT touch brain_service.py here)
- Modify: `src/brain_v42/repositories/pg_base.py` — add `skip_count` param to `list_all()`
- Modify: `src/brain_v42/repositories/pg_runbook.py` — use `model_dump` in `_runbook_update_to_dict`

**Context:** All validated LOW-effort fixes. No test changes expected except pg_base (skip_count callers).

- [ ] **Step 1: Apply all 7 fixes**

Read each file, apply the exact BEFORE/AFTER from the quick wins batch:

1. **server.py**: Remove `needs_async = True`, the `if needs_async:` guard, and the dead `else:` block. De-indent the async path.

2. **pg_adr.py:308-321**: Lift `tsquery = sa.func.plainto_tsquery("english", query)` to a single variable, reuse for both WHERE and ORDER BY.

3. **formatters.py:29-31**: Fix docstring to `"""Return the full UUID string (LLMs need complete IDs for tool chaining)."""`

4. **brain_service.py**: SKIP — `ensure_future → create_task` handled by T1.

5. **plan_indexer.py:93**: Wrap `read_text()`:
```python
try:
    content = file_path.read_text(encoding="utf-8")
except UnicodeDecodeError:
    logger.warning("plan_indexer.decode_failed", file=str(file_path))
    stats["errors"] = stats.get("errors", 0) + 1
    continue
```

6. **pg_base.py:list_all()**: Add `skip_count: bool = False` param. If True, set `total = 0` and skip the COUNT query. Update callers in `pg_runbook.py` and `pg_snippet.py` to pass `skip_count=True`.

7. **pg_runbook.py:76-99**: Replace manual field checks with `model_dump(exclude_none=True)` + special-case for `steps`/`rollback_steps`.

- [ ] **Step 2: Run ruff format on all modified files**

- [ ] **Step 3: Run full tests**

Run: `pytest tests/unit -x -q`

- [ ] **Step 4: Commit**

```bash
git add src/brain_v42/mcp/server.py src/brain_v42/repositories/pg_adr.py \
  src/brain_v42/mcp/tools/formatters.py src/brain_v42/services/brain_service.py \
  src/brain_v42/services/plan_indexer.py src/brain_v42/repositories/pg_base.py \
  src/brain_v42/repositories/pg_runbook.py src/brain_v42/repositories/pg_snippet.py
git commit -m "refactor: 7 quick wins — dead code, model_dump, skip_count, retry guard"
```

---

## Batch 2: Architecture + Decay (3 agents, no conflicts)

### Task 4: Extract graph helper functions (MED — 105 lines dedup)

**Files:**
- Create: `src/brain_v42/services/graph_helpers.py`
- Modify: `src/brain_v42/services/decision_service.py` — replace inline graph blocks
- Modify: `src/brain_v42/services/learning_service.py` — same
- Modify: `src/brain_v42/services/snippet_service.py` — same
- Modify: `src/brain_v42/services/runbook_service.py` — same
- Modify: `src/brain_v42/services/adr_service.py` — same
- Create: `tests/unit/services/test_graph_helpers.py`

**Context:** Graph write-through try/except + feature_linker.link_artifact blocks are copy-pasted across 5 services (10 create blocks + 5 delete blocks = 105 lines). Counter-proposal: standalone async functions, NOT a mixin.

- [ ] **Step 1: Write tests for helper functions**

Create `tests/unit/services/test_graph_helpers.py`:

```python
"""Tests for graph_helpers — graph_upsert_entity and graph_delete_entity."""
from __future__ import annotations
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4
import pytest

from brain_v42.services.graph_helpers import graph_upsert_entity, graph_delete_entity, link_artifact_if_enabled


class TestGraphUpsertEntity:
    @pytest.mark.asyncio
    async def test_upsert_calls_graph_methods(self) -> None:
        graph = AsyncMock()
        eid = uuid4()
        await graph_upsert_entity(graph, "Decision", eid, {"title": "t"}, project_key="pk")
        graph.upsert_node.assert_awaited_once()
        graph.link_to_project.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_upsert_skips_when_graph_is_none(self) -> None:
        await graph_upsert_entity(None, "Decision", uuid4(), {})  # must not raise

    @pytest.mark.asyncio
    async def test_upsert_catches_exceptions(self) -> None:
        graph = AsyncMock()
        graph.upsert_node.side_effect = Exception("Neo4j down")
        await graph_upsert_entity(graph, "Decision", uuid4(), {})  # must not raise

    @pytest.mark.asyncio
    async def test_upsert_creates_relations(self) -> None:
        graph = AsyncMock()
        eid = uuid4()
        rel_id = uuid4()
        await graph_upsert_entity(graph, "Decision", eid, {}, related_to=[{"id": str(rel_id), "type": "RELATED_TO"}])
        graph.create_relation.assert_awaited_once()


class TestGraphDeleteEntity:
    @pytest.mark.asyncio
    async def test_delete_calls_graph(self) -> None:
        graph = AsyncMock()
        eid = uuid4()
        await graph_delete_entity(graph, "Decision", eid)
        graph.delete_node.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_delete_skips_when_none(self) -> None:
        await graph_delete_entity(None, "Decision", uuid4())

    @pytest.mark.asyncio
    async def test_delete_catches_exceptions(self) -> None:
        graph = AsyncMock()
        graph.delete_node.side_effect = Exception("down")
        await graph_delete_entity(graph, "Decision", uuid4())


class TestLinkArtifact:
    @pytest.mark.asyncio
    async def test_links_when_enabled(self) -> None:
        linker = AsyncMock()
        await link_artifact_if_enabled(linker, [0.1]*1536, "decision", uuid4(), "pk", "title")
        linker.link_artifact.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_skips_when_no_linker(self) -> None:
        await link_artifact_if_enabled(None, [0.1]*1536, "decision", uuid4(), "pk", "title")

    @pytest.mark.asyncio
    async def test_skips_when_no_embedding(self) -> None:
        linker = AsyncMock()
        await link_artifact_if_enabled(linker, None, "decision", uuid4(), "pk", "title")
        linker.link_artifact.assert_not_awaited()
```

- [ ] **Step 2: Create helper functions**

Create `src/brain_v42/services/graph_helpers.py`:

```python
"""Standalone helper functions for safe graph writes + feature linking."""
from __future__ import annotations
from uuid import UUID
import structlog

logger = structlog.get_logger(__name__)


async def graph_upsert_entity(
    graph,
    entity_type: str,
    entity_id: UUID,
    props: dict,
    project_key: str | None = None,
    related_to: list[dict] | None = None,
) -> None:
    if graph is None:
        return
    try:
        await graph.upsert_node(entity_type, entity_id, props)
        if project_key:
            await graph.link_to_project(entity_id, project_key)
        for rel in related_to or []:
            await graph.create_relation(entity_id, UUID(rel["id"]), rel["type"])
    except Exception:
        logger.error("graph_write_failed", entity_type=entity_type, entity_id=str(entity_id), exc_info=True)


async def graph_delete_entity(graph, entity_type: str, entity_id: UUID) -> None:
    if graph is None:
        return
    try:
        await graph.delete_node(entity_type, entity_id)
    except Exception:
        logger.error("graph_delete_failed", entity_type=entity_type, entity_id=str(entity_id), exc_info=True)


async def link_artifact_if_enabled(
    feature_linker,
    embedding: list[float] | None,
    artifact_type: str,
    artifact_id: UUID,
    project_key: str | None,
    title: str | None,
) -> None:
    if not feature_linker or not embedding:
        return
    await feature_linker.link_artifact(
        embedding=embedding, artifact_type=artifact_type,
        artifact_id=artifact_id, project_key=project_key, title=title,
    )
```

- [ ] **Step 3: Replace inline blocks in 5 services**

In each service's `create()`, replace the ~15-line graph try/except + feature_linker block with:
```python
from brain_v42.services.graph_helpers import graph_upsert_entity, graph_delete_entity, link_artifact_if_enabled

# In create():
await link_artifact_if_enabled(self._feature_linker, embedding, "decision", result.id, data.project_key, data.title)
await graph_upsert_entity(self._graph, "Decision", result.id, {"project_key": data.project_key, "title": data.title}, project_key=data.project_key, related_to=related_to)

# In delete():
await graph_delete_entity(self._graph, "Decision", decision_id)
```

Repeat for all 5 services. For Learning, use `data.topic` instead of `data.title`.

- [ ] **Step 4: Run tests**

Run: `pytest tests/unit/services/ -x -q`
Expected: ALL PASS (existing mocks on `self._graph.upsert_node` still work — helpers call same methods)

- [ ] **Step 5: Commit**

```bash
git add src/brain_v42/services/graph_helpers.py tests/unit/services/test_graph_helpers.py \
  src/brain_v42/services/decision_service.py src/brain_v42/services/learning_service.py \
  src/brain_v42/services/snippet_service.py src/brain_v42/services/runbook_service.py \
  src/brain_v42/services/adr_service.py
git commit -m "refactor(services): extract graph_helpers — dedup 105 lines across 5 services"
```

---

### Task 5: Extract Decision `_row_to_model()` helper (LOW — cleanup)

**Files:**
- Modify: `src/brain_v42/repositories/pg_decision.py`
- Test: `tests/unit/repositories/test_pg_decision.py` (no changes expected)

**Context:** `{k: v for k, v in dict(row).items() if k != "search_vector"}` appears 7 times inline in `PgDecisionRepo`.

- [ ] **Step 1: Add helper method**

Add to `PgDecisionRepo`:
```python
@staticmethod
def _row_to_decision(row) -> Decision:
    """Convert a DB row to a Decision model, stripping generated columns."""
    data = {k: v for k, v in dict(row).items() if k not in ("search_vector", "rank", "similarity", "distance")}
    return Decision.model_validate(data)
```

- [ ] **Step 2: Replace all 7 inline occurrences**

Search for `Decision.model_validate({k: v for k, v in` and `Decision.model_validate(dict(row))` and replace with `self._row_to_decision(row)`.

- [ ] **Step 3: Run tests**

Run: `pytest tests/unit/repositories/test_pg_decision.py -v`
Expected: ALL PASS (pure refactor, same behavior)

- [ ] **Step 4: Commit**

```bash
git add src/brain_v42/repositories/pg_decision.py
git commit -m "refactor(decision): extract _row_to_decision helper — dedup 7 inline strips"
```

---

### Task 6: Batch decay flush + column prune (MED — N+1 → K+N)

**Files:**
- Modify: `src/brain_v42/services/decay_flusher.py`
- Test: `tests/unit/services/test_decay_flusher.py`

**Context:** `_flush()` does 1 SELECT + 1 UPDATE per entity (2N round-trips). Fix: group by entity_type, 1 SELECT per type (K selects, K<=5), select only needed columns (skip embedding).

- [ ] **Step 1: Write test for batch behavior**

Add to `tests/unit/services/test_decay_flusher.py`:

```python
class TestBatchFlush:
    @pytest.mark.asyncio
    async def test_flush_issues_one_select_per_entity_type(self) -> None:
        """_flush must batch-SELECT by entity_type, not one SELECT per entity."""
        # Mock aggregate_and_flush to return 3 entities of same type
        # Verify session.execute is called with SELECT...WHERE id IN (...)
        # instead of 3 separate SELECT...WHERE id = ...
```

Read the existing test file to understand mock patterns before writing the full test.

- [ ] **Step 2: Refactor `_flush()` and `_update_entity()`**

Replace `_update_entity()` with `_update_entities_batch()` that:
1. Groups `aggregated` dict by `entity_type`
2. Issues one `SELECT id, access_count, freshness_status, created_at, last_accessed_at, [validated_at/decided_at] WHERE id IN (...)` per type (no embedding column)
3. Computes decay for each entity in Python
4. Issues individual UPDATEs (still N updates, but 0 redundant SELECTs)

See the concrete BEFORE/AFTER from deep-decay agent.

- [ ] **Step 3: Run tests**

Run: `pytest tests/unit/services/test_decay_flusher.py -v`

- [ ] **Step 4: Commit**

```bash
git add src/brain_v42/services/decay_flusher.py tests/unit/services/test_decay_flusher.py
git commit -m "perf(decay): batch SELECT by entity_type + skip embedding columns in flush"
```

---

## Batch 3: Data integrity + cosmetic (2 agents, no conflicts)

### Task 7: Fix access_log snapshot delete (MED — data loss bug)

**Files:**
- Modify: `src/brain_v42/repositories/pg_access_log.py`
- Test: `tests/unit/repositories/test_pg_access_log.py`

**Context:** `aggregate_and_flush()` deletes ALL rows, including ones inserted after the SELECT snapshot. Fix: capture `MAX(id)` before aggregate, only delete `WHERE id <= max_id`.

- [ ] **Step 1: Write failing test**

```python
class TestAggregateSnapshotSafety:
    @pytest.mark.asyncio
    async def test_only_deletes_snapshotted_rows(self) -> None:
        """aggregate_and_flush must not delete rows inserted after the SELECT snapshot."""
        # Verify the DELETE statement includes a WHERE id <= max_id condition
```

- [ ] **Step 2: Apply fix**

In `pg_access_log.py`, add `MAX(id)` query before aggregate, then `DELETE ... WHERE id <= max_id`.

See concrete BEFORE/AFTER from deep-decay agent.

- [ ] **Step 3: Run tests**

- [ ] **Step 4: Commit**

```bash
git add src/brain_v42/repositories/pg_access_log.py tests/unit/repositories/test_pg_access_log.py
git commit -m "fix(access_log): delete only snapshotted rows in aggregate_and_flush"
```

---

### Task 8: Runbook model_dump (LOW — cosmetic)

Already covered in Task 3 quick wins batch (item 7). If not done there, do it here.

---

## Batch Execution Plan (TeamCreate)

```
┌──────────────────────────────────────────────────────────┐
│                  PARALLEL BATCH 1                        │
│                                                          │
│  T1: Embed once (6 service files)        [HIGH]         │
│  T2: GPU retry (gpu_embedding_service)   [MED]          │
│  T3: Quick wins (7 files)                [LOW]          │
│                                                          │
│  3 agents in parallel                                    │
└──────────────────────────────────────────────────────────┘
                          │
                          ▼
                   merge + test all
                          │
                          ▼
┌──────────────────────────────────────────────────────────┐
│                  PARALLEL BATCH 2                        │
│                                                          │
│  T4: Graph helpers (6 service files)     [MED]          │
│  T5: Decision _row_to_model              [LOW]          │
│  T6: Decay batch flush                   [MED]          │
│                                                          │
│  3 agents in parallel                                    │
└──────────────────────────────────────────────────────────┘
                          │
                          ▼
                   merge + test all
                          │
                          ▼
┌──────────────────────────────────────────────────────────┐
│                  PARALLEL BATCH 3                        │
│                                                          │
│  T7: Access log snapshot fix             [MED]          │
│                                                          │
│  1 agent                                                 │
└──────────────────────────────────────────────────────────┘
                          │
                          ▼
              ruff + mypy + push + CI
```

### Conflict check

| Batch | T1 files | T2 files | T3 files | Conflicts? |
|-------|----------|----------|----------|------------|
| 1 | 6 service files + brain_service | gpu_embedding_service | server, pg_adr, pg_runbook, pg_base, formatters, plan_indexer | brain_service.py shared T1+T3 (ensure_future fix) |

**Fix**: T3 should NOT touch `brain_service.py` — the `ensure_future → create_task` fix is absorbed into T1's `_fan_out()` rewrite.

| Batch | T4 files | T5 files | T6 files | Conflicts? |
|-------|----------|----------|----------|------------|
| 2 | graph_helpers + 5 services | pg_decision | decay_flusher | None |

### Post-merge verification

After each batch:
```bash
ruff check src/ tests/ && ruff format src/ tests/
mypy src/
pytest tests/unit -x -q
```
