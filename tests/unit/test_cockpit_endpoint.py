"""Integration tests for GET /api/cockpit route."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from brain_v42.metrics.collector import MetricsCollector
from brain_v42.metrics.server import MetricsServer

_MOCK_SETTINGS = MagicMock(
    embedding_service_url="http://localhost:8003",
    embedding_dimension=1536,
)


@pytest.fixture(autouse=True)
def _patch_settings():
    with patch("brain_v42.metrics.collector.get_settings", return_value=_MOCK_SETTINGS):
        yield


def _mk_session_factory() -> MagicMock:
    session = AsyncMock()
    session.__aenter__.return_value = session
    session.__aexit__.return_value = None
    session.execute.return_value = MagicMock(
        one=MagicMock(return_value=(0, 0)),
        fetchall=MagicMock(return_value=[]),
    )
    factory = MagicMock()
    factory.return_value = session
    return factory


@pytest.fixture
def collector() -> MetricsCollector:
    c = MetricsCollector(engine=MagicMock(), session_factory=_mk_session_factory())
    c.record_tool_call("brain_search", latency_ms=80.0)
    return c


@pytest.fixture
def mock_embedding_svc() -> MagicMock:
    svc = MagicMock()
    svc.healthcheck = AsyncMock(return_value=True)
    return svc


async def test_cockpit_endpoint_returns_200_json(
    aiohttp_client: Any,
    collector: MetricsCollector,
    mock_embedding_svc: MagicMock,
) -> None:
    server = MetricsServer(collector, mock_embedding_svc, port=0, host="127.0.0.1")
    with (
        patch(
            "brain_v42.metrics.cockpit.collect_memory_stats",
            new=AsyncMock(
                return_value={
                    "episodes": 14820,
                    "episodic_mb": 482,
                    "semantic_chunks": 38420,
                    "semantic_mb": 1240,
                    "lastCompaction": "14:31:51",
                    "freedLastCompaction": 38,
                    "vectorIndex": "hnsw (m=16, ef=200)",
                }
            ),
        ),
        # endpoint is read from settings (no longer hardcoded "stdio") — pin it
        # so the assertion is hermetic against local .env / lru_cache pollution.
        patch(
            "brain_v42.metrics.cockpit.get_settings",
            return_value=MagicMock(brain_mcp_transport="http"),
        ),
    ):
        client = await aiohttp_client(server._build_app())
        resp = await client.get("/api/cockpit")
    assert resp.status == 200
    data = await resp.json()
    # Honest contract: endpoint mirrors settings.brain_mcp_transport
    assert data["endpoint"] == "http"
    assert "metrics" in data
    assert data["memory"]["episodes"] == 14820
    assert isinstance(data["tools"], list)
    assert data["activeConvs"] == []


async def test_cockpit_endpoint_schema_contains_all_required_keys(
    aiohttp_client: Any,
    collector: MetricsCollector,
    mock_embedding_svc: MagicMock,
) -> None:
    server = MetricsServer(collector, mock_embedding_svc, port=0, host="127.0.0.1")
    with patch(
        "brain_v42.metrics.cockpit.collect_memory_stats",
        new=AsyncMock(
            return_value={
                "episodes": 0,
                "episodic_mb": 0,
                "semantic_chunks": 0,
                "semantic_mb": 0,
                "lastCompaction": None,
                "freedLastCompaction": 0,
                "vectorIndex": None,
            }
        ),
    ):
        client = await aiohttp_client(server._build_app())
        resp = await client.get("/api/cockpit")
        data = await resp.json()
    for key in [
        "version",
        "pid",
        "uptime_s",
        "endpoint",
        "metrics",
        "activeConvs",
        "tools",
        "skills",
        "memory",
        "retrieval",
        "latencyBuckets",
        "cost",
        "handoff",
        "rpsHistory",
        "p95History",
        "errHistory",
        "costHistory",
        "recent",
    ]:
        assert key in data


async def test_cockpit_endpoint_degraded_on_memory_stats_failure(
    aiohttp_client: Any,
    collector: MetricsCollector,
    mock_embedding_svc: MagicMock,
) -> None:
    server = MetricsServer(collector, mock_embedding_svc, port=0, host="127.0.0.1")

    # collect_memory_stats swallows its own exceptions (null shape) — mirror here.
    async def _degraded(_sf):
        return {
            "episodes": 0,
            "episodic_mb": 0,
            "semantic_chunks": 0,
            "semantic_mb": 0,
            "lastCompaction": None,
            "freedLastCompaction": 0,
            "vectorIndex": None,
        }

    with patch("brain_v42.metrics.cockpit.collect_memory_stats", new=_degraded):
        client = await aiohttp_client(server._build_app())
        resp = await client.get("/api/cockpit")
    assert resp.status == 200
    data = await resp.json()
    assert data["memory"]["episodes"] == 0


async def test_metrics_route_unchanged_no_regression(
    aiohttp_client: Any,
    collector: MetricsCollector,
    mock_embedding_svc: MagicMock,
) -> None:
    server = MetricsServer(collector, mock_embedding_svc, port=0, host="127.0.0.1")
    collector.collect_db_stats = AsyncMock(
        return_value={
            "pool": {"active": 0, "idle": 0, "overflow": 0, "max": 15},
            "tables": {},
            "db_size_bytes": 0,
            "dimension_mismatches": 0,
        }
    )
    client = await aiohttp_client(server._build_app())
    resp = await client.get("/metrics")
    assert resp.status == 200
