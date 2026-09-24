"""Tests for MetricsServer — aiohttp HTTP sidecar."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from brain_v42.metrics.collector import MetricsCollector
from brain_v42.metrics.server import MetricsServer

_MOCK_SETTINGS = MagicMock(
    embedding_service_url="http://localhost:8003",
    embedding_dimension=1024,
    embedding_backend="shim",
    embedding_model="qodo",
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
    # Nightly section (ticket de1ad785): an empty stub by default — otherwise the
    # handler would read the test machine's REAL killswitches file.
    c.collect_nightly_ops = AsyncMock(  # type: ignore[method-assign]
        return_value={}
    )
    # Tickets section (ticket 0fb857ef): an empty stub by default — otherwise
    # the handler would open a real DB session through the MagicMock factory.
    c.collect_ticket_counts = AsyncMock(  # type: ignore[method-assign]
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


async def test_metrics_endpoint_uses_cross_process_embedding_usage(
    aiohttp_client: Any, collector: MetricsCollector, mock_embedding_svc: MagicMock
) -> None:
    collector.collect_process_metrics = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "active_processes": 1,
            "active_agents": 0,
            "total_memory_rss_bytes": 0,
            "tools": {},
            # collect_process_metrics always carries a "decay" block since 04c09575
            # (a DB-wide gauge, reduced latest-row-wins, split out of "tools").
            "decay": {"stale_count": 0, "archived_count": 0, "access_log_size": 0},
            "embedding": {
                "total_requests": 2,
                "total_errors": 0,
                "gpu_busy_errors": 0,
                "unreachable_errors": 0,
                "recent_errors": 0,
                "avg_latency_ms": 4.0,
                "usage": {
                    "read": {"total_tokens": 3, "reported_requests": 1},
                    "write": {"total_tokens": 12, "reported_requests": 2},
                },
            },
        }
    )
    server = MetricsServer(collector, mock_embedding_svc, port=0, host="127.0.0.1")
    client = await aiohttp_client(server._build_app())

    data = await (await client.get("/metrics")).json()
    assert data["embedding_service"]["usage"] == {
        "read": {"total_tokens": 3, "reported_requests": 1},
        "write": {"total_tokens": 12, "reported_requests": 2},
    }


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


class TestSlowBlockCacheWiring:
    """dream/nightly/graph_inventory go through SlowBlockCache (decision 1669d429 item 2).

    `database` and the embedding healthcheck are deliberately excluded — they
    must stay live (recomputed) on every poll.
    """

    def _clock_from(self, box: list[float]) -> Any:
        def _clock() -> float:
            return box[0]

        return _clock

    def _server(
        self,
        collector: MetricsCollector,
        embedding_svc: MagicMock,
        *,
        time_box: list[float],
        graph_svc: Any = None,
    ) -> MetricsServer:
        from brain_v42.metrics.slow_block_cache import SlowBlockCache

        cache = SlowBlockCache(
            ttl_seconds=30.0,
            error_ttl_seconds=5.0,
            clock=self._clock_from(time_box),
            wall_clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        )
        return MetricsServer(
            collector,
            embedding_svc,
            port=0,
            host="127.0.0.1",
            graph_svc=graph_svc,
            slow_block_cache=cache,
        )

    async def test_dream_and_nightly_blocks_carry_generated_at(
        self, collector: MetricsCollector, mock_embedding_svc: MagicMock
    ) -> None:
        collector.collect_dream_metrics = AsyncMock(  # type: ignore[method-assign]
            return_value={"last_run": {"date": "2026-04-20", "status": "success"}, "history": []}
        )
        collector.collect_nightly_ops = AsyncMock(  # type: ignore[method-assign]
            return_value={"killswitches": {}, "extract": {"proposed_pending": 0}}
        )
        server = self._server(collector, mock_embedding_svc, time_box=[0.0])

        response = await server._handle_metrics(MagicMock())
        data = json.loads(response.body)

        assert data["dream"]["generated_at"] == "2026-01-01T00:00:00+00:00"
        assert data["nightly"]["generated_at"] == "2026-01-01T00:00:00+00:00"

    async def test_graph_block_carries_generated_at(
        self, collector: MetricsCollector, mock_embedding_svc: MagicMock
    ) -> None:
        collector.collect_graph_inventory = AsyncMock(  # type: ignore[method-assign]
            return_value={"nodes_total": {"Decision": 3}, "edges_total": {}, "orphans_total": {}}
        )
        graph_svc = AsyncMock()
        graph_svc.healthcheck = AsyncMock(return_value=True)
        server = self._server(collector, mock_embedding_svc, time_box=[0.0], graph_svc=graph_svc)

        response = await server._handle_metrics(MagicMock())
        data = json.loads(response.body)

        assert data["graph"]["generated_at"] == "2026-01-01T00:00:00+00:00"
        assert data["graph"]["nodes_total"] == {"Decision": 3}

    async def test_repeated_polls_within_ttl_compute_the_slow_collectors_once(
        self, collector: MetricsCollector, mock_embedding_svc: MagicMock
    ) -> None:
        collector.collect_dream_metrics = AsyncMock(  # type: ignore[method-assign]
            return_value={"last_run": {"date": "2026-04-20", "status": "success"}, "history": []}
        )
        collector.collect_nightly_ops = AsyncMock(return_value={"killswitches": {}})  # type: ignore[method-assign]
        graph_svc = AsyncMock()
        graph_svc.healthcheck = AsyncMock(return_value=True)
        collector.collect_graph_inventory = AsyncMock(  # type: ignore[method-assign]
            return_value={"nodes_total": {}, "edges_total": {}, "orphans_total": {}}
        )
        time_box = [0.0]
        server = self._server(collector, mock_embedding_svc, time_box=time_box, graph_svc=graph_svc)

        await server._handle_metrics(MagicMock())
        time_box[0] = 1.0  # well within the 30s TTL
        await server._handle_metrics(MagicMock())

        collector.collect_dream_metrics.assert_called_once()
        collector.collect_nightly_ops.assert_called_once()
        collector.collect_graph_inventory.assert_called_once()

    async def test_ttl_expiry_recomputes_the_slow_collectors(
        self, collector: MetricsCollector, mock_embedding_svc: MagicMock
    ) -> None:
        collector.collect_nightly_ops = AsyncMock(return_value={"killswitches": {}})  # type: ignore[method-assign]
        time_box = [0.0]
        server = self._server(collector, mock_embedding_svc, time_box=time_box)

        await server._handle_metrics(MagicMock())
        time_box[0] = 30.1  # past the 30s TTL
        await server._handle_metrics(MagicMock())

        assert collector.collect_nightly_ops.call_count == 2

    async def test_live_blocks_are_recomputed_on_every_poll_regardless_of_ttl(
        self, collector: MetricsCollector, mock_embedding_svc: MagicMock
    ) -> None:
        """database and the embedding healthcheck are NOT behind the cache."""
        time_box = [0.0]
        server = self._server(collector, mock_embedding_svc, time_box=time_box)

        await server._handle_metrics(MagicMock())
        await server._handle_metrics(MagicMock())  # still t=0, well within any TTL

        assert collector.collect_db_stats.call_count == 2
        assert mock_embedding_svc.healthcheck.call_count == 2

    async def test_a_dream_block_computation_failure_degrades_without_crashing(
        self, collector: MetricsCollector, mock_embedding_svc: MagicMock
    ) -> None:
        """A raised exception assembling the dream block must not crash /metrics.

        Degrades like every other collector failure: the section is simply
        absent, never a 500.
        """
        collector.collect_dream_metrics = AsyncMock(  # type: ignore[method-assign]
            return_value={"last_run": {"date": "2026-04-20", "status": "success"}, "history": []}
        )
        collector.collect_dream_promotions = AsyncMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]
        server = self._server(collector, mock_embedding_svc, time_box=[0.0])

        response = await server._handle_metrics(MagicMock())
        data = json.loads(response.body)

        assert response.status == 200
        assert "dream" not in data

    async def test_a_failed_slow_block_is_retried_after_the_short_error_ttl(
        self, collector: MetricsCollector, mock_embedding_svc: MagicMock
    ) -> None:
        collector.collect_nightly_ops = AsyncMock(  # type: ignore[method-assign]
            side_effect=[RuntimeError("boom"), {"killswitches": {}}]
        )
        time_box = [0.0]
        server = self._server(collector, mock_embedding_svc, time_box=time_box)

        first = json.loads((await server._handle_metrics(MagicMock())).body)
        assert "nightly" not in first

        time_box[0] = 1.0  # still inside the 5s error TTL: no retry yet
        second = json.loads((await server._handle_metrics(MagicMock())).body)
        assert "nightly" not in second
        assert collector.collect_nightly_ops.call_count == 1

        time_box[0] = 5.1  # past the error TTL: retried
        third = json.loads((await server._handle_metrics(MagicMock())).body)
        assert third["nightly"]["killswitches"] == {}
        assert collector.collect_nightly_ops.call_count == 2

    async def test_tickets_block_carries_generated_at_and_agreed_shape(
        self, collector: MetricsCollector, mock_embedding_svc: MagicMock
    ) -> None:
        """ticket 0fb857ef: `tickets` goes through the same cache under its own key."""
        collector.collect_ticket_counts = AsyncMock(  # type: ignore[method-assign]
            return_value={
                "categories": [
                    {"key": "todo", "label": "à traiter"},
                    {"key": "to_confirm", "label": "à confirmer"},
                    {"key": "waiting", "label": "en attente"},
                ],
                "projects": [
                    {"project": "brain-v42", "counts": {"todo": 53, "to_confirm": 2, "waiting": 9}}
                ],
            }
        )
        server = self._server(collector, mock_embedding_svc, time_box=[0.0])

        response = await server._handle_metrics(MagicMock())
        data = json.loads(response.body)

        assert data["tickets"]["generated_at"] == "2026-01-01T00:00:00+00:00"
        assert data["tickets"]["projects"] == [
            {"project": "brain-v42", "counts": {"todo": 53, "to_confirm": 2, "waiting": 9}}
        ]

    async def test_tickets_block_nothing_pending_is_an_empty_list_not_absent(
        self, collector: MetricsCollector, mock_embedding_svc: MagicMock
    ) -> None:
        """`projects: []` (nothing pending anywhere) must still publish the key."""
        collector.collect_ticket_counts = AsyncMock(  # type: ignore[method-assign]
            return_value={"categories": [], "projects": []}
        )
        server = self._server(collector, mock_embedding_svc, time_box=[0.0])

        response = await server._handle_metrics(MagicMock())
        data = json.loads(response.body)

        assert "tickets" in data
        assert data["tickets"]["projects"] == []

    async def test_a_tickets_block_computation_failure_degrades_without_crashing(
        self, collector: MetricsCollector, mock_embedding_svc: MagicMock
    ) -> None:
        """A raised exception must not crash /metrics: the key is absent, never null."""
        collector.collect_ticket_counts = AsyncMock(side_effect=RuntimeError("db down"))  # type: ignore[method-assign]
        server = self._server(collector, mock_embedding_svc, time_box=[0.0])

        response = await server._handle_metrics(MagicMock())
        data = json.loads(response.body)

        assert response.status == 200
        assert "tickets" not in data


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
