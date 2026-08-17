"""HTTP contract and network boundary for the Brain graph endpoint."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

from brain_v42.metrics.brain_graph_server import BrainGraphMetricsServer
from brain_v42.services.brain_graph_projection import (
    BrainGraphProjectionService,
    PostgresGraphRows,
)


def _server(projection: AsyncMock) -> BrainGraphMetricsServer:
    collector = MagicMock()
    collector._session_factory = MagicMock()
    return BrainGraphMetricsServer(
        collector=collector,
        embedding_svc=MagicMock(),
        graph_projection_svc=projection,
        port=0,
        host="0.0.0.0",
    )


async def test_graph_route_returns_versioned_snapshot_only_to_loopback(
    aiohttp_client: Any,
) -> None:
    projection = AsyncMock()
    projection.snapshot.return_value = {
        "schema_version": "brain-graph.v1",
        "nodes": [],
        "edges": [],
    }
    server = _server(projection)
    client = await aiohttp_client(server._build_app())

    response = await client.get("/api/brain-graph/v1")

    assert response.status == 200
    assert response.content_type == "application/json"
    assert response.headers["Cache-Control"] == "private, max-age=5"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert await response.json() == projection.snapshot.return_value
    projection.snapshot.assert_awaited_once_with()


async def test_graph_route_rejects_non_loopback_peer_before_loading_data(
    aiohttp_client: Any,
) -> None:
    projection = AsyncMock()
    server = _server(projection)
    client = await aiohttp_client(server._build_app())

    with patch(
        "brain_v42.metrics.brain_graph_server._has_loopback_tcp_peer",
        return_value=False,
    ):
        response = await client.get(
            "/api/brain-graph/v1",
            headers={"X-Forwarded-For": "127.0.0.1"},
        )

    assert response.status == 403
    assert await response.json() == {"error": "loopback peer required"}
    projection.snapshot.assert_not_awaited()


def test_graph_server_preserves_all_base_routes() -> None:
    server = _server(AsyncMock())

    routes = {
        (route.method, route.resource.canonical) for route in server._build_app().router.routes()
    }

    assert ("GET", "/metrics") in routes
    assert ("GET", "/api/cockpit") in routes
    assert ("GET", "/api/brain-graph/v1") in routes
    assert ("POST", "/v1/logs") not in routes  # host is deliberately non-loopback


async def test_graph_route_fails_closed_without_leaking_internal_errors(
    aiohttp_client: Any,
) -> None:
    projection = AsyncMock()
    projection.snapshot.side_effect = RuntimeError("postgres://user:secret@db")
    server = _server(projection)
    client = await aiohttp_client(server._build_app())

    response = await client.get("/api/brain-graph/v1")

    assert response.status == 503
    assert await response.json() == {"error": "brain graph unavailable"}


async def test_graph_route_serializes_a_real_projection_with_native_database_types() -> None:
    entity_id = UUID("10000000-0000-0000-0000-000000000001")
    created_at = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)

    class PostgresSource:
        async def read(self, _max_rows: int) -> PostgresGraphRows:
            return PostgresGraphRows(
                tables={
                    "project_contexts": [
                        {
                            "project_key": "brain-v42",
                            "name": "Brain v42",
                            "blockers": [],
                            "related_projects": [],
                            "created_at": created_at,
                            "updated_at": created_at,
                        }
                    ],
                    "decisions": [
                        {
                            "id": entity_id,
                            "title": "Contrat sérialisable",
                            "project_key": "brain-v42",
                            "status": "active",
                            "access_count": 0,
                            "created_at": created_at,
                            "updated_at": created_at,
                        }
                    ],
                }
            )

    payload = await BrainGraphProjectionService(
        postgres_source=PostgresSource(),
        neo4j_source=None,
        cache_ttl_s=0,
    ).snapshot()
    projection = AsyncMock()
    projection.snapshot.return_value = payload
    server = _server(projection)

    with patch(
        "brain_v42.metrics.brain_graph_server._has_loopback_tcp_peer",
        return_value=True,
    ):
        response = await server._handle_brain_graph(MagicMock())

    decoded = json.loads(response.text)
    assert response.status == 200
    assert decoded["schema_version"] == "brain-graph.v1"
    assert decoded["nodes"][0]["created_at"] == "2026-07-20T12:00:00Z"
    assert {node["id"] for node in decoded["nodes"]} == {
        "project:brain-v42",
        f"decision:{entity_id}",
    }


async def test_graph_route_returns_unavailable_snapshot_as_observable_state() -> None:
    projection = AsyncMock()
    projection.snapshot.return_value = {
        "schema_version": "brain-graph.v1",
        "status": "unavailable",
        "sources": {
            "postgres": {"status": "unavailable", "records": 0},
            "neo4j": {"status": "unavailable", "nodes": 0, "edges": 0},
        },
        "nodes": [],
        "edges": [],
    }
    server = _server(projection)

    with patch(
        "brain_v42.metrics.brain_graph_server._has_loopback_tcp_peer",
        return_value=True,
    ):
        response = await server._handle_brain_graph(MagicMock())

    assert response.status == 200
    assert json.loads(response.text)["status"] == "unavailable"
