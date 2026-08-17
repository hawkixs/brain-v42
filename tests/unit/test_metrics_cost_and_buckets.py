"""Tests for Phase 2 latency-buckets + cost tracking on MetricsCollector."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from brain_v42.metrics.collector import MetricsCollector


@pytest.fixture
def collector() -> MetricsCollector:
    return MetricsCollector(engine=MagicMock(), session_factory=MagicMock())


def test_bucket_counts_initialize_at_zero(collector: MetricsCollector) -> None:
    buckets = collector.latency_buckets()
    expected_ranges = {"< 100ms", "100-300ms", "300-600ms", "600ms-1s", "1-2s", "> 2s"}
    assert {b["range"] for b in buckets} == expected_ranges
    assert all(b["count"] == 0 for b in buckets)


def test_bucket_carries_tone_for_red_monitor_ui(collector: MetricsCollector) -> None:
    """Each bucket carries a `tone` hint (ok/info/warn/crit) matching red-monitor's CSS tokens.

    red-monitor Brain.jsx:330 reads `b.tone` to color the distribution bars.
    """
    by_range = {b["range"]: b for b in collector.latency_buckets()}
    assert by_range["< 100ms"]["tone"] == "ok"
    assert by_range["100-300ms"]["tone"] == "info"
    assert by_range["300-600ms"]["tone"] == "warn"
    assert by_range["600ms-1s"]["tone"] == "warn"
    assert by_range["1-2s"]["tone"] == "crit"
    assert by_range["> 2s"]["tone"] == "crit"


def test_tool_call_increments_correct_bucket(collector: MetricsCollector) -> None:
    collector.record_tool_call("brain_search", latency_ms=50.0)
    collector.record_tool_call("brain_search", latency_ms=150.0)
    collector.record_tool_call("brain_search", latency_ms=400.0)
    collector.record_tool_call("brain_search", latency_ms=700.0)
    collector.record_tool_call("brain_search", latency_ms=1500.0)
    collector.record_tool_call("brain_search", latency_ms=3000.0)
    buckets = {b["range"]: b["count"] for b in collector.latency_buckets()}
    assert buckets["< 100ms"] == 1
    assert buckets["100-300ms"] == 1
    assert buckets["300-600ms"] == 1
    assert buckets["600ms-1s"] == 1
    assert buckets["1-2s"] == 1
    assert buckets["> 2s"] == 1


def test_record_cost_tracks_per_model(collector: MetricsCollector) -> None:
    collector.record_cost(model="sonnet-4.5", cost_usd=0.82)
    collector.record_cost(model="sonnet-4.5", cost_usd=1.18)
    collector.record_cost(model="haiku-4.5", cost_usd=0.05)
    by_model = collector.cost_by_model()
    assert by_model["sonnet-4.5"] == pytest.approx(2.00)
    assert by_model["haiku-4.5"] == pytest.approx(0.05)


def test_cost_total_equals_sum_across_models(collector: MetricsCollector) -> None:
    collector.record_cost(model="a", cost_usd=3.0)
    collector.record_cost(model="b", cost_usd=5.0)
    assert collector.cost_total() == pytest.approx(8.0)


def test_flush_data_exposes_cost_and_buckets(collector: MetricsCollector) -> None:
    collector.record_tool_call("brain_search", latency_ms=50.0)
    collector.record_cost(model="sonnet-4.5", cost_usd=1.5)
    data = collector.get_flush_data()
    # cost/buckets are process-global → live under the _process row (Task 2.1).
    assert "buckets" in data["_process"]
    assert "cost" in data["_process"]
    assert data["_process"]["cost"]["total"] == pytest.approx(1.5)
    assert data["_process"]["cost"]["by_model"]["sonnet-4.5"] == pytest.approx(1.5)


def test_flusher_includes_cost_in_tool_stats_jsonb() -> None:
    from brain_v42.metrics.flusher import MetricsFlusher

    collector = MetricsCollector(engine=MagicMock(), session_factory=MagicMock())
    collector.record_cost(model="sonnet-4.5", cost_usd=2.0)
    MetricsFlusher(collector=collector, session_factory=MagicMock(), flush_interval=30.0)
    data = collector.get_flush_data()
    assert data["_process"]["cost"]["total"] == pytest.approx(2.0)
