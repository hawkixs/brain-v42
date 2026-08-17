"""Tests for MetricsServer — aiohttp HTTP sidecar."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from brain_v42.metrics.collector import MetricsCollector
from brain_v42.metrics.server import MetricsServer

_MOCK_SETTINGS = MagicMock(
    embedding_service_url="http://localhost:8003",
    embedding_dimension=1024,
)


@pytest.fixture(autouse=True)
def _patch_settings() -> Any:  # noqa: ANN401
    with patch("brain_v42.metrics.collector.get_settings", return_value=_MOCK_SETTINGS):
        yield


@pytest.fixture
def collector() -> MetricsCollector:
    c = MetricsCollector(engine=MagicMock(), session_factory=MagicMock())
    c.record_tool_call("brain_search", latency_ms=100.0)
    # Stub every DB-backed async method so _handle_metrics can await them
    # without hitting the MagicMock session_factory (would otherwise leak
    # un-awaited coroutines from AsyncMock probes inside the real methods).
    c.collect_db_stats = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "pool": {"active": 0, "idle": 0, "overflow": 0, "max": 15},
            "tables": {},
            "db_size_bytes": 0,
            "dimension_mismatches": 0,
            "embedding_backlog": {
                "total": 2,
                "by_entity_type": {"decision": {"count": 2, "oldest_age_seconds": 60.0}},
            },
            "graph_outbox": {
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
            },
        }
    )
    c.collect_search_quality = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "searches_total": 0,
            "searches_with_zero_results": 0,
            "avg_score": 0.0,
        }
    )
    c.collect_process_metrics = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "active_processes": 0,
            "total_memory_rss_bytes": 0,
            "tools": {},
            "embedding": {
                "total_requests": 0,
                "total_errors": 0,
                "gpu_busy_errors": 0,
                "unreachable_errors": 0,
                "recent_errors": 0,
                "avg_latency_ms": 0.0,
            },
        }
    )
    c.collect_dream_metrics = AsyncMock(  # type: ignore[method-assign]
        return_value={"last_run": None, "history": []}
    )
    c.collect_dream_promotions = AsyncMock(  # type: ignore[method-assign]
        return_value={}
    )
    c.collect_dream_promoted_health = AsyncMock(  # type: ignore[method-assign]
        return_value=[]
    )
    # Section nightly (ticket de1ad785) : stub vide par défaut — sinon le
    # handler lirait le VRAI fichier killswitches de la machine de test.
    c.collect_nightly_ops = AsyncMock(  # type: ignore[method-assign]
        return_value={}
    )
    return c


@pytest.fixture
def mock_embedding_svc() -> MagicMock:
    svc = MagicMock()
    svc.healthcheck = AsyncMock(return_value=True)
    return svc


async def test_metrics_endpoint_returns_json(
    aiohttp_client: Any, collector: MetricsCollector, mock_embedding_svc: MagicMock
) -> None:
    server = MetricsServer(collector, mock_embedding_svc, port=0, host="127.0.0.1")
    client = await aiohttp_client(server._build_app())

    resp = await client.get("/metrics")
    assert resp.status == 200
    data = await resp.json()
    assert "version" in data
    assert "tools" in data
    assert "database" in data
    assert "brain_search" in data["tools"]
    assert data["database"]["embedding_backlog"]["total"] == 2
    assert data["database"]["graph_outbox"]["pending"] == 7
    assert data["database"]["graph_outbox"]["projector"]["healthy"] is True


async def test_metrics_endpoint_includes_embedding_status(
    aiohttp_client: Any, collector: MetricsCollector, mock_embedding_svc: MagicMock
) -> None:
    server = MetricsServer(collector, mock_embedding_svc, port=0, host="127.0.0.1")
    client = await aiohttp_client(server._build_app())

    resp = await client.get("/metrics")
    data = await resp.json()
    assert data["embedding_service"]["status"] == "up"


async def test_metrics_endpoint_merges_dream_promotions_counts(
    aiohttp_client: Any, collector: MetricsCollector, mock_embedding_svc: MagicMock
) -> None:
    """Dream block includes a `promotions` sub-block with target_type counts.

    red-monitor's BrainDream card shows how many ADRs/runbooks have been
    promoted over the audit history. Without this, the PROMOTE-phase
    velocity is invisible.
    """
    server = MetricsServer(collector, mock_embedding_svc, port=0, host="127.0.0.1")
    collector.collect_dream_metrics = AsyncMock(  # type: ignore[method-assign]
        return_value={"last_run": {"date": "2026-04-20", "status": "success"}, "history": []}
    )
    collector.collect_dream_promotions = AsyncMock(  # type: ignore[method-assign]
        return_value={"adr": 3, "runbook": 1, "skipped_dedup": 5}
    )
    client = await aiohttp_client(server._build_app())

    resp = await client.get("/metrics")
    data = await resp.json()
    assert data["dream"]["promotions"] == {
        "total": 9,
        "by_type": {"adr": 3, "runbook": 1, "skipped_dedup": 5},
    }


async def test_metrics_endpoint_surfaces_promoted_health(
    aiohttp_client: Any, collector: MetricsCollector, mock_embedding_svc: MagicMock
) -> None:
    """Dream block includes `promoted_health` when the collector returns rows.

    ADR #4 v2 telemetry: post-promotion health signals (access_count,
    superseded, days_since_promotion) per auto-promoted ADR/runbook,
    surfaced via JSON only (per-target Prometheus labels would risk
    unbounded cardinality).
    """
    server = MetricsServer(collector, mock_embedding_svc, port=0, host="127.0.0.1")
    collector.collect_dream_metrics = AsyncMock(  # type: ignore[method-assign]
        return_value={"last_run": {"date": "2026-04-27", "status": "success"}, "history": []}
    )
    collector.collect_dream_promoted_health = AsyncMock(  # type: ignore[method-assign]
        return_value=[
            {
                "target_type": "adr",
                "target_id": "abc-123",
                "title": "Sample ADR",
                "status": "accepted",
                "superseded": False,
                "access_count": 4,
                "days_since_promotion": 2.5,
                "days_since_last_access": 1.0,
                "promoted_at": "2026-04-25T06:14:00+00:00",
            }
        ]
    )
    client = await aiohttp_client(server._build_app())

    resp = await client.get("/metrics")
    data = await resp.json()
    assert "promoted_health" in data["dream"]
    assert len(data["dream"]["promoted_health"]) == 1
    assert data["dream"]["promoted_health"][0]["target_type"] == "adr"
    assert data["dream"]["promoted_health"][0]["superseded"] is False


async def test_metrics_404_on_other_paths(
    aiohttp_client: Any, collector: MetricsCollector, mock_embedding_svc: MagicMock
) -> None:
    server = MetricsServer(collector, mock_embedding_svc, port=0, host="127.0.0.1")
    client = await aiohttp_client(server._build_app())

    resp = await client.get("/health")
    assert resp.status == 404


def test_otlp_receiver_preserves_existing_routes_and_global_body_limit(
    collector: MetricsCollector,
    mock_embedding_svc: MagicMock,
) -> None:
    server = MetricsServer(
        collector,
        mock_embedding_svc,
        host="127.0.0.1",
        gitlab_ingestor=MagicMock(),
        project_key_resolver=AsyncMock(return_value="brain_v42"),
        webhook_secret="secret",
    )

    app = server._build_app()
    routes = {(route.method, route.resource.canonical) for route in app.router.routes()}

    assert ("GET", "/metrics") in routes
    assert ("GET", "/api/cockpit") in routes
    assert ("POST", "/gitlab/webhook") in routes
    assert ("POST", "/v1/logs") in routes
    assert app._client_max_size == 1024**2


async def test_start_disables_aiohttp_request_decompression(
    collector: MetricsCollector,
    mock_embedding_svc: MagicMock,
) -> None:
    server = MetricsServer(collector, mock_embedding_svc, port=0, host="127.0.0.1")
    runner = MagicMock()
    runner.setup = AsyncMock()
    site = MagicMock()
    site.start = AsyncMock()

    with (
        patch("brain_v42.metrics.server.web.AppRunner", return_value=runner) as app_runner,
        patch("brain_v42.metrics.server.web.TCPSite", return_value=site),
    ):
        await server.start()

    app_runner.assert_called_once()
    assert app_runner.call_args.kwargs == {"auto_decompress": False}
