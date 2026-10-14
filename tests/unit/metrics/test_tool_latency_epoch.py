"""Tool observations must expose their window and counter reset boundary."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from brain_v42.metrics.collector import MetricsCollector
from brain_v42.metrics.collector_db import _DbCollectorsMixin
from brain_v42.metrics.flusher import MetricsFlusher
from brain_v42.metrics.server import MetricsServer


@pytest.fixture
def collector() -> Iterator[MetricsCollector]:
    with patch(
        "brain_v42.metrics.collector.get_settings",
        return_value=MagicMock(
            embedding_service_url="http://localhost:8003",
            embedding_backend="shim",
            embedding_model="test",
            embedding_dimension=1536,
        ),
    ):
        yield MetricsCollector(MagicMock(), MagicMock())


def test_tools_have_distinct_process_percentiles(collector: MetricsCollector) -> None:
    for latency in (10.0, 20.0, 30.0):
        collector.record_tool_call("search", latency, agent="first")
        collector.record_tool_call("search", latency, agent="second")
        collector.record_tool_call("learn", latency * 10)
    data = collector.get_flush_data()
    assert data["_process"]["tool_latency"] == {
        "search": {"p50": 20.0, "p95": 30.0, "p99": 30.0, "samples": 6},
        "learn": {"p50": 200.0, "p95": 300.0, "p99": 300.0, "samples": 3},
    }
    assert all("tool_latency" not in row for name, row in data.items() if name != "_process")
    assert "tool_latency" not in data["_process"]["embedding"]


def test_tool_samples_outside_window_are_ignored(collector: MetricsCollector) -> None:
    with patch("brain_v42.metrics.collector.time.time", return_value=1000.0):
        collector.record_tool_call("search", 999.0)
    with patch("brain_v42.metrics.collector.time.time", return_value=1301.0):
        collector.record_tool_call("search", 10.0)
        assert collector.get_flush_data()["_process"]["tool_latency"]["search"] == {
            "p50": 10.0,
            "p95": 10.0,
            "p99": 10.0,
            "samples": 1,
        }


def test_empty_tool_window_is_null(collector: MetricsCollector) -> None:
    with patch("brain_v42.metrics.collector.time.time", return_value=1000.0):
        collector.record_tool_call("search", 10.0)
    with patch("brain_v42.metrics.collector.time.time", return_value=1301.0):
        assert collector.get_flush_data()["_process"]["tool_latency"]["search"] == {
            "p50": None,
            "p95": None,
            "p99": None,
            "samples": 0,
        }


def test_local_tools_carry_the_same_percentile_keys(collector: MetricsCollector) -> None:
    """With no live process row the handler publishes these entries as `tools`.

    Their shape must not depend on liveness: a consumer reading `p95` gets a
    measured value or null, never a missing key.
    """
    for latency in (10.0, 20.0, 30.0):
        collector.record_tool_call("search", latency, agent="first")
    entry = collector.get_metrics()["tools"]["search"]
    assert {key: entry[key] for key in ("p50", "p95", "p99", "samples")} == {
        "p50": 20.0,
        "p95": 30.0,
        "p99": 30.0,
        "samples": 3,
    }


def test_tool_samples_are_bounded(collector: MetricsCollector) -> None:
    for _ in range(2001):
        collector.record_tool_call("search", 10.0)
    assert collector.get_flush_data()["_process"]["tool_latency"]["search"]["samples"] == 2000
    assert collector._tool_latency_by_name["search"].maxlen == 2000


async def test_tool_latency_survives_the_existing_flusher(collector: MetricsCollector) -> None:
    collector.record_tool_call("search", 42.0)
    session = AsyncMock()
    session.__aenter__.return_value = session
    session.__aexit__.return_value = None
    flusher = MetricsFlusher(collector, MagicMock(return_value=session))
    await flusher._flush()
    rows = []
    for call in session.execute.call_args_list:
        if len(call.args) > 1 and "agent_name" in call.args[1]:
            params = call.args[1]
            if params["agent_name"] == "_process":
                assert json.loads(params["tool_stats"])["_tool_latency"] == {
                    "search": {"p50": 42.0, "p95": 42.0, "p99": 42.0, "samples": 1}
                }
            assert "tool_latency" not in json.loads(params["embedding_stats"])
            rows.append(
                (
                    params["agent_name"],
                    params["pid"],
                    params["started_at"],
                    params["started_at"],
                    json.loads(params["tool_stats"]),
                    json.loads(params["embedding_stats"]),
                    0,
                    True,
                )
            )
    result = await _collect(rows)
    assert result["tools"]["search"]["p95"] == 42.0
    assert result["tools"]["search"]["samples"] == 1
    assert "_tool_latency" not in result["tools"]
    assert "tool_latency" not in result["embedding"]

    projected = MagicMock()
    projected.get_metrics.return_value = collector.get_metrics()
    projected.collect_process_metrics = AsyncMock(return_value=result)
    for method in (
        "collect_db_stats",
        "collect_search_quality",
        "collect_dream_metrics",
        "collect_nightly_ops",
        "collect_ticket_counts",
        "collect_session_slots",
    ):
        setattr(projected, method, AsyncMock(return_value={}))
    server = MetricsServer(projected, MagicMock(healthcheck=AsyncMock(return_value=True)))
    request = MagicMock(query={})
    payload = json.loads((await server._handle_metrics(request)).body)
    assert "tool_latency" not in payload["cross_process"]["embedding"]
    assert "tool_latency" not in payload["embedding_service"]
    assert "_tool_latency" not in payload["tools"]


async def test_flusher_refreshes_started_at_on_conflict(collector: MetricsCollector) -> None:
    collector.record_tool_call("search", 42.0, agent="first")
    collector.record_tool_call("search", 42.0, agent="second")
    session = AsyncMock()
    session.__aenter__.return_value = session
    session.__aexit__.return_value = None
    flusher = MetricsFlusher(collector, MagicMock(return_value=session))
    flusher._pid = 1
    flusher._started_at = _START.timestamp()
    await flusher._flush()

    upserts = [call for call in session.execute.call_args_list if len(call.args) > 1]
    assert {call.args[1]["agent_name"] for call in upserts} == {"first", "second", "_process"}
    for call in upserts:
        update = str(call.args[0]).split("ON CONFLICT (agent_name) DO UPDATE SET")[1]
        assert "started_at = :started_at" in update
        assert call.args[1]["started_at"] == _START
        assert call.args[1]["pid"] == 1


def _row(
    agent: str,
    pid: int,
    started_at: datetime,
    tools: dict[str, Any],
    live: bool = True,
    latency: dict[str, Any] | None = None,
) -> tuple[Any, ...]:
    if latency is not None:
        tools = {**tools, "_tool_latency": latency}
    return (agent, pid, started_at, started_at, tools, {}, 0, live)


async def _collect(rows: list[tuple[Any, ...]]) -> dict[str, Any]:
    session = AsyncMock()
    session.execute.return_value = MagicMock(all=MagicMock(return_value=rows))
    session.__aenter__.return_value = session
    session.__aexit__.return_value = None
    collector = _DbCollectorsMixin.__new__(_DbCollectorsMixin)
    collector._session_factory = MagicMock(return_value=session)
    result = await collector.collect_process_metrics()
    assert collector._block_health.health("cross_process")["status"] == "ok"
    return result


_START = datetime(2026, 10, 12, tzinfo=UTC)


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize(
    ("first", "second", "expected"),
    [
        (
            {"p50": 20.0, "p95": 50.0, "p99": 70.0, "samples": 3},
            {"p50": 10.0, "p95": 60.0, "p99": 80.0, "samples": 2},
            {"p50": 20.0, "p95": 60.0, "p99": 80.0, "samples": 5},
        ),
        (
            {"p50": None, "p95": None, "p99": None, "samples": 0},
            {"p50": 10.0, "p95": 60.0, "p99": 80.0, "samples": 2},
            {"p50": 10.0, "p95": 60.0, "p99": 80.0, "samples": 2},
        ),
        (
            {"p50": None, "p95": None, "p99": None, "samples": 0},
            {"p50": None, "p95": None, "p99": None, "samples": 0},
            {"p50": None, "p95": None, "p99": None, "samples": 0},
        ),
    ],
)
async def test_tool_percentiles_merge_none_safely(
    reverse: bool, first: dict[str, Any], second: dict[str, Any], expected: dict[str, Any]
) -> None:
    rows = [
        _row("agent", 1, _START, {"search": {"calls": 3}}),
        _row("_process", 1, _START, {}, latency={"search": first}),
        _row("_process", 2, _START, {}, latency={"search": second}),
    ]
    result = await _collect(list(reversed(rows)) if reverse else rows)
    assert {key: result["tools"]["search"][key] for key in expected} == expected
    assert "_tool_latency" not in result["tools"]


async def test_legacy_tools_always_have_latency_fields() -> None:
    result = await _collect([_row("agent", 1, _START, {"search": {"calls": 3}})])
    assert result["tools"]["search"] == {
        "calls": 3,
        "errors": 0,
        "recent_errors": 0,
        "avg_latency_ms": 0.0,
        "p50": None,
        "p95": None,
        "p99": None,
        "samples": 0,
    }


async def test_epoch_is_stable_for_same_contributing_set() -> None:
    rows = [
        _row("first", 1, _START, {"search": {"calls": 3}}),
        _row("second", 1, _START, {"search": {"calls": 2}}),
        _row("_process", 1, _START, {}),
    ]
    first = await _collect(rows)
    second = await _collect(list(reversed(rows)))
    assert first["counters"]["epoch"] == second["counters"]["epoch"]
    assert first["counters"]["processes"] == second["counters"]["processes"] == 1
    identities = [f"1:{_START.isoformat()}"]
    assert (
        first["counters"]["epoch"]
        == hashlib.sha256("\n".join(identities).encode()).hexdigest()[:16]
    )


async def test_epoch_changes_when_process_restarts() -> None:
    restarted_at = _START + timedelta(seconds=1)
    first = await _collect(
        [_row(agent, 1, _START, {}) for agent in ("_process", "first", "second")]
    )
    second = await _collect(
        [_row(agent, 1, restarted_at, {}) for agent in ("_process", "first", "second")]
    )
    assert first["counters"]["epoch"] != second["counters"]["epoch"]
    assert first["counters"]["since"] == _START.isoformat()
    assert second["counters"]["since"] == restarted_at.isoformat()
    assert first["counters"]["processes"] == second["counters"]["processes"] == 1


async def test_counters_describe_all_contributing_processes() -> None:
    latest = _START + timedelta(seconds=1)
    result = await _collect(
        [
            _row("agent", 1, _START, {}),
            _row("_process", 1, _START, {}),
            _row("idle", 2, latest, {}, live=False),
        ]
    )
    assert result["counters"]["since"] == latest.isoformat()
    assert result["counters"]["processes"] == 2
    assert result["counters"]["tool_latency_window_s"] == 300.0


async def test_empty_counters_have_no_since() -> None:
    result = await _collect([])
    assert result["counters"] == {
        "epoch": hashlib.sha256(b"").hexdigest()[:16],
        "since": None,
        "processes": 0,
        "tool_latency_window_s": 300.0,
    }


async def test_degraded_counters_keep_null_key_set() -> None:
    collector = _DbCollectorsMixin.__new__(_DbCollectorsMixin)
    collector._session_factory = MagicMock(side_effect=ConnectionError("offline"))
    result = await collector.collect_process_metrics()
    assert result["counters"] == dict.fromkeys(
        ("epoch", "since", "processes", "tool_latency_window_s")
    )
