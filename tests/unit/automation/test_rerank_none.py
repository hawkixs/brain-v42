"""Both auxiliary runtimes accept the no-reranker rollback."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from brain_v42.config import Settings


@pytest.mark.parametrize("runtime_kind", ["automation", "metrics"])
async def test_none_runtime_builds_and_dedup_signals_nothing(runtime_kind: str) -> None:
    settings = Settings(
        postgres_url="postgresql+asyncpg://test:test@localhost/brain_unit",
        rerank_backend="none",
        graph_enabled=False,
        metrics_legacy_automation_enabled=True,
        _env_file=None,
    )
    engine = MagicMock(dispose=AsyncMock())
    embedding = MagicMock(close=AsyncMock())
    if runtime_kind == "automation":
        from brain_v42.automation.runtime import build_automation_runtime

        with patch("brain_v42.automation.runtime.build_embedding_service", return_value=embedding):
            runtime = build_automation_runtime(settings=settings, engine=engine)
        resources = runtime._resources
        resources.server.stop = AsyncMock()
        resources.lease.release = AsyncMock()
        assert resources.reranker is None
        assert await resources.dedup_job.find_candidates("brain-v42") == []
        assert await runtime._cleanup(None) == []
    else:
        from brain_v42.metrics.runtime import build_metrics_runtime

        with (
            patch("brain_v42.metrics.runtime.build_embedding_service", return_value=embedding),
            patch("brain_v42.metrics.collector.get_settings", return_value=settings),
        ):
            runtime = build_metrics_runtime(settings=settings, engine=engine)
        legacy = runtime._resources.legacy_factory(runtime._resources.lease)
        assert legacy.reranker is None
        assert await legacy.dedup_job.find_candidates("brain-v42") == []
