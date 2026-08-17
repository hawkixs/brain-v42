"""Contract tests: _cost and _buckets pseudo-tools must not leak into metrics["tools"].

Background: collect_process_metrics() returns a ``tools`` dict that may include
``_reranker``, ``_graph``, ``_cost``, and ``_buckets`` pseudo-tools injected by
the MetricsFlusher into the ``_process`` row in the DB.  MetricsServer._handle_metrics
pops ``_reranker`` and ``_graph`` (promoting them to top-level sections) but did NOT
pop ``_cost``/``_buckets``, which leaked as garbage entries in metrics["tools"] and
cross_process.tools — visible in red-monitor as spurious tool rows.

Fix (server.py lines 97-114): also pop ``_cost`` and ``_buckets`` after popping
``_reranker``/``_graph``.  Their values are DISCARDED (they duplicate top-level
sections already assembled from the in-memory collector, so merging them would
double-count; drop is the right behaviour).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from brain_v42.metrics.collector import MetricsCollector
from brain_v42.metrics.server import MetricsServer

_MOCK_SETTINGS = MagicMock(
    embedding_service_url="http://localhost:8003",
    embedding_dimension=1024,
)


def _make_collector() -> MetricsCollector:
    c = MetricsCollector(engine=MagicMock(), session_factory=MagicMock())
    c.record_tool_call("brain_search", latency_ms=50.0)
    c.collect_db_stats = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "pool": {"active": 0, "idle": 0, "overflow": 0, "max": 15},
            "tables": {},
            "db_size_bytes": 0,
            "dimension_mismatches": 0,
        }
    )
    c.collect_search_quality = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "searches_total": 0,
            "searches_with_zero_results": 0,
            "avg_score": 0.0,
        }
    )
    # Simulate a DB row where flusher injected _cost and _buckets pseudo-tools
    # into the _process row; collect_process_metrics returns them in "tools".
    c.collect_process_metrics = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "active_processes": 1,
            "active_agents": 1,
            "total_memory_rss_bytes": 0,
            "tools": {
                "brain_search": {
                    "calls": 5,
                    "errors": 0,
                    "recent_errors": 0,
                    "avg_latency_ms": 50.0,
                },
                "_reranker": {
                    "calls": 2,
                    "errors": 0,
                    "recent_errors": 0,
                    "avg_latency_ms": 10.0,
                },
                "_graph": {
                    "calls": 1,
                    "errors": 0,
                    "recent_errors": 0,
                    "avg_latency_ms": 5.0,
                },
                "_cost": {
                    "calls": 0,
                    "errors": 0,
                    "recent_errors": 0,
                    "avg_latency_ms": 0.0,
                    "total": 0.042,
                    "by_model": {"sonnet-4.5": 0.042},
                },
                "_buckets": {
                    "calls": 0,
                    "errors": 0,
                    "recent_errors": 0,
                    "avg_latency_ms": 0.0,
                    "< 100ms": 4,
                    "100-300ms": 1,
                },
                "_decay": {
                    "stale_count": 7,
                    "archived_count": 3,
                    "access_log_size": 42,
                },
            },
            "embedding": {
                "total_requests": 0,
                "total_errors": 0,
                "gpu_busy_errors": 0,
                "unreachable_errors": 0,
                "recent_errors": 0,
                "avg_latency_ms": 0.0,
            },
            "by_agent": {},
        }
    )
    c.collect_dream_metrics = AsyncMock(return_value={"last_run": None, "history": []})  # type: ignore[method-assign]
    c.collect_dream_promotions = AsyncMock(return_value={})  # type: ignore[method-assign]
    c.collect_dream_promoted_health = AsyncMock(return_value=[])  # type: ignore[method-assign]
    c.collect_nightly_ops = AsyncMock(return_value={})  # type: ignore[method-assign]
    return c


async def test_cost_pseudo_tool_absent_from_metrics_tools(aiohttp_client: Any) -> None:
    """_cost must be stripped from metrics["tools"] — it is a flusher pseudo-tool."""
    with patch("brain_v42.metrics.collector.get_settings", return_value=_MOCK_SETTINGS):
        c = _make_collector()
        embedding_svc = MagicMock()
        embedding_svc.healthcheck = AsyncMock(return_value=True)
        server = MetricsServer(c, embedding_svc, port=0, host="127.0.0.1")
        client = await aiohttp_client(server._build_app())

        resp = await client.get("/metrics")
        data = await resp.json()

        assert "_cost" not in data["tools"], "_cost pseudo-tool must not appear in metrics['tools']"


async def test_buckets_pseudo_tool_absent_from_metrics_tools(aiohttp_client: Any) -> None:
    """_buckets must be stripped from metrics["tools"] — it is a flusher pseudo-tool."""
    with patch("brain_v42.metrics.collector.get_settings", return_value=_MOCK_SETTINGS):
        c = _make_collector()
        embedding_svc = MagicMock()
        embedding_svc.healthcheck = AsyncMock(return_value=True)
        server = MetricsServer(c, embedding_svc, port=0, host="127.0.0.1")
        client = await aiohttp_client(server._build_app())

        resp = await client.get("/metrics")
        data = await resp.json()

        assert "_buckets" not in data["tools"], (
            "_buckets pseudo-tool must not appear in metrics['tools']"
        )


async def test_cost_pseudo_tool_absent_from_cross_process_tools(aiohttp_client: Any) -> None:
    """_cost must be stripped from cross_process.tools, not just top-level tools."""
    with patch("brain_v42.metrics.collector.get_settings", return_value=_MOCK_SETTINGS):
        c = _make_collector()
        embedding_svc = MagicMock()
        embedding_svc.healthcheck = AsyncMock(return_value=True)
        server = MetricsServer(c, embedding_svc, port=0, host="127.0.0.1")
        client = await aiohttp_client(server._build_app())

        resp = await client.get("/metrics")
        data = await resp.json()

        assert "_cost" not in data["cross_process"]["tools"], (
            "_cost must not appear in cross_process.tools"
        )


async def test_real_tools_preserved_after_pseudo_tool_strip(aiohttp_client: Any) -> None:
    """After stripping pseudo-tools, real tools remain intact in metrics["tools"]."""
    with patch("brain_v42.metrics.collector.get_settings", return_value=_MOCK_SETTINGS):
        c = _make_collector()
        embedding_svc = MagicMock()
        embedding_svc.healthcheck = AsyncMock(return_value=True)
        server = MetricsServer(c, embedding_svc, port=0, host="127.0.0.1")
        client = await aiohttp_client(server._build_app())

        resp = await client.get("/metrics")
        data = await resp.json()

        # brain_search must still be present (it's a real tool, not a pseudo-tool)
        assert "brain_search" in data["tools"], "brain_search must survive pseudo-tool filtering"


async def test_decay_pseudo_tool_absent_from_metrics_tools(aiohttp_client: Any) -> None:
    """_decay must be stripped from metrics["tools"] — it is a flusher pseudo-tool."""
    with patch("brain_v42.metrics.collector.get_settings", return_value=_MOCK_SETTINGS):
        c = _make_collector()
        embedding_svc = MagicMock()
        embedding_svc.healthcheck = AsyncMock(return_value=True)
        server = MetricsServer(c, embedding_svc, port=0, host="127.0.0.1")
        client = await aiohttp_client(server._build_app())

        resp = await client.get("/metrics")
        data = await resp.json()

        assert "_decay" not in data["tools"], (
            "_decay pseudo-tool must not appear in metrics['tools']"
        )
        assert "_decay" not in data["cross_process"]["tools"], (
            "_decay must not appear in cross_process.tools"
        )


async def test_decay_pseudo_tool_promoted_to_top_level_decay(aiohttp_client: Any) -> None:
    """_decay values must be promoted to metrics["decay"] (authoritative cross-process).

    The sidecar's in-memory collector never sees MCP-process decay stats — without
    this promotion the decay block stays at structural zeros forever.
    """
    with patch("brain_v42.metrics.collector.get_settings", return_value=_MOCK_SETTINGS):
        c = _make_collector()
        embedding_svc = MagicMock()
        embedding_svc.healthcheck = AsyncMock(return_value=True)
        server = MetricsServer(c, embedding_svc, port=0, host="127.0.0.1")
        client = await aiohttp_client(server._build_app())

        resp = await client.get("/metrics")
        data = await resp.json()

        assert data["decay"]["stale_count"] == 7
        assert data["decay"]["archived_count"] == 3
        assert data["decay"]["access_log_size"] == 42
