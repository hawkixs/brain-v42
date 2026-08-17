# Audit #2 — Perf + Quality Fixes Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix 5 performance issues and 2 quality bugs found during the 2026-03-19 code audit — bulk UPDATEs, column pruning in FTS/vector search, parallel plan indexing, embed_texts metrics gap, and private attribute access cleanup.

**Architecture:** 3 independent batches organized by file ownership. No file conflicts between tasks in the same batch. Each task follows TDD: write failing test first, then fix.

**Tech Stack:** Python 3.12+, SQLAlchemy 2.0 async, pytest-asyncio, structlog

**Test command:** `source .venv/bin/activate && pytest tests/unit -x -q`

**Lint command:** `source .venv/bin/activate && ruff check src/ tests/ && ruff format src/ tests/`

---

## File Map

| Batch | Task | Files Modified | Files Tested |
|-------|------|---------------|--------------|
| 1 | T1: Bulk UPDATEs in decay_flusher | `services/decay_flusher.py` | `tests/unit/services/test_decay_flusher.py` |
| 1 | T2: Column pruning in pg_base FTS + vector | `repositories/pg_base.py` | `tests/unit/repositories/test_pg_base.py` |
| 2 | T3: Parallel plan indexing | `services/plan_indexer.py` | `tests/unit/test_plan_indexer.py` |
| 2 | T4: embed_texts metrics bypass fix | `metrics/instrument.py` | `tests/unit/test_metrics_instrument.py` |
| 3 | T5: Expose graph avg_latency via public API | `metrics/collector.py`, `metrics/server.py` | `tests/unit/test_metrics_collector.py`, `tests/unit/test_metrics_server.py` |

---

## Batch 1: Database Performance (2 agents, no conflicts)

### Task 1: Bulk UPDATEs in DecayFlusher (HIGH — N roundtrips → 1-2)

**Files:**
- Modify: `src/brain_v42/services/decay_flusher.py:124-172`
- Test: `tests/unit/services/test_decay_flusher.py`

**Context:** `_update_entities_batch()` loops through entities and executes one `UPDATE` per entity. With 200 entities, that's 200 sequential DB roundtrips. Replace with bulk UPDATEs using `executemany` (SQLAlchemy `bindparam`). The decay computation depends on per-row data from the SELECT, so it stays in Python. Updates are split into two groups: entities with freshness_status change vs without (different column sets).

**Important:** Existing tests assert specific `session.execute.call_count` values (3, 4, 2) based on 1 SELECT + N individual UPDATEs. These counts change with bulk UPDATEs (1 SELECT + 1-2 bulk UPDATEs instead of 1 SELECT + N UPDATEs). Update those assertions too.

- [ ] **Step 1: Write failing test for bulk update behavior**

Add to `tests/unit/services/test_decay_flusher.py`:

```python
class TestBulkUpdate:
    @pytest.mark.asyncio
    async def test_update_entities_batch_fewer_calls_than_entities(
        self,
        session_factory: MagicMock,
        access_log_repo: AsyncMock,
        decay_calculator: DecayCalculator,
        session: AsyncMock,
    ) -> None:
        """_update_entities_batch must use bulk UPDATE, not one UPDATE per entity."""
        now = datetime.now(tz=UTC)
        created = now - timedelta(days=10)  # fresh entities

        # 10 entities — old code would do 1 SELECT + 10 UPDATEs = 11 calls
        ids = [uuid4() for _ in range(10)]
        aggregated = {
            ("decision", eid): {"max_accessed": now, "count": 1}
            for eid in ids
        }
        access_log_repo.aggregate_and_flush.return_value = aggregated

        rows = [
            {
                "id": eid,
                "created_at": created,
                "access_count": 0,
                "freshness_status": "fresh",
                "last_accessed_at": None,
            }
            for eid in ids
        ]
        select_result = MagicMock()
        select_result.mappings.return_value.all.return_value = rows
        session.execute = AsyncMock(return_value=select_result)

        flusher = DecayFlusher(
            session_factory=session_factory,
            access_log_repo=access_log_repo,
            decay_calculator=decay_calculator,
        )
        await flusher._flush()

        # Bulk: 1 SELECT + 1-2 bulk UPDATEs = 2-3 calls (NOT 11)
        call_count = session.execute.call_count
        assert call_count <= 3, f"Expected bulk UPDATE (<=3 calls), got {call_count} (likely N individual UPDATEs)"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/services/test_decay_flusher.py::TestBulkUpdate -v`
Expected: FAIL — currently makes 11 calls (1 SELECT + 10 UPDATEs)

- [ ] **Step 3: Refactor _update_entities_batch to use bulk UPDATE**

In `src/brain_v42/services/decay_flusher.py`, replace the for-loop at lines 124-172 with:

```python
        # Compute updates in Python, then issue bulk UPDATEs
        status_changed: list[dict[str, Any]] = []
        status_same: list[dict[str, Any]] = []

        for entity_id, stats in id_stats.items():
            row = rows.get(entity_id)
            if row is None:
                continue

            new_access_count = row["access_count"] + stats["count"]
            new_last_accessed = stats["max_accessed"]

            is_validated = False
            if "validated_at" in table.c and row["validated_at"] is not None:
                is_validated = True
            elif "decided_at" in table.c and row["decided_at"] is not None:
                is_validated = True

            multiplier = self._decay_calculator.compute_multiplier(
                entity_type=entity_type,
                created_at=row["created_at"],
                last_accessed_at=new_last_accessed,
                access_count=new_access_count,
                is_validated=is_validated,
            )

            new_status = self._decay_calculator.freshness_status(multiplier)
            old_status = row["freshness_status"]

            params: dict[str, Any] = {
                "_entity_id": entity_id,
                "access_count": new_access_count,
                "last_accessed_at": new_last_accessed,
            }

            if new_status != old_status:
                params["freshness_status"] = new_status
                logger.info(
                    "freshness_transition",
                    entity_type=entity_type,
                    entity_id=str(entity_id),
                    old=old_status,
                    new=new_status,
                    multiplier=round(multiplier, 3),
                )
                status_changed.append(params)
            else:
                status_same.append(params)

        # Bulk UPDATE — one statement per group (different column sets)
        if status_same:
            stmt = (
                sa.update(table)
                .where(table.c.id == sa.bindparam("_entity_id"))
                .values(
                    access_count=sa.bindparam("access_count"),
                    last_accessed_at=sa.bindparam("last_accessed_at"),
                )
            )
            await session.execute(stmt, status_same)

        if status_changed:
            stmt = (
                sa.update(table)
                .where(table.c.id == sa.bindparam("_entity_id"))
                .values(
                    access_count=sa.bindparam("access_count"),
                    last_accessed_at=sa.bindparam("last_accessed_at"),
                    freshness_status=sa.bindparam("freshness_status"),
                )
            )
            await session.execute(stmt, status_changed)
```

- [ ] **Step 4: Update existing tests that assert exact execute call counts**

In `tests/unit/services/test_decay_flusher.py`:

**`test_flush_batches_same_type_in_single_select` (line 177):** Change assertion from `== 3` to `<= 3`:
```python
# OLD: assert session.execute.call_count == 3
# NEW (1 SELECT + 1-2 bulk UPDATEs):
assert session.execute.call_count <= 3
```

**`test_flush_groups_by_entity_type` (line 230):** Change from `== 4` to `<= 4`:
```python
# OLD: assert session.execute.call_count == 4
# NEW (2 SELECTs + 1-2 bulk UPDATEs per type):
assert session.execute.call_count <= 4
```

**`test_flush_skips_missing_entity_in_batch` (line 299):** Keep as `== 2` (1 SELECT + 1 bulk UPDATE for 1 entity — same count).

- [ ] **Step 5: Run all tests**

Run: `pytest tests/unit/services/test_decay_flusher.py -v`
Expected: ALL PASS

- [ ] **Step 6: Run lint + commit**

```bash
ruff check src/brain_v42/services/decay_flusher.py && ruff format src/brain_v42/services/decay_flusher.py
git add src/brain_v42/services/decay_flusher.py tests/unit/services/test_decay_flusher.py
git commit -m "perf(decay): bulk UPDATE via executemany instead of N sequential UPDATEs"
```

---

### Task 2: Column Pruning in FTS + Vector Search (MEDIUM — skip embedding columns)

**Files:**
- Modify: `src/brain_v42/repositories/pg_base.py:273-281,324-329`
- Test: `tests/unit/repositories/test_pg_base.py`

**Context:** `search_fts()` and `search_vector()` both use `sa.select(self.table, ...)` which selects ALL columns including `embedding` (1536 floats) and `search_vector` (tsvector). These heavy columns are never needed in search results. Excluding them saves ~6KB per row.

**Test pattern:** Existing tests in `test_pg_base.py` use `full_repo` fixture (has `embedding` + `search_vector` columns), `_make_mock_session()` helper, and `_patch_factory()` for session injection.

- [ ] **Step 1: Write failing test for column exclusion**

Add to `tests/unit/repositories/test_pg_base.py`:

```python
class TestSearchColumnPruning:
    """search_fts() and search_vector() must NOT project embedding/search_vector columns."""

    def test_search_columns_excludes_heavy_columns(self, full_repo):
        """_search_columns() must exclude embedding and search_vector."""
        col_names = [c.name for c in full_repo._search_columns()]
        assert "embedding" not in col_names
        assert "search_vector" not in col_names
        # But other columns should still be present
        assert "id" in col_names
        assert "name" in col_names
        assert "project_key" in col_names

    def test_search_columns_returns_all_on_simple_table(self, simple_repo):
        """_search_columns() on a table without embedding returns all columns."""
        cols = simple_repo._search_columns()
        col_names = [c.name for c in cols]
        assert "id" in col_names
        assert "name" in col_names
        # simple_repo has no embedding/search_vector, so all columns returned
        assert len(cols) == len(simple_repo.table.c)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/repositories/test_pg_base.py::TestSearchColumnPruning -v`
Expected: FAIL — `_search_columns` method does not exist

- [ ] **Step 3: Add helper method and fix search_fts + search_vector**

In `src/brain_v42/repositories/pg_base.py`, add after `_build_filter_clauses` (line 238):

```python
    # Columns to exclude from search results (heavy, not needed for display)
    _SEARCH_EXCLUDE_COLS: frozenset[str] = frozenset({"embedding", "search_vector"})

    def _search_columns(self) -> list[sa.Column]:
        """Return table columns suitable for search results (excludes embedding, search_vector)."""
        return [c for c in self.table.c if c.name not in self._SEARCH_EXCLUDE_COLS]
```

Then in `search_fts()` (around line 274), change:
```python
# OLD:
sa.select(self.table, rank_expr)
# NEW:
sa.select(*self._search_columns(), rank_expr)
```

And in `search_vector()` (around line 325), change:
```python
# OLD:
sa.select(self.table, similarity_expr)
# NEW:
sa.select(*self._search_columns(), similarity_expr)
```

- [ ] **Step 4: Run all tests**

Run: `pytest tests/unit/repositories/test_pg_base.py -v`
Expected: ALL PASS (existing FTS/vector tests still pass because they use mocked execute)

- [ ] **Step 5: Run lint + commit**

```bash
ruff check src/brain_v42/repositories/pg_base.py && ruff format src/brain_v42/repositories/pg_base.py
git add src/brain_v42/repositories/pg_base.py tests/unit/repositories/test_pg_base.py
git commit -m "perf(search): exclude embedding/search_vector from FTS + vector search results"
```

---

## Batch 2: I/O + Metrics (2 agents, no conflicts)

### Task 3: Parallel Plan Indexing via gather() (MEDIUM — 5-10x faster startup)

**Files:**
- Modify: `src/brain_v42/services/plan_indexer.py:92-152`
- Test: `tests/unit/test_plan_indexer.py`

**Context:** `index_path()` processes files sequentially — each file does `await embed()` + `await upsert()` + `await resolve()` in series. Use `asyncio.gather()` with a semaphore to bound concurrency.

**Test pattern:** Existing tests use `mock_deps` fixture + `_build_indexer()` helper + `_mock_execute_for_new_file()` to set up mock DB responses.

- [ ] **Step 1: Write failing test for concurrent processing**

Add to `tests/unit/test_plan_indexer.py`:

```python
@pytest.mark.asyncio
async def test_index_path_processes_files_concurrently(mock_deps, tmp_path):
    """index_path should use gather() to process files, not sequential loop."""
    import asyncio

    # Create 5 plan files
    for i in range(5):
        (tmp_path / f"2026-01-0{i + 1}-feature-{i}-plan.md").write_text(f"# Plan {i}\nContent")

    # Track concurrency: record how many embeds are in-flight simultaneously
    in_flight = 0
    max_concurrent = 0

    original_embed = mock_deps["embedding_svc"].embed

    async def tracking_embed(text):
        nonlocal in_flight, max_concurrent
        in_flight += 1
        max_concurrent = max(max_concurrent, in_flight)
        await asyncio.sleep(0.01)  # small delay to expose concurrency
        in_flight -= 1
        return [0.1] * 1536

    mock_deps["embedding_svc"].embed = tracking_embed

    # Set up DB mocks: 5 files × 3 calls each (is_unchanged + upsert + link)
    side_effects = []
    for _ in range(5):
        unchanged = MagicMock()
        unchanged.fetchone.return_value = None
        upsert = MagicMock()
        upsert_row = MagicMock()
        upsert_row.id = uuid.uuid4()
        upsert.fetchone.return_value = upsert_row
        link = MagicMock()
        side_effects.extend([unchanged, upsert, link])

    mock_deps["session"].execute = AsyncMock(side_effect=side_effects)

    indexer = _build_indexer(mock_deps)
    stats = await indexer.index_path(str(tmp_path), "test_project")

    assert stats["indexed"] == 5
    # If parallel: max_concurrent > 1 (multiple embeds in-flight)
    # If sequential: max_concurrent == 1 (one at a time)
    assert max_concurrent > 1, f"max_concurrent={max_concurrent} — files processed sequentially, not in parallel"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_plan_indexer.py::test_index_path_processes_files_concurrently -v`
Expected: FAIL — `max_concurrent == 1` because files are processed sequentially

- [ ] **Step 3: Refactor index_path to use gather()**

Add `import asyncio` at the top of `src/brain_v42/services/plan_indexer.py` (after `import hashlib`).

Replace the `index_path()` method body (lines 86-159) with:

```python
    async def index_path(self, scan_path: str, project_key: str) -> dict[str, int]:
        """Scan dir for plan files, index new/changed ones concurrently.

        Returns:
            {"indexed": N, "skipped": M, "linked": L}
        """
        stats = {"indexed": 0, "skipped": 0, "linked": 0}

        files = self._find_plan_files(scan_path)
        if not files:
            return stats

        sem = asyncio.Semaphore(5)

        async def _process_file(file_path: Path) -> tuple[int, int, int, int]:
            """Process one file. Returns (indexed, skipped, linked, errors)."""
            async with sem:
                try:
                    content = file_path.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    logger.warning("plan_indexer.decode_failed", file=str(file_path))
                    return (0, 0, 0, 1)
                content_hash = self._content_hash(content)

                if await self._is_unchanged(str(file_path), content_hash):
                    return (0, 1, 0, 0)

                title, plan_type = self.parse_plan(str(file_path), content)
                embed_text = f"{title}\n{content[:500]}"
                try:
                    embedding = await self._embedding_svc.embed(embed_text)
                except Exception:
                    logger.warning("plan_indexer.embed_failed", file=str(file_path), exc_info=True)
                    return (0, 0, 0, 1)

                plan_id = await self._upsert_plan(
                    file_path=str(file_path),
                    title=title,
                    plan_type=plan_type,
                    project_key=project_key,
                    content_hash=content_hash,
                    embedding=embedding,
                )

                linked = 0
                try:
                    feature, action = await self._cluster_guard.resolve(
                        text=title,
                        embedding=embedding,
                        project_key=project_key,
                        signal_type="plan",
                    )
                    await self._link_plan_to_feature(
                        feature_id=feature.id,
                        plan_id=plan_id,
                        similarity_score=1.0 if action == "created" else 0.85,
                    )
                    linked = 1
                except Exception:
                    logger.warning(
                        "plan_indexer.link_failed",
                        file_path=str(file_path),
                        exc_info=True,
                    )

                return (1, 0, linked, 0)

        results = await asyncio.gather(
            *[_process_file(f) for f in files],
            return_exceptions=True,
        )

        for result in results:
            if isinstance(result, Exception):
                logger.warning("plan_indexer.file_error", error=str(result))
                stats["errors"] = stats.get("errors", 0) + 1
                continue
            indexed, skipped, linked, errors = result
            stats["indexed"] += indexed
            stats["skipped"] += skipped
            stats["linked"] += linked
            if errors:
                stats["errors"] = stats.get("errors", 0) + errors

        logger.info(
            "plan_indexer.index_path_done",
            scan_path=scan_path,
            project_key=project_key,
            **stats,
        )
        return stats
```

- [ ] **Step 4: Run all tests**

Run: `pytest tests/unit/test_plan_indexer.py -v`
Expected: ALL PASS

- [ ] **Step 5: Run lint + commit**

```bash
ruff check src/brain_v42/services/plan_indexer.py && ruff format src/brain_v42/services/plan_indexer.py
git add src/brain_v42/services/plan_indexer.py tests/unit/test_plan_indexer.py
git commit -m "perf(plan_indexer): parallel file processing via asyncio.gather + semaphore"
```

---

### Task 4: Fix embed_texts Early Return Skipping Metrics (MEDIUM — bug fix)

**Files:**
- Modify: `src/brain_v42/metrics/instrument.py:61-63`
- Test: `tests/unit/test_metrics_instrument.py`

**Context:** `InstrumentedEmbeddingService.embed_texts()` returns early for empty lists without recording metrics. The fix: remove the early-return branch so all calls go through the timing/recording path.

- [ ] **Step 1: Write failing test**

Add to `tests/unit/test_metrics_instrument.py`:

```python
class TestEmbedTextsEmptyMetrics:
    @pytest.mark.asyncio
    async def test_embed_texts_empty_records_metrics(self):
        """embed_texts([]) must still record a metrics entry."""
        inner = AsyncMock()
        inner.embed_texts = AsyncMock(return_value=[])
        collector = MagicMock(spec=MetricsCollector)

        svc = InstrumentedEmbeddingService(inner, collector)
        result = await svc.embed_texts([])

        assert result == []
        inner.embed_texts.assert_awaited_once_with([])
        collector.record_embedding_request.assert_called_once()
        assert collector.record_embedding_request.call_args[1]["error"] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_metrics_instrument.py::TestEmbedTextsEmptyMetrics -v`
Expected: FAIL — `record_embedding_request` not called (early return bypasses it)

- [ ] **Step 3: Fix the early return**

In `src/brain_v42/metrics/instrument.py`, replace lines 61-73:

```python
    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        start = time.monotonic()
        error = False
        try:
            return await self._inner.embed_texts(texts)  # type: ignore[no-any-return]
        except Exception:
            error = True
            raise
        finally:
            latency_ms = (time.monotonic() - start) * 1000
            self._collector.record_embedding_request(latency_ms, error=error)
```

- [ ] **Step 4: Run all tests**

Run: `pytest tests/unit/test_metrics_instrument.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add src/brain_v42/metrics/instrument.py tests/unit/test_metrics_instrument.py
git commit -m "fix(metrics): record embed_texts metrics even for empty input lists"
```

---

## Batch 3: Encapsulation Fix (1 agent)

### Task 5: Expose Graph Avg Latency via Public Collector API (MEDIUM — encapsulation)

**Files:**
- Modify: `src/brain_v42/metrics/collector.py` — add `get_graph_avg_latency()` method
- Modify: `src/brain_v42/metrics/server.py:118-131` — use public method instead of `_graph_stats`
- Test: `tests/unit/test_metrics_collector.py`

**Context:** `MetricsServer._handle_metrics()` accesses `self._collector._graph_stats["total_latency"]` directly (line 121). Add a public method.

- [ ] **Step 1: Write failing test for public method**

Add to `tests/unit/test_metrics_collector.py`:

```python
class TestGraphAvgLatency:
    def test_get_graph_avg_latency_returns_zero_when_no_queries(self, collector):
        """get_graph_avg_latency() must return 0.0 when no graph queries recorded."""
        assert collector.get_graph_avg_latency() == 0.0

    def test_get_graph_avg_latency_computes_average(self, collector):
        """get_graph_avg_latency() must return total_latency / total_queries."""
        collector.record_graph_query(10.0)
        collector.record_graph_query(20.0)
        collector.record_graph_query(30.0)
        assert collector.get_graph_avg_latency() == 20.0

    def test_get_graph_avg_latency_rounds_to_one_decimal(self, collector):
        """get_graph_avg_latency() must round to 1 decimal place."""
        collector.record_graph_query(10.0)
        collector.record_graph_query(10.0)
        collector.record_graph_query(11.0)
        # (10 + 10 + 11) / 3 = 10.333... → 10.3
        assert collector.get_graph_avg_latency() == 10.3
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_metrics_collector.py::TestGraphAvgLatency -v`
Expected: FAIL — `get_graph_avg_latency` method does not exist

- [ ] **Step 3: Add public method to MetricsCollector**

In `src/brain_v42/metrics/collector.py`, after `record_graph_query()` (around line 121):

```python
    def get_graph_avg_latency(self) -> float:
        """Return average graph query latency in ms (0.0 if no queries)."""
        total = self._graph_stats["total_queries"]
        if not total:
            return 0.0
        return round(self._graph_stats["total_latency"] / total, 1)
```

- [ ] **Step 4: Update server.py to use public method**

In `src/brain_v42/metrics/server.py`, replace lines 118-131:

```python
            graph_stats = metrics.get("graph", {})
            avg_graph_latency = self._collector.get_graph_avg_latency()
            metrics["graph"] = {
                "status": "up" if graph_healthy else "down",
                "total_queries": graph_stats.get("total_queries", 0),
                "total_errors": graph_stats.get("total_errors", 0),
                "recent_errors": graph_stats.get("recent_errors", 0),
                "avg_latency_ms": avg_graph_latency,
            }
```

- [ ] **Step 5: Run all tests**

Run: `pytest tests/unit/test_metrics_collector.py tests/unit/test_metrics_server.py -v`
Expected: ALL PASS

- [ ] **Step 6: Run lint + commit**

```bash
ruff check src/brain_v42/metrics/collector.py src/brain_v42/metrics/server.py && ruff format src/brain_v42/metrics/collector.py src/brain_v42/metrics/server.py
git add src/brain_v42/metrics/collector.py src/brain_v42/metrics/server.py tests/unit/test_metrics_collector.py
git commit -m "refactor(metrics): expose graph avg_latency via public API, remove private access"
```

---

## Final Verification

After all tasks:

```bash
source .venv/bin/activate && pytest tests/unit -x -q
ruff check src/ tests/ && ruff format --check src/ tests/
```

Expected: All tests pass, no lint issues.
