"""Failures must remain distinguishable from measured zeros in sidecar payloads."""

from __future__ import annotations

import json
import time
from collections import deque
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from brain_v42.metrics.cockpit import CockpitCollector
from brain_v42.metrics.collector import MetricsCollector
from brain_v42.metrics.collector_db import _DbCollectorsMixin
from brain_v42.metrics.memory_stats import collect_memory_stats
from brain_v42.metrics.server import MetricsServer
from brain_v42.metrics.slow_block_cache import CollectorDegraded, SlowBlockCache
from brain_v42.metrics.timeseries_flusher import TimeseriesFlusher

_MEMORY_KEYS = {
    "episodes",
    "episodic_mb",
    "semantic_chunks",
    "semantic_mb",
    "lastCompaction",
    "freedLastCompaction",
    "vectorIndex",
}
_DB_KEYS = {
    "pool",
    "tables",
    "db_size_bytes",
    "dimension_mismatches",
    "embedding_backlog",
    "graph_outbox",
}
_SEARCH_KEYS = {"searches_total", "searches_with_zero_results", "avg_score"}
_PROCESS_KEYS = {
    "active_processes",
    "active_agents",
    "total_memory_rss_bytes",
    "tools",
    "mcp_auth_refused",
    "elevation_refused",
    "mcp_auth_agent_unresolved",
    "decay",
    "embedding_identity",
    "embedding",
    "by_agent",
}


@pytest.fixture(autouse=True)
def settings(monkeypatch: pytest.MonkeyPatch) -> None:
    value = MagicMock(
        embedding_dimension=1536,
        embedding_backend="shim",
        embedding_model="qodo",
        embedding_service_url="http://localhost:8003",
        brain_mcp_transport="http",
    )
    monkeypatch.setattr("brain_v42.metrics.collector.get_settings", lambda: value)
    monkeypatch.setattr("brain_v42.metrics.cockpit.get_settings", lambda: value)


def _factory() -> MagicMock:
    session = AsyncMock()
    session.__aenter__.return_value = session
    session.__aexit__.return_value = None

    def result(query: Any, *args: Any) -> MagicMock:
        row = (None, 0) if "MAX(created_at)" in str(query) else (0, 0, 0, 0)
        return MagicMock(
            one=MagicMock(return_value=row),
            scalar=MagicMock(return_value=0),
            all=MagicMock(return_value=[]),
            fetchall=MagicMock(return_value=[]),
        )

    session.execute.side_effect = result
    return MagicMock(return_value=session)


def _collector(factory: MagicMock) -> MetricsCollector:
    engine = MagicMock()
    pool = engine.sync_engine.pool
    pool.size.return_value = 5
    pool._max_overflow = 10
    pool.checkedout.return_value = 2
    pool.checkedin.return_value = 3
    pool.overflow.return_value = 0
    return MetricsCollector(engine=engine, session_factory=factory)


def _assert_health(entry: dict[str, Any], status: str, error_type: str | None) -> None:
    assert set(entry) == {"status", "error_type", "as_of", "last_ok_at"}
    assert entry["status"] == status
    assert entry["error_type"] == error_type
    assert datetime.fromisoformat(entry["as_of"]).utcoffset() == timedelta(0)


async def test_database_failure_keeps_pool_and_nulls_source_fields() -> None:
    factory = _factory()
    factory.side_effect = ConnectionError("offline")
    collector = _collector(factory)
    block = await collector.collect_db_stats()
    assert set(block) == _DB_KEYS
    assert block["tables"] is None
    assert block["db_size_bytes"] is None
    assert block["dimension_mismatches"] is None
    assert block["embedding_backlog"]["total"] is None
    assert block["embedding_backlog"]["by_entity_type"] is None
    assert "worker_last_24h" in block["embedding_backlog"]
    assert block["pool"] == {"active": 2, "idle": 3, "overflow": 0, "max": 15}
    assert block["graph_outbox"]["pending"] is None
    _assert_health(collector.block_health("database"), "degraded", "ConnectionError")
    assert collector.block_health("database")["last_ok_at"] is None


async def test_search_failure_preserves_keys_with_nulls() -> None:
    factory = _factory()
    factory.side_effect = ConnectionError("offline")
    collector = _collector(factory)
    block = await collector.collect_search_quality()
    assert set(block) == _SEARCH_KEYS
    assert all(value is None for value in block.values())
    _assert_health(collector.block_health("search_quality"), "degraded", "ConnectionError")


async def test_search_session_exit_failure_does_not_record_a_success() -> None:
    factory = _factory()
    factory.return_value.__aexit__.side_effect = TimeoutError("closing session")
    collector = _collector(factory)
    await collector.collect_search_quality()
    health = collector.block_health("search_quality")
    _assert_health(health, "degraded", "TimeoutError")
    assert health["last_ok_at"] is None


async def test_process_failure_preserves_keys_with_nulls() -> None:
    factory = _factory()
    factory.side_effect = ConnectionError("offline")
    collector = _collector(factory)
    block = await collector.collect_process_metrics()
    assert set(block) == _PROCESS_KEYS
    for key in _PROCESS_KEYS - {"embedding", "decay"}:
        assert block[key] is None
    assert all(value is None for value in block["decay"].values())
    assert set(block["embedding"]) == {
        "total_requests",
        "total_errors",
        "gpu_busy_errors",
        "unreachable_errors",
        "recent_errors",
        "avg_latency_ms",
        "usage",
    }
    for key, value in block["embedding"].items():
        if key != "usage":
            assert value is None
    assert block["embedding"]["usage"] == {
        "read": {"total_tokens": None, "reported_requests": None},
        "write": {"total_tokens": None, "reported_requests": None},
    }
    _assert_health(collector.block_health("cross_process"), "degraded", "ConnectionError")


@pytest.mark.parametrize("fail", [False, True])
async def test_db_mixin_can_collect_without_metrics_collector_init(fail: bool) -> None:
    factory = _factory()
    if fail:
        factory.side_effect = ConnectionError("offline")
    mixin = _DbCollectorsMixin()
    mixin._session_factory = factory
    block = await mixin.collect_process_metrics()
    assert block["active_processes"] == (None if fail else 0)


async def test_memory_failure_preserves_keys_with_nulls() -> None:
    factory = _factory()
    factory.side_effect = ConnectionError("offline")
    memory = await collect_memory_stats(factory)
    assert set(memory) == _MEMORY_KEYS
    assert all(value is None for value in memory.values())
    cockpit = CockpitCollector(_collector(factory), factory)
    payload = await cockpit.snapshot()
    _assert_health(payload["block_status"]["memory"], "degraded", "ConnectionError")
    assert payload["block_status"]["memory"]["last_ok_at"] is None


@pytest.mark.parametrize(
    ("method", "key"),
    [
        ("collect_db_stats", "database"),
        ("collect_search_quality", "search_quality"),
        ("collect_process_metrics", "cross_process"),
    ],
)
async def test_collector_failure_remembers_last_success(method: str, key: str) -> None:
    factory = _factory()
    collector = _collector(factory)
    success = await getattr(collector, method)()
    health = collector.block_health(key)
    _assert_health(health, "ok", None)
    assert health["last_ok_at"] == health["as_of"]
    factory.side_effect = TimeoutError("offline")
    failed = await getattr(collector, method)()
    assert set(failed) == set(success)
    degraded = collector.block_health(key)
    _assert_health(degraded, "degraded", "TimeoutError")
    assert degraded["last_ok_at"] == health["last_ok_at"]
    assert degraded["as_of"] >= health["as_of"]


async def test_memory_failure_remembers_last_success() -> None:
    factory = _factory()
    cockpit = CockpitCollector(_collector(factory), factory)
    success = await cockpit.snapshot()
    health = success["block_status"]["memory"]
    _assert_health(health, "ok", None)
    assert health["last_ok_at"] == health["as_of"]
    factory.side_effect = TimeoutError("offline")
    cockpit._cache_ts = 0
    failed = await cockpit.snapshot()
    assert set(failed["memory"]) == set(success["memory"])
    degraded = failed["block_status"]["memory"]
    _assert_health(degraded, "degraded", "TimeoutError")
    assert degraded["last_ok_at"] == health["last_ok_at"]


async def test_memory_invalid_result_is_degraded_without_advancing_last_success() -> None:
    factory = _factory()
    cockpit = CockpitCollector(_collector(factory), factory)
    success = await cockpit.snapshot()
    last_ok = success["block_status"]["memory"]["last_ok_at"]
    factory.return_value.execute.side_effect = [
        MagicMock(one=MagicMock(return_value=("invalid", 0))),
        MagicMock(one=MagicMock(return_value=(0, 0))),
        MagicMock(one=MagicMock(return_value=(None, 0))),
        MagicMock(scalar=MagicMock(return_value=None)),
    ]
    cockpit._cache_ts = 0
    failed = await cockpit.snapshot()
    assert failed["memory"] == dict.fromkeys(_MEMORY_KEYS)
    health = failed["block_status"]["memory"]
    _assert_health(health, "degraded", "ValueError")
    assert health["last_ok_at"] == last_ok


async def test_slow_cache_success_health_matches_generated_at() -> None:
    cache = SlowBlockCache(ttl_seconds=30, error_ttl_seconds=5)
    result = await cache.get("dream", AsyncMock(return_value={"count": 2}))
    health = cache.health("dream")
    _assert_health(health, "ok", None)
    assert health["as_of"] == result["generated_at"]
    assert health["last_ok_at"] == health["as_of"]


@pytest.mark.parametrize("with_cause", [False, True])
async def test_slow_cache_degraded_payload_keeps_shape_and_health(with_cause: bool) -> None:
    cache = SlowBlockCache(ttl_seconds=30, error_ttl_seconds=5)
    payload = {"partial": True}
    degraded = CollectorDegraded(payload)
    if with_cause:
        degraded.__cause__ = ConnectionError("offline")
    result = await cache.get("nightly", AsyncMock(side_effect=degraded))
    assert result == {**payload, "generated_at": result["generated_at"]}
    assert payload == {"partial": True}
    health = cache.health("nightly")
    _assert_health(health, "degraded", "ConnectionError" if with_cause else "CollectorDegraded")
    assert health["as_of"] == result["generated_at"]
    assert health["last_ok_at"] is None


async def test_slow_cache_plain_exception_remembers_last_success_and_short_ttl() -> None:
    now = [0.0]
    wall = [datetime(2026, 10, 13, tzinfo=UTC)]
    cache = SlowBlockCache(
        ttl_seconds=30,
        error_ttl_seconds=5,
        clock=lambda: now[0],
        wall_clock=lambda: wall[0],
    )
    compute = AsyncMock(return_value={"count": 2})
    await cache.get("tickets", compute)
    last_ok = cache.health("tickets")["last_ok_at"]
    now[0] = 31
    wall[0] += timedelta(seconds=31)
    compute.side_effect = TimeoutError("offline")
    assert await cache.get("tickets", compute) is None
    health = cache.health("tickets")
    _assert_health(health, "degraded", "TimeoutError")
    assert health["last_ok_at"] == last_ok
    assert health["as_of"] == wall[0].isoformat()
    now[0] = 32
    wall[0] += timedelta(seconds=1)
    assert await cache.get("tickets", compute) is None
    assert cache.health("tickets") == health
    assert compute.await_count == 2
    now[0] = 37
    compute.side_effect = None
    await cache.get("tickets", compute)
    assert cache.health("tickets")["status"] == "ok"
    assert cache.health("tickets")["last_ok_at"] != last_ok


def test_slow_cache_unknown_key_is_not_measured() -> None:
    cache = SlowBlockCache(ttl_seconds=30, error_ttl_seconds=5)
    health = cache.health("unknown")
    _assert_health(health, "not_measured", None)
    assert health["last_ok_at"] is None


@pytest.mark.parametrize("graph_configured", [False, True])
async def test_metrics_failure_keeps_payload_keys_and_exposes_all_health(
    graph_configured: bool,
) -> None:
    factory = _factory()
    collector = _collector(factory)
    for method in (
        "collect_dream_metrics",
        "collect_nightly_ops",
        "collect_ticket_counts",
        "collect_session_slots",
        "collect_graph_inventory",
    ):
        setattr(collector, method, AsyncMock(return_value={"count": 2}))
    collector.collect_dream_promotions = AsyncMock(return_value={})
    graph = MagicMock(healthcheck=AsyncMock(return_value=True)) if graph_configured else None
    server = MetricsServer(
        collector,
        MagicMock(healthcheck=AsyncMock(return_value=True)),
        graph_svc=graph,
        slow_block_cache=SlowBlockCache(ttl_seconds=0, error_ttl_seconds=0),
    )
    success = json.loads((await server._handle_metrics(MagicMock())).body)
    keys = {
        "database",
        "search_quality",
        "cross_process",
        "dream",
        "nightly",
        "tickets",
        "session_slots",
    }
    if graph_configured:
        keys.add("graph_inventory")
    assert set(success["block_status"]) == keys
    for health in success["block_status"].values():
        _assert_health(health, "ok", None)
    factory.side_effect = ConnectionError("offline")
    for method in (
        "collect_dream_metrics",
        "collect_nightly_ops",
        "collect_ticket_counts",
        "collect_session_slots",
        "collect_graph_inventory",
    ):
        getattr(collector, method).side_effect = ConnectionError("offline")
    failed = json.loads((await server._handle_metrics(MagicMock())).body)
    slow_keys = {"dream", "nightly", "tickets", "session_slots"}
    assert set(success) - slow_keys <= set(failed)
    assert "block_status" in failed
    for key in ("database", "search_quality", "cross_process", "embedding_service"):
        assert set(failed[key]) == set(success[key])
    for key in ("total_requests", "total_errors", "recent_errors", "avg_latency_ms"):
        assert failed["embedding_service"][key] is None
    assert failed["embedding_service"]["model"] == success["embedding_service"]["model"]
    assert failed["embedding_service"]["status"] == "up"
    for key in keys:
        _assert_health(failed["block_status"][key], "degraded", "ConnectionError")
        assert (
            failed["block_status"][key]["last_ok_at"] == success["block_status"][key]["last_ok_at"]
        )


@pytest.mark.parametrize(
    ("block", "method"),
    [
        ("dream", "collect_dream_metrics"),
        ("nightly", "collect_nightly_ops"),
        ("tickets", "collect_ticket_counts"),
        ("session_slots", "collect_session_slots"),
    ],
)
async def test_metrics_slow_block_failure_after_success_omits_key_and_keeps_health(
    block: str, method: str
) -> None:
    collector = _collector(_factory())
    compute = AsyncMock(return_value={"count": 2})
    setattr(collector, method, compute)
    collector.collect_dream_promotions = AsyncMock(return_value={})
    server = MetricsServer(
        collector,
        MagicMock(healthcheck=AsyncMock(return_value=True)),
        slow_block_cache=SlowBlockCache(ttl_seconds=0, error_ttl_seconds=0),
    )
    success = json.loads((await server._handle_metrics(MagicMock())).body)
    assert success[block]["count"] == 2
    last_ok = success["block_status"][block]["last_ok_at"]
    assert last_ok is not None
    compute.side_effect = RuntimeError("offline")
    response = await server._handle_metrics(MagicMock())
    failed = json.loads(response.body)
    assert response.status == 200
    assert block not in failed
    _assert_health(failed["block_status"][block], "degraded", "RuntimeError")
    assert failed["block_status"][block]["last_ok_at"] == last_ok


async def test_cockpit_empty_retrieval_is_not_measured() -> None:
    factory = _factory()
    collector = _collector(factory)
    server = MetricsServer(collector, MagicMock(), host="127.0.0.1", port=0)
    response = await server._handle_cockpit(MagicMock())
    assert response.status == 200
    payload = json.loads(response.body)
    assert set(payload["block_status"]) == {"memory", "retrieval"}
    assert payload["retrieval"]["p50"] is None
    assert payload["retrieval"]["p95"] is None
    _assert_health(payload["block_status"]["retrieval"], "not_measured", "no_samples")
    assert payload["block_status"]["retrieval"]["last_ok_at"] is None


async def test_cockpit_retrieval_health_remembers_samples_when_window_expires() -> None:
    factory = _factory()
    collector = _collector(factory)
    collector.record_search_latency(0.0)
    cockpit = CockpitCollector(collector, factory)
    success = await cockpit.snapshot()
    health = success["block_status"]["retrieval"]
    _assert_health(health, "ok", None)
    assert success["retrieval"]["p95"] == 0.0
    collector._search_latencies.clear()
    cockpit._cache_ts = 0
    empty = await cockpit.snapshot()
    _assert_health(empty["block_status"]["retrieval"], "not_measured", "no_samples")
    assert empty["block_status"]["retrieval"]["last_ok_at"] == health["last_ok_at"]


@pytest.mark.parametrize("samples", [deque(), deque([(time.time() - 3600, 50.0)])])
def test_empty_percentile_window_returns_nulls(samples: deque[tuple[float, float]]) -> None:
    assert MetricsCollector._percentiles(samples, window_s=300) == {
        "p50": None,
        "p95": None,
        "p99": None,
    }


async def test_timeseries_skips_p95_without_samples() -> None:
    factory = _factory()
    flusher = TimeseriesFlusher(_collector(factory), factory)
    await flusher._flush_30min_metrics()
    params = [call.args[1] for call in factory.return_value.execute.await_args_list]
    assert {row["metric"] for row in params} == {"rps", "err_rate"}
    assert all(row["value"] == 0.0 for row in params)
    factory.return_value.commit.assert_awaited_once()


async def test_timeseries_persists_a_measured_zero_p95() -> None:
    factory = _factory()
    collector = _collector(factory)
    collector.record_tool_call("brain_search", latency_ms=0.0)
    await TimeseriesFlusher(collector, factory)._flush_30min_metrics()
    params = [call.args[1] for call in factory.return_value.execute.await_args_list]
    assert {row["metric"] for row in params} == {"rps", "p95", "err_rate"}
    assert next(row["value"] for row in params if row["metric"] == "p95") == 0.0
