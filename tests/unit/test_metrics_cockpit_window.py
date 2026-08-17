"""Tests for MetricsCollector rolling-window percentiles + rate derivation."""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from brain_v42.metrics.collector import MetricsCollector


@pytest.fixture
def collector() -> MetricsCollector:
    return MetricsCollector(engine=MagicMock(), session_factory=MagicMock())


def test_tool_percentiles_returns_zero_without_samples(collector: MetricsCollector) -> None:
    p = collector.tool_percentiles(window_s=300)
    assert p == {"p50": 0.0, "p95": 0.0, "p99": 0.0}


def test_tool_percentiles_computed_from_recent_samples(collector: MetricsCollector) -> None:
    for latency in [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]:
        collector.record_tool_call("brain_search", latency_ms=float(latency))
    p = collector.tool_percentiles(window_s=300)
    assert p["p50"] == pytest.approx(55.0, abs=5.0)
    assert p["p95"] == pytest.approx(95.0, abs=5.0)
    assert p["p99"] == pytest.approx(99.0, abs=5.0)


def test_tool_percentiles_drops_samples_outside_window(
    collector: MetricsCollector, monkeypatch: pytest.MonkeyPatch
) -> None:
    t0 = time.time()
    monkeypatch.setattr("brain_v42.metrics.collector.time.time", lambda: t0 - 3600)
    collector.record_tool_call("brain_search", latency_ms=1000.0)
    monkeypatch.setattr("brain_v42.metrics.collector.time.time", lambda: t0)
    collector.record_tool_call("brain_search", latency_ms=10.0)
    p = collector.tool_percentiles(window_s=300)
    assert p["p95"] < 100.0


def test_retrieval_percentiles_uses_search_latencies(collector: MetricsCollector) -> None:
    for latency in [20, 40, 60, 80, 100, 200, 300]:
        collector.record_search_latency(float(latency))
    p = collector.retrieval_percentiles(window_s=86400)
    assert p["p50"] == pytest.approx(80.0, abs=20.0)
    assert p["p95"] == pytest.approx(300.0, abs=20.0)


def test_compute_rates_returns_zero_without_snapshots(collector: MetricsCollector) -> None:
    r = collector.compute_rates(window_s=60)
    assert r["rps"] == 0.0
    assert r["err_rate"] == 0.0


def test_compute_rates_derives_rps_from_snapshots(
    collector: MetricsCollector, monkeypatch: pytest.MonkeyPatch
) -> None:
    t0 = time.time()
    monkeypatch.setattr("brain_v42.metrics.collector.time.time", lambda: t0 - 60)
    for _ in range(100):
        collector.record_tool_call("brain_search", latency_ms=10.0)
    for _ in range(2):
        collector.record_tool_call("brain_search", latency_ms=10.0, error=True)
    collector.snapshot_counters()
    monkeypatch.setattr("brain_v42.metrics.collector.time.time", lambda: t0)
    for _ in range(58):
        collector.record_tool_call("brain_search", latency_ms=10.0)
    collector.record_tool_call("brain_search", latency_ms=10.0, error=True)
    collector.snapshot_counters()
    r = collector.compute_rates(window_s=60)
    assert r["rps"] == pytest.approx(59.0 / 60, abs=0.1)
    assert r["err_rate"] == pytest.approx(1.0 / 59, abs=0.05)


def test_recent_log_buffer_keeps_last_50(collector: MetricsCollector) -> None:
    for i in range(80):
        collector.push_recent_log("info", f"msg {i}")
    entries = collector.get_recent_log()
    assert len(entries) == 50
    assert entries[0]["msg"] == "msg 30"
    assert entries[-1]["msg"] == "msg 79"
    assert "t" in entries[0]
    assert entries[0]["lvl"] == "info"
