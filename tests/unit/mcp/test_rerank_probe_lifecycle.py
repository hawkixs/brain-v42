"""The server probes the reranker in the background, and stops with the lifecycle."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from brain_v42.config import Settings


def _settings(interval: float) -> MagicMock:
    settings = MagicMock(spec=Settings)
    settings.metrics_enabled = False
    settings.decay_enabled = False
    settings.otel_tracing_enabled = False
    settings.plan_index_refresh_enabled = False
    settings.brain_session_auto_open_enabled = False
    settings.rerank_probe_interval_seconds = interval
    return settings


def _services(reranker: object | None) -> dict[str, object]:
    indexer = MagicMock()
    indexer.index_all_projects = AsyncMock(return_value={})
    services: dict[str, object] = {
        "access_logger": MagicMock(),
        "plan_indexer": indexer,
        "neo4j_driver": None,
        "access_log_repo": MagicMock(),
        "decay_calculator": MagicMock(),
    }
    if reranker is not None:
        services["reranker_client"] = reranker
    return services


@pytest.mark.asyncio
async def test_probes_in_the_background_and_stops_at_shutdown() -> None:
    from brain_v42.mcp.server import app_lifecycle

    calls = 0
    twice = asyncio.Event()

    class FakeReranker:
        async def is_available(self) -> bool:
            nonlocal calls
            calls += 1
            if calls >= 2:
                twice.set()
            return True

        async def close(self) -> None:
            pass

    with (
        patch("brain_v42.mcp.server.close_neo4j_driver", new_callable=AsyncMock),
        patch("brain_v42.mcp.server.dispose_engine", new_callable=AsyncMock),
    ):
        async with app_lifecycle(_settings(0.01), _services(FakeReranker()), MagicMock()):
            await asyncio.wait_for(twice.wait(), timeout=2)

    seen_at_exit = calls
    await asyncio.sleep(0.05)
    assert calls == seen_at_exit, "the probe task outlived the lifecycle"


@pytest.mark.asyncio
async def test_a_hanging_probe_does_not_block_startup() -> None:
    from brain_v42.mcp.server import app_lifecycle

    class HangingReranker:
        async def is_available(self) -> bool:
            await asyncio.sleep(999)
            return True

        async def close(self) -> None:
            pass

    with (
        patch("brain_v42.mcp.server.close_neo4j_driver", new_callable=AsyncMock),
        patch("brain_v42.mcp.server.dispose_engine", new_callable=AsyncMock),
    ):
        async with asyncio.timeout(2):
            async with app_lifecycle(_settings(0.01), _services(HangingReranker()), MagicMock()):
                pass


@pytest.mark.asyncio
async def test_the_reranker_client_is_closed_after_the_probe_stops() -> None:
    """Nothing else closes it: its httpx pool would outlive the lifecycle."""
    from brain_v42.mcp.server import app_lifecycle

    events: list[str] = []
    probed = asyncio.Event()

    class FakeReranker:
        async def is_available(self) -> bool:
            events.append("probe")
            probed.set()
            return True

        async def close(self) -> None:
            events.append("close")

    with (
        patch("brain_v42.mcp.server.close_neo4j_driver", new_callable=AsyncMock),
        patch("brain_v42.mcp.server.dispose_engine", new_callable=AsyncMock),
    ):
        async with app_lifecycle(_settings(0.01), _services(FakeReranker()), MagicMock()):
            await asyncio.wait_for(probed.wait(), timeout=2)

    assert events.count("close") == 1
    assert events[-1] == "close", "the client was closed while the probe could still use it"
