"""Tests for MetricsCollector — in-memory counters and metrics assembly."""

from __future__ import annotations

import time
from collections import deque
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from brain_v42.metrics.collector import MetricsCollector


class TestToolRecording:
    def test_record_tool_call_increments_counter(self) -> None:
        collector = MetricsCollector(engine=MagicMock(), session_factory=MagicMock())
        collector.record_tool_call("brain_search", latency_ms=100.0)
        collector.record_tool_call("brain_search", latency_ms=200.0)

        # _tool_stats is now keyed [agent][tool]; default agent is "unknown"
        stats = collector._tool_stats["unknown"]["brain_search"]
        assert stats["calls"] == 2
        assert stats["errors"] == 0
        assert stats["total_latency"] == 300.0

    def test_record_tool_call_with_error(self) -> None:
        collector = MetricsCollector(engine=MagicMock(), session_factory=MagicMock())
        collector.record_tool_call("brain_learn", latency_ms=50.0, error=True)

        stats = collector._tool_stats["unknown"]["brain_learn"]
        assert stats["calls"] == 1
        assert stats["errors"] == 1


class TestEmbeddingRecording:
    def test_record_embedding_request(self) -> None:
        collector = MetricsCollector(engine=MagicMock(), session_factory=MagicMock())
        collector.record_embedding_request(latency_ms=25.0)
        collector.record_embedding_request(latency_ms=35.0, error=True)

        assert collector._embedding_stats["total_requests"] == 2
        assert collector._embedding_stats["total_errors"] == 1
        assert collector._embedding_stats["total_latency"] == 60.0

    def test_record_embedding_request_splits_gpu_busy_from_unreachable(self) -> None:
        """Errors are split into expected (gpu_busy contention) vs real outage
        (unreachable) so red-monitor can alert only on the second."""
        collector = MetricsCollector(engine=MagicMock(), session_factory=MagicMock())
        collector.record_embedding_request(latency_ms=10.0, error=True, error_kind="gpu_busy")
        collector.record_embedding_request(latency_ms=20.0, error=True, error_kind="unreachable")
        collector.record_embedding_request(latency_ms=30.0, error=True, error_kind="unreachable")
        collector.record_embedding_request(latency_ms=5.0, error=True)  # legacy / unknown kind

        assert collector._embedding_stats["total_errors"] == 4
        assert collector._embedding_stats["gpu_busy_errors"] == 1
        assert collector._embedding_stats["unreachable_errors"] == 2

    def test_record_embedding_request_no_error_does_not_bump_split_counters(self) -> None:
        collector = MetricsCollector(engine=MagicMock(), session_factory=MagicMock())
        collector.record_embedding_request(latency_ms=10.0)

        assert collector._embedding_stats["gpu_busy_errors"] == 0
        assert collector._embedding_stats["unreachable_errors"] == 0


class TestSearchRecording:
    def test_record_search_with_results(self) -> None:
        collector = MetricsCollector(engine=MagicMock(), session_factory=MagicMock())
        collector.record_search(similarity_score=0.85, result_count=5)
        collector.record_search(similarity_score=0.65, result_count=3)

        assert collector._search_stats["searches_total"] == 2
        assert collector._search_stats["searches_with_zero_results"] == 0
        assert collector._search_stats["total_score"] == pytest.approx(1.5)

    def test_record_search_zero_results(self) -> None:
        collector = MetricsCollector(engine=MagicMock(), session_factory=MagicMock())
        collector.record_search(similarity_score=0.0, result_count=0)

        assert collector._search_stats["searches_with_zero_results"] == 1


class TestRerankerRecording:
    def test_record_reranker_call(self) -> None:
        collector = MetricsCollector(engine=MagicMock(), session_factory=MagicMock())
        collector.record_reranker_call(latency_ms=15.0, candidate_count=10)
        collector.record_reranker_call(latency_ms=25.0, candidate_count=8)

        assert collector._reranker_stats["total_calls"] == 2
        assert collector._reranker_stats["total_errors"] == 0
        assert collector._reranker_stats["total_latency"] == 40.0
        assert collector._reranker_stats["total_candidates"] == 18

    def test_record_reranker_call_with_error(self) -> None:
        collector = MetricsCollector(engine=MagicMock(), session_factory=MagicMock())
        collector.record_reranker_call(latency_ms=5.0, candidate_count=3, error=True)

        assert collector._reranker_stats["total_calls"] == 1
        assert collector._reranker_stats["total_errors"] == 1


class TestRecentErrors:
    def test_tool_error_tracked_in_deque(self) -> None:
        collector = MetricsCollector(engine=MagicMock(), session_factory=MagicMock())
        collector.record_tool_call("brain_search", latency_ms=100.0, error=True)
        assert len(collector._tool_error_times.get("brain_search", [])) == 1

    def test_tool_no_error_no_deque_entry(self) -> None:
        collector = MetricsCollector(engine=MagicMock(), session_factory=MagicMock())
        collector.record_tool_call("brain_search", latency_ms=100.0, error=False)
        assert "brain_search" not in collector._tool_error_times

    def test_embedding_error_tracked_in_deque(self) -> None:
        collector = MetricsCollector(engine=MagicMock(), session_factory=MagicMock())
        collector.record_embedding_request(latency_ms=25.0, error=True)
        assert len(collector._embedding_error_times) == 1

    def test_reranker_error_tracked_in_deque(self) -> None:
        collector = MetricsCollector(engine=MagicMock(), session_factory=MagicMock())
        collector.record_reranker_call(latency_ms=10.0, candidate_count=5, error=True)
        assert len(collector._reranker_error_times) == 1

    def test_count_recent_within_window(self) -> None:
        collector = MetricsCollector(engine=MagicMock(), session_factory=MagicMock())
        collector.record_tool_call("brain_search", latency_ms=50.0, error=True)
        collector.record_tool_call("brain_search", latency_ms=50.0, error=True)
        count = collector._count_recent(collector._tool_error_times["brain_search"])
        assert count == 2

    def test_count_recent_purges_old_entries(self) -> None:
        collector = MetricsCollector(engine=MagicMock(), session_factory=MagicMock())
        collector.record_tool_call("brain_search", latency_ms=50.0, error=True)
        old_time = time.time() - 7200
        collector._tool_error_times["brain_search"].appendleft(old_time)
        count = collector._count_recent(collector._tool_error_times["brain_search"])
        assert count == 1

    def test_count_recent_empty_deque(self) -> None:
        collector = MetricsCollector(engine=MagicMock(), session_factory=MagicMock())
        count = collector._count_recent(deque())
        assert count == 0


def _mock_settings() -> MagicMock:
    s = MagicMock()
    s.embedding_service_url = "http://localhost:8003"
    s.embedding_dimension = 1024
    return s


@patch("brain_v42.metrics.collector.get_settings", return_value=_mock_settings())
class TestGetMetrics:
    def test_get_metrics_returns_complete_structure(self, _mock: MagicMock) -> None:
        collector = MetricsCollector(engine=MagicMock(), session_factory=MagicMock())
        collector.record_tool_call("brain_search", latency_ms=100.0)

        metrics = collector.get_metrics()

        assert "collected_at" in metrics
        assert "started_at" in metrics
        assert "version" in metrics
        assert "uptime_seconds" in metrics
        assert "memory_rss_bytes" in metrics
        assert "tools" in metrics
        assert "brain_search" in metrics["tools"]
        assert metrics["tools"]["brain_search"]["calls"] == 1
        assert metrics["tools"]["brain_search"]["avg_latency_ms"] == 100.0

    def test_get_metrics_embedding_service_avg_latency(self, _mock: MagicMock) -> None:
        collector = MetricsCollector(engine=MagicMock(), session_factory=MagicMock())
        collector.record_embedding_request(latency_ms=20.0)
        collector.record_embedding_request(latency_ms=40.0)

        metrics = collector.get_metrics()
        assert metrics["embedding_service"]["avg_latency_ms"] == pytest.approx(30.0)

    def test_get_metrics_search_quality_avg(self, _mock: MagicMock) -> None:
        collector = MetricsCollector(engine=MagicMock(), session_factory=MagicMock())
        collector.record_search(similarity_score=0.8, result_count=5)
        collector.record_search(similarity_score=0.6, result_count=3)

        metrics = collector.get_metrics()
        assert metrics["search_quality"]["avg_score"] == pytest.approx(0.7)

    def test_get_metrics_includes_reranker_stats(self, _mock: MagicMock) -> None:
        collector = MetricsCollector(engine=MagicMock(), session_factory=MagicMock())
        collector.record_reranker_call(latency_ms=10.0, candidate_count=5)
        collector.record_reranker_call(latency_ms=20.0, candidate_count=8)

        metrics = collector.get_metrics()
        assert "reranker" in metrics
        assert metrics["reranker"]["total_calls"] == 2
        assert metrics["reranker"]["total_errors"] == 0
        assert metrics["reranker"]["total_candidates"] == 13
        assert metrics["reranker"]["avg_latency_ms"] == pytest.approx(15.0)

    def test_get_metrics_reranker_defaults_when_no_calls(self, _mock: MagicMock) -> None:
        collector = MetricsCollector(engine=MagicMock(), session_factory=MagicMock())

        metrics = collector.get_metrics()
        assert metrics["reranker"]["total_calls"] == 0
        assert metrics["reranker"]["avg_latency_ms"] == 0.0

    def test_get_metrics_includes_recent_errors_for_tools(self, _mock: MagicMock) -> None:
        collector = MetricsCollector(engine=MagicMock(), session_factory=MagicMock())
        collector.record_tool_call("brain_search", latency_ms=100.0, error=True)
        collector.record_tool_call("brain_search", latency_ms=100.0, error=False)
        metrics = collector.get_metrics()
        assert metrics["tools"]["brain_search"]["recent_errors"] == 1
        assert metrics["tools"]["brain_search"]["errors"] == 1

    def test_get_metrics_includes_recent_errors_for_embedding(self, _mock: MagicMock) -> None:
        collector = MetricsCollector(engine=MagicMock(), session_factory=MagicMock())
        collector.record_embedding_request(latency_ms=25.0, error=True)
        metrics = collector.get_metrics()
        assert metrics["embedding_service"]["recent_errors"] == 1

    def test_get_metrics_exposes_embedding_error_split(self, _mock: MagicMock) -> None:
        collector = MetricsCollector(engine=MagicMock(), session_factory=MagicMock())
        collector.record_embedding_request(latency_ms=10.0, error=True, error_kind="gpu_busy")
        collector.record_embedding_request(latency_ms=20.0, error=True, error_kind="unreachable")
        metrics = collector.get_metrics()
        assert metrics["embedding_service"]["gpu_busy_errors"] == 1
        assert metrics["embedding_service"]["unreachable_errors"] == 1

    def test_get_metrics_includes_recent_errors_for_reranker(self, _mock: MagicMock) -> None:
        collector = MetricsCollector(engine=MagicMock(), session_factory=MagicMock())
        collector.record_reranker_call(latency_ms=10.0, candidate_count=5, error=True)
        metrics = collector.get_metrics()
        assert metrics["reranker"]["recent_errors"] == 1

    def test_get_metrics_recent_errors_zero_when_no_errors(self, _mock: MagicMock) -> None:
        collector = MetricsCollector(engine=MagicMock(), session_factory=MagicMock())
        collector.record_tool_call("brain_search", latency_ms=100.0)
        metrics = collector.get_metrics()
        assert metrics["tools"]["brain_search"]["recent_errors"] == 0
        assert metrics["embedding_service"]["recent_errors"] == 0
        assert metrics["reranker"]["recent_errors"] == 0

    def test_get_metrics_recent_errors_excludes_old_errors(self, _mock: MagicMock) -> None:
        collector = MetricsCollector(engine=MagicMock(), session_factory=MagicMock())
        collector.record_tool_call("brain_search", latency_ms=50.0, error=True)
        old_time = time.time() - 7200
        collector._tool_error_times["brain_search"].appendleft(old_time)
        metrics = collector.get_metrics()
        assert metrics["tools"]["brain_search"]["errors"] == 1
        assert metrics["tools"]["brain_search"]["recent_errors"] == 1

    def test_get_metrics_includes_decay_stats(self, _mock: MagicMock) -> None:
        collector = MetricsCollector(engine=MagicMock(), session_factory=MagicMock())
        collector.record_decay_stats(stale_count=12, archived_count=4, access_log_size=3000)
        metrics = collector.get_metrics()
        assert "decay" in metrics
        assert metrics["decay"]["stale_count"] == 12
        assert metrics["decay"]["archived_count"] == 4
        assert metrics["decay"]["access_log_size"] == 3000

    def test_get_metrics_decay_defaults_when_not_recorded(self, _mock: MagicMock) -> None:
        collector = MetricsCollector(engine=MagicMock(), session_factory=MagicMock())
        metrics = collector.get_metrics()
        assert metrics["decay"]["stale_count"] == 0
        assert metrics["decay"]["archived_count"] == 0
        assert metrics["decay"]["access_log_size"] == 0

    def test_get_metrics_includes_graph_stats(self, _mock: MagicMock) -> None:
        collector = MetricsCollector(engine=MagicMock(), session_factory=MagicMock())
        collector.record_graph_query(latency_ms=4.0)
        collector.record_graph_query(latency_ms=6.0)

        metrics = collector.get_metrics()
        assert "graph" in metrics
        assert metrics["graph"]["total_queries"] == 2
        assert metrics["graph"]["total_errors"] == 0
        assert metrics["graph"]["recent_errors"] == 0
        assert metrics["graph"]["avg_latency_ms"] == pytest.approx(5.0)

    def test_get_metrics_graph_defaults_when_no_queries(self, _mock: MagicMock) -> None:
        collector = MetricsCollector(engine=MagicMock(), session_factory=MagicMock())
        metrics = collector.get_metrics()
        assert "graph" in metrics
        assert metrics["graph"]["total_queries"] == 0
        assert metrics["graph"]["recent_errors"] == 0
        assert metrics["graph"]["avg_latency_ms"] == 0.0

    def test_get_metrics_includes_recent_errors_for_graph(self, _mock: MagicMock) -> None:
        collector = MetricsCollector(engine=MagicMock(), session_factory=MagicMock())
        collector.record_graph_query(latency_ms=5.0, error=True)
        collector.record_graph_query(latency_ms=3.0, error=True)
        collector.record_graph_query(latency_ms=2.0, error=False)

        metrics = collector.get_metrics()
        assert metrics["graph"]["total_errors"] == 2
        assert metrics["graph"]["recent_errors"] == 2


class TestGraphRecording:
    def test_record_graph_query_increments_counter(self) -> None:
        collector = MetricsCollector(engine=MagicMock(), session_factory=MagicMock())
        collector.record_graph_query(latency_ms=5.0)
        collector.record_graph_query(latency_ms=10.0)

        assert collector._graph_stats["total_queries"] == 2
        assert collector._graph_stats["total_errors"] == 0
        assert collector._graph_stats["total_latency"] == 15.0

    def test_record_graph_query_with_error(self) -> None:
        collector = MetricsCollector(engine=MagicMock(), session_factory=MagicMock())
        collector.record_graph_query(latency_ms=5.0, error=True)

        assert collector._graph_stats["total_queries"] == 1
        assert collector._graph_stats["total_errors"] == 1

    def test_record_graph_query_error_tracked_in_deque(self) -> None:
        collector = MetricsCollector(engine=MagicMock(), session_factory=MagicMock())
        collector.record_graph_query(latency_ms=5.0, error=True)
        collector.record_graph_query(latency_ms=3.0, error=False)

        assert len(collector._graph_error_times) == 1
        assert collector._graph_stats["total_errors"] == 1

    def test_graph_stats_default_zeros(self) -> None:
        collector = MetricsCollector(engine=MagicMock(), session_factory=MagicMock())
        assert collector._graph_stats["total_queries"] == 0
        assert collector._graph_stats["total_errors"] == 0
        assert collector._graph_stats["total_latency"] == 0.0


class TestDecayRecording:
    def test_record_decay_stats_stores_values(self) -> None:
        collector = MetricsCollector(engine=MagicMock(), session_factory=MagicMock())
        collector.record_decay_stats(stale_count=5, archived_count=3, access_log_size=1200)

        assert collector._decay_stats["stale_count"] == 5
        assert collector._decay_stats["archived_count"] == 3
        assert collector._decay_stats["access_log_size"] == 1200

    def test_record_decay_stats_overwrites_previous(self) -> None:
        collector = MetricsCollector(engine=MagicMock(), session_factory=MagicMock())
        collector.record_decay_stats(stale_count=5, archived_count=3, access_log_size=1200)
        collector.record_decay_stats(stale_count=10, archived_count=7, access_log_size=2500)

        assert collector._decay_stats["stale_count"] == 10
        assert collector._decay_stats["archived_count"] == 7
        assert collector._decay_stats["access_log_size"] == 2500

    def test_decay_stats_default_zeros(self) -> None:
        collector = MetricsCollector(engine=MagicMock(), session_factory=MagicMock())
        assert collector._decay_stats["stale_count"] == 0
        assert collector._decay_stats["archived_count"] == 0
        assert collector._decay_stats["access_log_size"] == 0


class TestGraphAvgLatency:
    @pytest.fixture()
    def collector(self) -> MetricsCollector:
        return MetricsCollector(engine=MagicMock(), session_factory=MagicMock())

    def test_get_graph_avg_latency_returns_zero_when_no_queries(
        self, collector: MetricsCollector
    ) -> None:
        """get_graph_avg_latency() must return 0.0 when no graph queries recorded."""
        assert collector.get_graph_avg_latency() == 0.0

    def test_get_graph_avg_latency_computes_average(self, collector: MetricsCollector) -> None:
        """get_graph_avg_latency() must return total_latency / total_queries."""
        collector.record_graph_query(10.0)
        collector.record_graph_query(20.0)
        collector.record_graph_query(30.0)
        assert collector.get_graph_avg_latency() == 20.0

    def test_get_graph_avg_latency_rounds_to_one_decimal(self, collector: MetricsCollector) -> None:
        """get_graph_avg_latency() must round to 1 decimal place."""
        collector.record_graph_query(10.0)
        collector.record_graph_query(10.0)
        collector.record_graph_query(11.0)
        # (10 + 10 + 11) / 3 = 10.333... → 10.3
        assert collector.get_graph_avg_latency() == 10.3


class TestGetFlushData:
    def test_get_flush_data_includes_tool_stats(self) -> None:
        collector = MetricsCollector(engine=MagicMock(), session_factory=MagicMock())
        collector.record_tool_call("brain_search", latency_ms=100.0)
        collector.record_tool_call("brain_search", latency_ms=50.0, error=True)
        data = collector.get_flush_data()
        # stdio calls bucket under "unknown" (Task 2.1 per-agent shape).
        tool = data["unknown"]["tools"]["brain_search"]
        assert tool["calls"] == 2
        assert tool["errors"] == 1
        assert tool["recent_errors"] == 1
        assert tool["total_latency"] == 150.0

    def test_get_flush_data_includes_embedding_stats(self) -> None:
        collector = MetricsCollector(engine=MagicMock(), session_factory=MagicMock())
        collector.record_embedding_request(latency_ms=25.0, error=True)
        collector.record_embedding_request(latency_ms=30.0)
        data = collector.get_flush_data()
        emb = data["_process"]["embedding"]
        assert emb["total_requests"] == 2
        assert emb["total_errors"] == 1
        assert emb["recent_errors"] == 1
        assert emb["total_latency"] == 55.0

    def test_get_flush_data_includes_embedding_error_split(self) -> None:
        collector = MetricsCollector(engine=MagicMock(), session_factory=MagicMock())
        collector.record_embedding_request(latency_ms=10.0, error=True, error_kind="gpu_busy")
        collector.record_embedding_request(latency_ms=20.0, error=True, error_kind="unreachable")
        data = collector.get_flush_data()
        emb = data["_process"]["embedding"]
        assert emb["gpu_busy_errors"] == 1
        assert emb["unreachable_errors"] == 1

    def test_get_flush_data_empty_collector(self) -> None:
        collector = MetricsCollector(engine=MagicMock(), session_factory=MagicMock())
        data = collector.get_flush_data()
        # No real-agent entries; only the _process row with zeroed globals.
        assert set(data) == {"_process"}
        assert data["_process"]["embedding"]["total_requests"] == 0
        assert data["_process"]["embedding"]["recent_errors"] == 0

    def test_get_flush_data_includes_decay_stats(self) -> None:
        collector = MetricsCollector(engine=MagicMock(), session_factory=MagicMock())
        collector.record_decay_stats(stale_count=5, archived_count=3, access_log_size=1200)
        data = collector.get_flush_data()
        decay = data["_process"]["decay"]
        assert decay["stale_count"] == 5
        assert decay["archived_count"] == 3
        assert decay["access_log_size"] == 1200

    def test_get_flush_data_decay_stats_defaults(self) -> None:
        collector = MetricsCollector(engine=MagicMock(), session_factory=MagicMock())
        data = collector.get_flush_data()
        decay = data["_process"]["decay"]
        assert decay["stale_count"] == 0
        assert decay["archived_count"] == 0
        assert decay["access_log_size"] == 0

    def test_get_flush_data_includes_graph_stats(self) -> None:
        collector = MetricsCollector(engine=MagicMock(), session_factory=MagicMock())
        collector.record_graph_query(latency_ms=4.0)
        collector.record_graph_query(latency_ms=6.0, error=True)

        data = collector.get_flush_data()
        graph = data["_process"]["graph"]
        assert graph["total_queries"] == 2
        assert graph["total_errors"] == 1
        assert graph["recent_errors"] == 1
        assert graph["total_latency"] == 10.0

    def test_get_flush_data_graph_stats_defaults(self) -> None:
        collector = MetricsCollector(engine=MagicMock(), session_factory=MagicMock())
        data = collector.get_flush_data()
        graph = data["_process"]["graph"]
        assert graph["total_queries"] == 0
        assert graph["total_errors"] == 0
        assert graph["recent_errors"] == 0
        assert graph["total_latency"] == 0.0


@patch("brain_v42.metrics.collector.get_settings", return_value=_mock_settings())
class TestPoolStats:
    """Pool stats must reflect configured max + real overflow, not SQLAlchemy internals."""

    def _collector_with_pool(
        self, *, checkedout: int, checkedin: int, size: int, overflow: int, max_overflow: int
    ) -> MetricsCollector:
        engine = MagicMock()
        pool = engine.sync_engine.pool
        pool.checkedout.return_value = checkedout
        pool.checkedin.return_value = checkedin
        pool.size.return_value = size
        pool.overflow.return_value = overflow
        pool._max_overflow = max_overflow
        session_factory = MagicMock(side_effect=RuntimeError("db unavailable"))
        return MetricsCollector(engine=engine, session_factory=session_factory)

    @pytest.mark.asyncio
    async def test_max_reports_configured_capacity_not_current_usage(
        self, _mock: MagicMock
    ) -> None:
        """max must be pool_size + max_overflow (configured), not current connections."""
        collector = self._collector_with_pool(
            checkedout=0, checkedin=2, size=5, overflow=-3, max_overflow=10
        )
        stats = (await collector.collect_db_stats())["pool"]
        assert stats["max"] == 15

    @pytest.mark.asyncio
    async def test_overflow_is_never_negative(self, _mock: MagicMock) -> None:
        """overflow clamps to >=0 when pool is underutilized (SQLA returns negative)."""
        collector = self._collector_with_pool(
            checkedout=0, checkedin=2, size=5, overflow=-3, max_overflow=10
        )
        stats = (await collector.collect_db_stats())["pool"]
        assert stats["overflow"] == 0

    @pytest.mark.asyncio
    async def test_overflow_reports_real_surplus_above_pool_size(self, _mock: MagicMock) -> None:
        """When connections exceed pool_size, overflow reflects the surplus."""
        collector = self._collector_with_pool(
            checkedout=7, checkedin=0, size=5, overflow=2, max_overflow=10
        )
        stats = (await collector.collect_db_stats())["pool"]
        assert stats["overflow"] == 2
        assert stats["max"] == 15


@patch("brain_v42.metrics.collector.get_settings", return_value=_mock_settings())
class TestEmbeddingBacklogMetrics:
    @pytest.mark.asyncio
    async def test_collect_db_stats_exposes_count_and_oldest_age_by_type(
        self, _mock: MagicMock
    ) -> None:
        session = AsyncMock()
        table_rows = [
            (10, 8, 2, 3600.0),
            (20, 17, 3, 7200.0),
            (5, 5, 0, 0.0),
            (4, 4, 0, 0.0),
            (3, 2, 1, 60.0),
        ]
        results: list[MagicMock] = []
        for row in table_rows:
            result = MagicMock()
            result.one.return_value = row
            results.append(result)
        for scalar in [1, 1234, 0, 0, 0, 0, 0]:
            result = MagicMock()
            result.scalar.return_value = scalar
            results.append(result)
        worker_result = MagicMock()
        worker_result.all.return_value = [
            ("embedding_backfill.attempted", 8.0),
            ("embedding_backfill.stored", 6.0),
            ("embedding_backfill.timed_out", 1.0),
            ("embedding_backfill.unavailable.gpu_busy", 1.0),
        ]
        results.append(worker_result)
        outbox_result = MagicMock()
        outbox_result.one.return_value = (0, 0, 0, 0, 0.0, 1, False, False, False)
        results.append(outbox_result)
        session.execute = AsyncMock(side_effect=results)

        @asynccontextmanager
        async def session_context():
            yield session

        engine = MagicMock()
        engine.sync_engine.pool.size.return_value = 5
        engine.sync_engine.pool.checkedout.return_value = 0
        engine.sync_engine.pool.checkedin.return_value = 1
        engine.sync_engine.pool.overflow.return_value = -4
        engine.sync_engine.pool._max_overflow = 10
        collector = MetricsCollector(engine=engine, session_factory=session_context)

        database = await collector.collect_db_stats()

        assert database["embedding_backlog"]["total"] == 6
        assert database["embedding_backlog"]["by_entity_type"]["decision"] == {
            "count": 2,
            "oldest_age_seconds": 3600.0,
        }
        assert database["embedding_backlog"]["by_entity_type"]["learning"]["count"] == 3
        assert database["embedding_backlog"]["by_entity_type"]["adr"]["oldest_age_seconds"] == 60.0
        assert database["embedding_backlog"]["worker_last_24h"] == {
            "attempted": 8,
            "stored": 6,
            "stale": 0,
            "missing": 0,
            "unavailable": 0,
            "timed_out": 1,
            "failed": 0,
            "unavailable_by_kind": {"gpu_busy": 1},
        }


@patch("brain_v42.metrics.collector.get_settings", return_value=_mock_settings())
class TestGraphOutboxMetrics:
    @pytest.mark.asyncio
    async def test_collect_db_stats_exposes_graph_outbox_go_no_go_gauges(
        self, _mock: MagicMock
    ) -> None:
        session = AsyncMock()
        results: list[MagicMock] = []

        for row in [(0, 0, 0, 0.0)] * 5:
            result = MagicMock()
            result.one.return_value = row
            results.append(result)

        for scalar in [0, 0, 0, 0, 0, 0, 0]:
            result = MagicMock()
            result.scalar.return_value = scalar
            results.append(result)

        worker_result = MagicMock()
        worker_result.all.return_value = []
        results.append(worker_result)

        outbox_result = MagicMock()
        outbox_result.one.return_value = (7, 4, 2, 1, 125.5, 9, True, True, False)
        results.append(outbox_result)
        session.execute = AsyncMock(side_effect=results)

        @asynccontextmanager
        async def session_context():
            yield session

        engine = MagicMock()
        engine.sync_engine.pool.size.return_value = 5
        engine.sync_engine.pool.checkedout.return_value = 0
        engine.sync_engine.pool.checkedin.return_value = 1
        engine.sync_engine.pool.overflow.return_value = -4
        engine.sync_engine.pool._max_overflow = 10
        collector = MetricsCollector(engine=engine, session_factory=session_context)

        database = await collector.collect_db_stats()

        assert database["graph_outbox"] == {
            "available": True,
            "pending": 7,
            "ready": 4,
            "claimed": 2,
            "exhausted": 1,
            "oldest_pending_age_seconds": 125.5,
            "projector": {
                "generation": 9,
                "armed": True,
                "lease_active": True,
                "recovery_active": False,
                "healthy": True,
            },
        }

        outbox_query = str(session.execute.await_args_list[-1].args[0]).lower()
        assert "with observed as materialized" in outbox_query
        assert outbox_query.count("clock_timestamp()") == 1
        assert "from graph_outbox where delivered_at is null" in outbox_query
        assert "last_error_code = 'max_attempts'" in outbox_query
        assert "from graph_projection_leases" in outbox_query

    @pytest.mark.asyncio
    async def test_graph_outbox_collection_failure_preserves_other_database_metrics(
        self, _mock: MagicMock
    ) -> None:
        session = AsyncMock()
        results: list[MagicMock | Exception] = []

        for row in [(10, 8, 2, 3600.0), *((0, 0, 0, 0.0),) * 4]:
            result = MagicMock()
            result.one.return_value = row
            results.append(result)

        for scalar in [1, 1234, 0, 0, 0, 0, 0]:
            result = MagicMock()
            result.scalar.return_value = scalar
            results.append(result)

        worker_result = MagicMock()
        worker_result.all.return_value = []
        results.append(worker_result)
        results.append(PermissionError("graph metrics denied"))
        session.execute = AsyncMock(side_effect=results)

        @asynccontextmanager
        async def session_context():
            yield session

        engine = MagicMock()
        engine.sync_engine.pool.size.return_value = 5
        engine.sync_engine.pool.checkedout.return_value = 0
        engine.sync_engine.pool.checkedin.return_value = 1
        engine.sync_engine.pool.overflow.return_value = -4
        engine.sync_engine.pool._max_overflow = 10
        collector = MetricsCollector(engine=engine, session_factory=session_context)

        database = await collector.collect_db_stats()

        assert database["tables"]["decisions"]["rows"] == 10
        assert database["embedding_backlog"]["total"] == 2
        assert database["graph_outbox"]["available"] is False
        assert database["graph_outbox"]["projector"]["healthy"] is False


class TestCollectGraphInventory:
    """Inventory: nodes per label, edges per type, PG↔Neo4j orphan estimate.

    Orphans are computed as ``max(0, pg_count - neo4j_count)`` per (PG table,
    Neo4j label) pair — a coarse drift signal rather than an exhaustive id
    diff. Negative deltas (more nodes than rows) bucket as 0.
    """

    @staticmethod
    def _make_graph_svc(*, nodes: dict[str, int], edges: dict[str, int]) -> MagicMock:
        graph = MagicMock()
        graph.count_nodes_by_label = AsyncMock(return_value=nodes)
        graph.count_edges_by_type = AsyncMock(return_value=edges)
        return graph

    @pytest.mark.asyncio
    async def test_returns_skipped_when_graph_svc_is_none(self) -> None:
        collector = MetricsCollector(engine=MagicMock(), session_factory=MagicMock())
        result = await collector.collect_graph_inventory(graph_svc=None)
        assert result == {"status": "disabled"}

    @pytest.mark.asyncio
    async def test_returns_node_and_edge_counts(self) -> None:
        graph = self._make_graph_svc(
            nodes={"Decision": 12, "Learning": 47, "ADR": 5},
            edges={"RELATED_TO": 89, "SUPERSEDES": 3},
        )
        collector = MetricsCollector(
            engine=MagicMock(),
            session_factory=MagicMock(side_effect=RuntimeError("db unavailable")),
        )
        result = await collector.collect_graph_inventory(graph_svc=graph)

        assert result["nodes_total"] == {"Decision": 12, "Learning": 47, "ADR": 5}
        assert result["edges_total"] == {"RELATED_TO": 89, "SUPERSEDES": 3}

    @pytest.mark.asyncio
    async def test_orphans_unknown_when_pg_unavailable(self) -> None:
        graph = self._make_graph_svc(nodes={"Decision": 5}, edges={})
        collector = MetricsCollector(
            engine=MagicMock(),
            session_factory=MagicMock(side_effect=RuntimeError("db unavailable")),
        )
        result = await collector.collect_graph_inventory(graph_svc=graph)
        assert result["orphans_total"] == {}

    @pytest.mark.asyncio
    async def test_handles_graph_svc_error(self) -> None:
        graph = MagicMock()
        graph.count_nodes_by_label = AsyncMock(side_effect=Exception("neo4j down"))
        graph.count_edges_by_type = AsyncMock(side_effect=Exception("neo4j down"))
        collector = MetricsCollector(
            engine=MagicMock(),
            session_factory=MagicMock(side_effect=RuntimeError("db unavailable")),
        )
        result = await collector.collect_graph_inventory(graph_svc=graph)

        assert result["nodes_total"] == {}
        assert result["edges_total"] == {}
        assert result["status"] == "error"
