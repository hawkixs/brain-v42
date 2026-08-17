"""Production composition exposes the Brain graph without enabling Neo4j."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from brain_v42.config import Settings
from brain_v42.metrics.brain_graph_server import BrainGraphMetricsServer
from brain_v42.metrics.runtime import build_metrics_runtime


def test_metrics_runtime_wires_postgres_graph_projection_when_neo4j_is_disabled() -> None:
    settings = Settings(
        postgres_url="postgresql+asyncpg://u:p@localhost:5433/brain_test",
        metrics_legacy_automation_enabled=False,
        graph_enabled=False,
        _env_file=None,  # type: ignore[call-arg]
    )

    runtime = build_metrics_runtime(settings=settings, engine=MagicMock())
    assert runtime._resources.server_factory is not None
    server = runtime._resources.server_factory(None, None)

    assert isinstance(server, BrainGraphMetricsServer)
    routes = {route.resource.canonical for route in server._build_app().router.routes()}
    assert "/api/brain-graph/v1" in routes


def test_metrics_runtime_refuses_the_service_private_projector_role() -> None:
    settings = Settings(
        postgres_url="postgresql+asyncpg://u:p@localhost:5433/brain_test",
        metrics_legacy_automation_enabled=False,
        graph_enabled=True,
        graph_ledger_write_enabled=True,
        graph_projector_enabled=True,
        graph_projector_neo4j_url="bolt://projector-only:7687",
        graph_projector_neo4j_user="projector",
        graph_projector_neo4j_password="private-secret",
        _env_file=None,  # type: ignore[call-arg]
    )

    with pytest.raises(RuntimeError, match="projector role is restricted to the MCP runtime"):
        build_metrics_runtime(settings=settings, engine=MagicMock())


async def test_metrics_runtime_shares_and_closes_enabled_neo4j_driver_once() -> None:
    settings = Settings(
        postgres_url="postgresql+asyncpg://u:p@localhost:5433/brain_test",
        metrics_legacy_automation_enabled=False,
        graph_enabled=True,
        neo4j_url="bolt://localhost:7687",
        neo4j_user="neo4j",
        neo4j_password="test-only",
        _env_file=None,  # type: ignore[call-arg]
    )
    engine = MagicMock()
    engine.dispose = AsyncMock()
    driver = MagicMock()
    driver.close = AsyncMock()

    with patch(
        "brain_v42.metrics.runtime.create_neo4j_driver",
        return_value=driver,
    ):
        runtime = build_metrics_runtime(settings=settings, engine=engine)

    server = runtime._resources.server_factory(None, None)  # type: ignore[misc]
    assert isinstance(server, BrainGraphMetricsServer)
    assert server._graph_svc._driver is driver
    assert server._graph_projection_svc._neo4j_source._driver is driver
    assert runtime._resources.neo4j_driver is driver

    runtime._resources.embedding_svc.close = AsyncMock()  # type: ignore[method-assign]
    errors = await runtime._cleanup(None, None)

    assert errors == []
    driver.close.assert_awaited_once_with()
    engine.dispose.assert_awaited_once_with()
