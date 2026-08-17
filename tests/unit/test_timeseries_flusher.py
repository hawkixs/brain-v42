"""Tests for the 30min/1h bucketed flush of rps/p95/err_rate/cost."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from brain_v42.metrics.collector import MetricsCollector
from brain_v42.metrics.timeseries_flusher import TimeseriesFlusher, _bucket_floor


def test_bucket_floor_rounds_down_to_30min() -> None:
    ts = datetime(2026, 4, 19, 14, 47, 38, tzinfo=UTC)
    assert _bucket_floor(ts, 1800) == datetime(2026, 4, 19, 14, 30, tzinfo=UTC)


def test_bucket_floor_rounds_down_to_hour() -> None:
    ts = datetime(2026, 4, 19, 14, 47, 38, tzinfo=UTC)
    assert _bucket_floor(ts, 3600) == datetime(2026, 4, 19, 14, 0, tzinfo=UTC)


@pytest.fixture
def collector() -> MetricsCollector:
    c = MetricsCollector(engine=MagicMock(), session_factory=MagicMock())
    for latency in [100, 200, 300]:
        c.record_tool_call("brain_search", latency_ms=float(latency))
    c.snapshot_counters()
    for latency in [150, 250]:
        c.record_tool_call("brain_search", latency_ms=float(latency))
    c.snapshot_counters()
    c.record_cost(model="sonnet-4.5", cost_usd=1.5)
    return c


@pytest.fixture
def session_factory() -> MagicMock:
    session = AsyncMock()
    session.__aenter__.return_value = session
    session.__aexit__.return_value = None
    factory = MagicMock()
    factory.return_value = session
    return factory


async def test_flush_upserts_30min_metrics(
    collector: MetricsCollector, session_factory: MagicMock
) -> None:
    flusher = TimeseriesFlusher(collector=collector, session_factory=session_factory)
    await flusher._flush_30min_metrics()
    calls = session_factory.return_value.execute.call_args_list
    metrics_inserted = [
        c.args[1].get("metric") for c in calls if len(c.args) >= 2 and isinstance(c.args[1], dict)
    ]
    assert "rps" in metrics_inserted
    assert "p95" in metrics_inserted
    assert "err_rate" in metrics_inserted


async def test_flush_upserts_cost_bucket_hourly(
    collector: MetricsCollector, session_factory: MagicMock
) -> None:
    flusher = TimeseriesFlusher(collector=collector, session_factory=session_factory)
    await flusher._flush_cost_bucket()
    calls = session_factory.return_value.execute.call_args_list
    metrics_inserted = [
        c.args[1].get("metric") for c in calls if len(c.args) >= 2 and isinstance(c.args[1], dict)
    ]
    assert "cost" in metrics_inserted


async def test_retention_deletes_rows_older_than_7_days(
    collector: MetricsCollector, session_factory: MagicMock
) -> None:
    flusher = TimeseriesFlusher(collector=collector, session_factory=session_factory)
    await flusher._retention_sweep()
    exec_calls = session_factory.return_value.execute.call_args_list
    assert any(
        "DELETE FROM metrics_timeseries" in str(c.args[0]) and "'7 days'" in str(c.args[0])
        for c in exec_calls
    )
