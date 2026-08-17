"""Task 2.1 — get_flush_data per-agent + `_process` shape (Plan 2, H7).

These tests pin the NEW get_flush_data() contract:
  - one entry per real agent, carrying ONLY a "tools" dict
  - a single "_process" entry carrying ONLY the process-global blocks
    (embedding/reranker/graph/cost/buckets/decay), never duplicated per agent.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from brain_v42.metrics.collector import MetricsCollector


@pytest.fixture
def collector() -> MetricsCollector:
    return MetricsCollector(engine=MagicMock(), session_factory=MagicMock())


def test_process_globals_isolated_to_process_row(collector: MetricsCollector) -> None:
    collector.record_tool_call("brain_search", 50, agent="red-shrik")
    collector.record_tool_call("brain_learn", 50, agent="red-codex")
    collector.record_embedding_request(20)  # process-global
    collector.record_reranker_call(5.0, 10)  # process-global

    fd = collector.get_flush_data()

    assert set(fd) >= {"red-shrik", "red-codex", "_process"}
    # Global blocks live ONLY under _process, never under a real agent.
    assert "embedding" in fd["_process"] and "embedding" not in fd["red-shrik"]
    assert "reranker" in fd["_process"] and "reranker" not in fd["red-codex"]
    # No pseudo-tool injection at the collector layer (that's the flusher, 3.2).
    assert "_reranker" not in fd["red-shrik"]["tools"]
    assert "_reranker" not in fd["red-codex"]["tools"]
    # Real agents carry ONLY their own tools, nothing else.
    assert fd["red-shrik"]["tools"].keys() == {"brain_search"}
    assert fd["red-codex"]["tools"].keys() == {"brain_learn"}
    assert set(fd["red-shrik"]) == {"tools"}
    assert set(fd["red-codex"]) == {"tools"}


def test_real_agent_tool_row_carries_full_stats(collector: MetricsCollector) -> None:
    collector.record_tool_call("brain_search", latency_ms=100.0, agent="red-shrik")
    collector.record_tool_call("brain_search", latency_ms=50.0, error=True, agent="red-shrik")

    tool = collector.get_flush_data()["red-shrik"]["tools"]["brain_search"]

    assert tool["calls"] == 2
    assert tool["errors"] == 1
    assert tool["recent_errors"] == 1
    assert tool["total_latency"] == 150.0


def test_stdio_calls_bucket_under_unknown(collector: MetricsCollector) -> None:
    # No agent kwarg → defaults to "unknown" (stdio path).
    collector.record_tool_call("brain_search", latency_ms=10.0)

    fd = collector.get_flush_data()

    assert "unknown" in fd
    assert fd["unknown"]["tools"]["brain_search"]["calls"] == 1


def test_recent_errors_attributed_to_every_agent_with_that_tool(
    collector: MetricsCollector,
) -> None:
    # Error-times are per-tool (not per-(agent,tool)); the same per-tool
    # recent_errors count is attached to each agent row holding that tool.
    collector.record_tool_call("brain_search", latency_ms=10.0, error=True, agent="red-shrik")
    collector.record_tool_call("brain_search", latency_ms=10.0, error=True, agent="red-codex")

    fd = collector.get_flush_data()

    assert fd["red-shrik"]["tools"]["brain_search"]["recent_errors"] == 2
    assert fd["red-codex"]["tools"]["brain_search"]["recent_errors"] == 2


def test_empty_collector_returns_only_process_with_zeroed_globals(
    collector: MetricsCollector,
) -> None:
    fd = collector.get_flush_data()

    assert set(fd) == {"_process"}
    proc = fd["_process"]
    assert proc["embedding"]["total_requests"] == 0
    assert proc["embedding"]["recent_errors"] == 0
    assert proc["reranker"]["total_calls"] == 0
    assert proc["graph"]["total_queries"] == 0
    assert proc["decay"]["stale_count"] == 0
    assert proc["cost"]["total"] == 0.0
    assert proc["cost"]["by_model"] == {}
    assert proc["buckets"]["< 100ms"] == 0
    # _process never carries a "tools" key from the collector (flusher adds it).
    assert "tools" not in proc


def test_reserved_process_agent_label_is_remapped(collector: MetricsCollector) -> None:
    # A spoofed x-brain-agent: _process must never collide with the globals row.
    collector.record_tool_call("brain_search", 50, agent="_process")

    fd = collector.get_flush_data()

    # Tool lands under the safe bucket, NOT under the real _process sentinel.
    assert "brain_search" in fd["_process_collision"]["tools"]
    assert "tools" not in fd["_process"]
    # The _process globals row is intact (embedding key present, not overwritten).
    assert "embedding" in fd["_process"]


def test_process_block_carries_all_global_subblocks(collector: MetricsCollector) -> None:
    collector.record_embedding_request(20)
    collector.record_reranker_call(5.0, 10)
    collector.record_graph_query(4.0)
    collector.record_decay_stats(stale_count=1, archived_count=2, access_log_size=3)
    collector.record_cost(model="sonnet-4.5", cost_usd=1.5)

    proc = collector.get_flush_data()["_process"]

    assert set(proc) == {"embedding", "reranker", "graph", "cost", "buckets", "decay"}
    assert proc["embedding"]["total_requests"] == 1
    assert proc["reranker"]["total_calls"] == 1
    assert proc["graph"]["total_queries"] == 1
    assert proc["decay"]["stale_count"] == 1
    assert proc["cost"]["total"] == pytest.approx(1.5)
    assert proc["cost"]["by_model"]["sonnet-4.5"] == pytest.approx(1.5)
