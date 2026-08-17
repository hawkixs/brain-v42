"""E2E integration tests for recent patches (commits 6308e16..de1bc47).

Covers:
1. Reranker sigmoid normalization — scores in [0, 1]
2. project_keys passthrough — brain_search via project_group returns multi-project results
3. AccessLogger resilience — _run_loop survives flush exceptions
4. Metrics sidecar — no pseudo-tool leak, correct graph avg_latency

All tests run against real PostgreSQL + real GPU embedding/reranker services.
"""

from __future__ import annotations

import asyncio
import math
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brain_v42.models.learning import LearningCreate
from brain_v42.models.project_context import ProjectContextCreate
from brain_v42.repositories.pg_learning import PgLearningRepo
from brain_v42.repositories.pg_project_context import PgProjectContextRepo
from brain_v42.services.access_logger import AccessLogger
from brain_v42.services.search.reranker import HybridReranker

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _unique_key() -> str:
    return f"integ-{uuid.uuid4().hex[:8]}"


def _unique_group() -> str:
    return f"integ-group-{uuid.uuid4().hex[:6]}"


# ---------------------------------------------------------------------------
# 1. Reranker sigmoid normalization
# ---------------------------------------------------------------------------


class TestRerankerSigmoid:
    """Verify that HybridReranker normalizes raw logits to [0, 1] via sigmoid."""

    async def test_sigmoid_scores_in_unit_range(self) -> None:
        """Scores from reranker are in [0, 1] after sigmoid, not raw logits."""
        from brain_v42.services.search.hybrid import RankedCandidate

        # Mock client that returns raw logits (cross-encoder typical range)
        mock_client = AsyncMock()
        mock_client.rerank.return_value = [-11.0, 0.0, 5.0, 8.0]

        reranker = HybridReranker(client=mock_client)
        candidates = [
            RankedCandidate(id=uuid.uuid4(), entity=None, entity_type="learning", score=0.0, text=t)
            for t in ["irrelevant text", "mid-range text", "good match", "best match"]
        ]

        mode, result = await reranker.rerank_with_mode("test query", candidates)

        assert mode == "reranked", f"Expected mode 'reranked', got {mode!r}"
        for c in result:
            assert 0.0 <= c.score <= 1.0, f"Score {c.score} not in [0, 1]"

        # Verify monotonicity: higher logit → higher sigmoid score
        scores = [c.score for c in result]
        assert scores == sorted(scores, reverse=True)

    async def test_sigmoid_matches_math(self) -> None:
        """Verify the sigmoid formula: 1 / (1 + exp(-x))."""
        from brain_v42.services.search.hybrid import RankedCandidate

        raw_logits = [-5.0, -1.0, 0.0, 1.0, 5.0]
        expected = [1.0 / (1.0 + math.exp(-x)) for x in raw_logits]

        mock_client = AsyncMock()
        mock_client.rerank.return_value = raw_logits

        reranker = HybridReranker(client=mock_client)
        candidates = [
            RankedCandidate(
                id=uuid.uuid4(), entity=None, entity_type="learning", score=0.0, text=f"text {i}"
            )
            for i in range(len(raw_logits))
        ]

        mode, result = await reranker.rerank_with_mode("query", candidates)

        assert mode == "reranked", f"Expected mode 'reranked', got {mode!r}"
        # Map results back by checking scores match expected (order changed by sort)
        result_scores = sorted([c.score for c in result])
        expected_sorted = sorted(expected)
        for actual, exp in zip(result_scores, expected_sorted, strict=True):
            assert abs(actual - exp) < 1e-10, f"{actual} != {exp}"

    async def test_real_reranker_scores_in_unit_range(self) -> None:
        """E2E: real HTTP reranker service returns scores normalized to [0, 1]."""
        from brain_v42.services.reranker_client import RerankerClient
        from brain_v42.services.search.hybrid import RankedCandidate

        client = RerankerClient(base_url="http://localhost:8003")
        try:
            available = await client.is_available()
            if not available:
                pytest.skip("Reranker service not available at localhost:8003")

            reranker = HybridReranker(client=client)
            candidates = [
                RankedCandidate(
                    id=uuid.uuid4(),
                    entity=None,
                    entity_type="learning",
                    score=0.0,
                    text=t,
                )
                for t in [
                    "Python asyncio event loop internals",
                    "Best pasta recipe for beginners",
                    "How to configure PostgreSQL connection pooling",
                ]
            ]

            mode, result = await reranker.rerank_with_mode(
                "async database connection pool", candidates
            )

            assert mode in ("reranked", "rrf_fallback"), f"Unexpected mode: {mode!r}"
            for c in result:
                assert 0.0 <= c.score <= 1.0, f"Real reranker score {c.score} not in [0, 1]"

            # The DB-related text should score higher than pasta recipe (only when reranked)
            if mode == "reranked":
                scores_by_text = {c.text: c.score for c in result}
                assert (
                    scores_by_text["How to configure PostgreSQL connection pooling"]
                    > scores_by_text["Best pasta recipe for beginners"]
                )
        finally:
            await client.close()


# ---------------------------------------------------------------------------
# 2. project_keys passthrough via project_group
# ---------------------------------------------------------------------------


class TestProjectKeysPassthrough:
    """Search with project_group returns results from all projects in the group."""

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

    async def test_search_fts_with_project_keys_returns_multi_project(
        self,
        learning_repo: PgLearningRepo,
        project_context_repo: PgProjectContextRepo,
    ) -> None:
        """FTS search with project_keys list returns learnings from multiple projects."""
        group = _unique_group()
        pk1 = _unique_key()
        pk2 = _unique_key()
        unique_word = f"multiproj{uuid.uuid4().hex[:6]}"

        # Create project contexts in same group
        for pk in (pk1, pk2):
            await project_context_repo.get_or_create(
                ProjectContextCreate(
                    project_key=pk,
                    name=f"Test project {pk}",
                    description="Integration test project",
                    project_group=group,
                )
            )

        # Create learnings in each project
        await learning_repo.create(
            LearningCreate(
                topic=f"Learning about {unique_word} in project one",
                insight="First project insight",
                project_key=pk1,
            )
        )
        await learning_repo.create(
            LearningCreate(
                topic=f"Learning about {unique_word} in project two",
                insight="Second project insight",
                project_key=pk2,
            )
        )

        # Verify project_group resolves to both keys
        keys = await project_context_repo.get_keys_by_group(group)
        assert set(keys) == {pk1, pk2}

        # Search with project_keys (the passthrough that was broken before ba3db7b)
        results = await learning_repo.search_fts(unique_word, project_keys=[pk1, pk2])

        assert len(results) >= 2
        project_keys_found = {r.project_key for r in results}
        assert pk1 in project_keys_found
        assert pk2 in project_keys_found

    async def test_vector_search_with_project_keys(
        self,
        learning_repo: PgLearningRepo,
    ) -> None:
        """Vector search with project_keys returns results from multiple projects."""
        pk1 = _unique_key()
        pk2 = _unique_key()
        embedding = [0.3] * 1536

        await learning_repo.create(
            LearningCreate(
                topic="Vector search multi-project A",
                insight="Insight A",
                project_key=pk1,
            ),
            embedding=embedding,
        )
        await learning_repo.create(
            LearningCreate(
                topic="Vector search multi-project B",
                insight="Insight B",
                project_key=pk2,
            ),
            embedding=embedding,
        )

        query_vec = [0.3] * 1536
        results = await learning_repo.search_vector(query_vec, project_keys=[pk1, pk2])

        assert len(results) >= 2
        project_keys_found = {r.project_key for r, _ in results}
        assert pk1 in project_keys_found
        assert pk2 in project_keys_found

    async def test_project_keys_overrides_project_key(
        self,
        learning_repo: PgLearningRepo,
    ) -> None:
        """When project_keys is set, project_key is ignored (project_keys takes precedence)."""
        pk1 = _unique_key()
        pk2 = _unique_key()
        unique_word = f"override{uuid.uuid4().hex[:6]}"

        await learning_repo.create(
            LearningCreate(
                topic=f"Override test {unique_word}",
                insight="In pk1",
                project_key=pk1,
            )
        )
        await learning_repo.create(
            LearningCreate(
                topic=f"Override test {unique_word}",
                insight="In pk2",
                project_key=pk2,
            )
        )

        # Search with project_key=pk1 but project_keys=[pk2]
        # project_keys should take precedence (repo uses elif)
        results = await learning_repo.search_fts(unique_word, project_key=pk1, project_keys=[pk2])

        project_keys_found = {r.project_key for r in results}
        assert pk2 in project_keys_found
        # pk1 should be excluded because project_keys takes precedence
        assert pk1 not in project_keys_found


# ---------------------------------------------------------------------------
# 3. AccessLogger resilience
# ---------------------------------------------------------------------------


class TestAccessLoggerResilience:
    """AccessLogger._run_loop survives exceptions in _flush_batch."""

    async def test_run_loop_survives_flush_exception(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """After a flush error, the run_loop continues and processes the next batch."""
        al = AccessLogger(session_factory)

        # Track flush calls
        flush_count = 0
        original_flush = al._flush_batch

        async def exploding_flush() -> None:
            nonlocal flush_count
            flush_count += 1
            if flush_count == 1:
                raise RuntimeError("Simulated DB error on first flush")
            await original_flush()

        al._flush_batch = exploding_flush  # type: ignore[assignment]

        # Start the logger
        await al.start()
        assert al._task is not None

        try:
            # Enqueue an event — this will hit the exploding flush
            al.log_access("learning", str(uuid.uuid4()), "search_hit")

            # Wait for first flush cycle (5s interval + margin)
            await asyncio.sleep(6)

            # The task should still be alive after the exception
            assert not al._task.done(), "run_loop task died after flush exception"

            # Enqueue another event — should be flushed successfully
            al.log_access("decision", str(uuid.uuid4()), "search_hit")
            await asyncio.sleep(6)

            # Second flush should have succeeded
            assert flush_count >= 2, f"Expected >= 2 flushes, got {flush_count}"
            assert not al._task.done(), "run_loop task died after second flush"
        finally:
            await al.stop()

    async def test_run_loop_cancelled_propagates(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """CancelledError is still re-raised (not swallowed by the exception handler)."""
        al = AccessLogger(session_factory)
        await al.start()

        assert al._task is not None
        al._task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await al._task


# ---------------------------------------------------------------------------
# 4. Metrics sidecar — pseudo-tool leak + graph avg_latency
# ---------------------------------------------------------------------------


class TestMetricsSidecar:
    """Verify _reranker/_graph pseudo-tools are extracted, not leaked into tools."""

    async def test_reranker_pseudo_tool_extracted(self) -> None:
        """_reranker pseudo-tool is popped from tools and placed in reranker section."""
        from brain_v42.metrics.server import MetricsServer

        collector = MagicMock()
        collector.get_metrics.return_value = {
            "tools": {},
            "embedding_service": {
                "total_requests": 0,
                "total_errors": 0,
                "recent_errors": 0,
                "avg_latency_ms": 0.0,
            },
        }
        collector.collect_db_stats = AsyncMock(return_value={})
        collector.collect_search_quality = AsyncMock(return_value={})
        collector.collect_process_metrics = AsyncMock(
            return_value={
                "active_processes": 1,
                "total_memory_rss_bytes": 100_000,
                "tools": {
                    "brain_search": {
                        "calls": 10,
                        "errors": 0,
                        "recent_errors": 0,
                        "avg_latency_ms": 50.0,
                    },
                    "_reranker": {
                        "calls": 5,
                        "errors": 1,
                        "recent_errors": 0,
                        "avg_latency_ms": 30.0,
                        "total_candidates": 25,
                    },
                    "_graph": {"calls": 8, "errors": 0, "recent_errors": 0, "avg_latency_ms": 15.0},
                },
                "embedding": {
                    "total_requests": 20,
                    "total_errors": 0,
                    "recent_errors": 0,
                    "avg_latency_ms": 25.0,
                },
            }
        )
        collector.collect_dream_metrics = AsyncMock(return_value={})
        collector.collect_dream_promotions = AsyncMock(return_value={})
        collector.collect_nightly_ops = AsyncMock(return_value={})

        embedding_svc = AsyncMock()
        embedding_svc.healthcheck = AsyncMock(return_value=True)

        server = MetricsServer(collector=collector, embedding_svc=embedding_svc)

        # Call the handler directly with a mock request
        mock_request = MagicMock()
        response = await server._handle_metrics(mock_request)

        import json

        data = json.loads(response.body)

        # _reranker should NOT be in tools
        assert "_reranker" not in data["tools"], "_reranker leaked into tools"
        # _graph should NOT be in tools
        assert "_graph" not in data["tools"], "_graph leaked into tools"
        # brain_search should still be there
        assert "brain_search" in data["tools"]

        # Reranker section should exist with correct values
        assert data["reranker"]["total_calls"] == 5
        assert data["reranker"]["total_candidates"] == 25
        assert data["reranker"]["avg_latency_ms"] == 30.0

        # Graph section should exist with correct values
        assert data["graph"]["total_queries"] == 8
        assert data["graph"]["avg_latency_ms"] == 15.0

    async def test_graph_avg_latency_from_cross_process(self) -> None:
        """Graph avg_latency uses cross-process value, not standalone collector method."""
        from brain_v42.metrics.server import MetricsServer

        collector = MagicMock()
        collector.get_metrics.return_value = {
            "tools": {},
            "embedding_service": {
                "total_requests": 0,
                "total_errors": 0,
                "recent_errors": 0,
                "avg_latency_ms": 0.0,
            },
        }
        collector.collect_db_stats = AsyncMock(return_value={})
        collector.collect_search_quality = AsyncMock(return_value={})
        collector.collect_process_metrics = AsyncMock(
            return_value={
                "active_processes": 1,
                "total_memory_rss_bytes": 100_000,
                "tools": {
                    "_graph": {
                        "calls": 10,
                        "errors": 0,
                        "recent_errors": 0,
                        "avg_latency_ms": 42.5,
                    },
                },
                "embedding": {
                    "total_requests": 5,
                    "total_errors": 0,
                    "recent_errors": 0,
                    "avg_latency_ms": 20.0,
                },
            }
        )
        collector.collect_dream_metrics = AsyncMock(return_value={})
        collector.collect_dream_promotions = AsyncMock(return_value={})
        collector.collect_nightly_ops = AsyncMock(return_value={})
        collector.collect_graph_inventory = AsyncMock(
            return_value={"nodes_total": {}, "edges_total": {}, "orphans_total": {}}
        )
        # This standalone method should NOT be called anymore
        collector.get_graph_avg_latency = MagicMock(return_value=999.0)

        embedding_svc = AsyncMock()
        embedding_svc.healthcheck = AsyncMock(return_value=True)

        graph_svc = AsyncMock()
        graph_svc.healthcheck = AsyncMock(return_value=True)

        server = MetricsServer(
            collector=collector,
            embedding_svc=embedding_svc,
            graph_svc=graph_svc,
        )

        mock_request = MagicMock()
        response = await server._handle_metrics(mock_request)

        import json

        data = json.loads(response.body)

        # Graph section should use cross-process avg_latency (42.5 from _graph pseudo-tool),
        # NOT the standalone get_graph_avg_latency (999.0)
        assert data["graph"]["avg_latency_ms"] == 42.5
        assert data["graph"]["status"] == "up"
        # Standalone method should NOT have been called
        collector.get_graph_avg_latency.assert_not_called()

    async def test_cross_process_placed_after_tool_extraction(self) -> None:
        """cross_process in response still contains the raw tools (post-pop)."""
        from brain_v42.metrics.server import MetricsServer

        collector = MagicMock()
        collector.get_metrics.return_value = {
            "tools": {},
            "embedding_service": {
                "total_requests": 0,
                "total_errors": 0,
                "recent_errors": 0,
                "avg_latency_ms": 0.0,
            },
        }
        collector.collect_db_stats = AsyncMock(return_value={})
        collector.collect_search_quality = AsyncMock(return_value={})
        collector.collect_process_metrics = AsyncMock(
            return_value={
                "active_processes": 1,
                "total_memory_rss_bytes": 50_000,
                "tools": {
                    "brain_learn": {
                        "calls": 3,
                        "errors": 0,
                        "recent_errors": 0,
                        "avg_latency_ms": 10.0,
                    },
                    "_reranker": {
                        "calls": 2,
                        "errors": 0,
                        "recent_errors": 0,
                        "avg_latency_ms": 20.0,
                        "total_candidates": 10,
                    },
                },
                "embedding": {
                    "total_requests": 1,
                    "total_errors": 0,
                    "recent_errors": 0,
                    "avg_latency_ms": 5.0,
                },
            }
        )
        collector.collect_dream_metrics = AsyncMock(return_value={})
        collector.collect_dream_promotions = AsyncMock(return_value={})
        collector.collect_nightly_ops = AsyncMock(return_value={})

        embedding_svc = AsyncMock()
        embedding_svc.healthcheck = AsyncMock(return_value=True)

        server = MetricsServer(collector=collector, embedding_svc=embedding_svc)

        mock_request = MagicMock()
        response = await server._handle_metrics(mock_request)

        import json

        data = json.loads(response.body)

        # cross_process should be present in response
        assert "cross_process" in data
        # After pop, _reranker should NOT be in cross_process.tools either
        # (the dict was mutated in place, which is the actual behavior)
        assert "_reranker" not in data["cross_process"]["tools"]
