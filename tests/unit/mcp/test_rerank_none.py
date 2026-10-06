"""The rollback constructs no reranker wrappers and starts no probe."""

from unittest.mock import AsyncMock, MagicMock, patch

from brain_v42.config import Settings
from brain_v42.mcp.server import app_lifecycle, build_services
from tests.unit.mcp.test_rerank_probe_lifecycle import _services, _settings


async def test_build_services_none_and_lifecycle_without_probe() -> None:
    settings = Settings(
        postgres_url="postgresql+asyncpg://test:test@localhost/brain_unit",
        rerank_backend="none",
        metrics_enabled=True,
        decay_enabled=False,
        graph_enabled=False,
        _env_file=None,
    )
    with (
        patch("brain_v42.mcp.server.get_session_factory", return_value=MagicMock()),
        patch("brain_v42.mcp.server.get_settings", return_value=settings),
        patch("brain_v42.mcp.server.build_embedding_service", return_value=MagicMock()),
        patch("brain_v42.db.engine.get_engine", return_value=MagicMock()),
        patch("brain_v42.metrics.collector.get_settings", return_value=settings),
        patch("brain_v42.mcp.server.create_neo4j_driver", return_value=None),
        patch("brain_v42.services.search.HybridReranker", side_effect=AssertionError("wrapper")),
        patch(
            "brain_v42.services.search.batching_reranker.BatchingRerankerClient",
            side_effect=AssertionError("batcher"),
        ),
    ):
        services = build_services()
    assert services["reranker_client"] is None
    assert services["brain_svc"]._hybrid_searcher._reranker is None
    assert services["brain_svc"]._rerank_identity == "none"
    assert services["metrics_collector"].get_metrics()["reranker"]["backend"] == "none"

    lifecycle_services = _services(None)
    lifecycle_services["reranker_client"] = services["reranker_client"]
    with (
        patch("brain_v42.mcp.server.run_rerank_probe_loop", new_callable=AsyncMock) as probe,
        patch("brain_v42.mcp.server.close_neo4j_driver", new_callable=AsyncMock),
        patch("brain_v42.mcp.server.dispose_engine", new_callable=AsyncMock),
    ):
        async with app_lifecycle(_settings(0.01), lifecycle_services, MagicMock()):
            probe.assert_not_called()
        probe.assert_not_called()
