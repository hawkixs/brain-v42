"""Configured backend survives idle flushes and wins over stale probes."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from brain_v42.config import Settings
from brain_v42.metrics.collector import MetricsCollector
from brain_v42.metrics.flusher import MetricsFlusher


@pytest.mark.parametrize("backend", ["none", "cohere", "shim"])
def test_configured_backend_is_reported_and_flushed_when_idle(backend: str) -> None:
    settings = Settings(
        postgres_url="postgresql+asyncpg://test:test@localhost/brain_unit", _env_file=None
    )
    with patch("brain_v42.metrics.collector.get_settings", return_value=settings):
        collector = MetricsCollector(MagicMock(), MagicMock())
    collector.record_rerank_backend(backend)
    metrics = collector.get_metrics()["reranker"]
    assert metrics["backend"] == backend
    assert metrics["by_identity"] == {}
    for key in ("last_probe_ok", "last_probe_reason", "last_probe_age_s"):
        assert metrics[key] is None
    process = collector.get_flush_data()["_process"]
    persisted = MetricsFlusher._process_pseudo_tools(process)["_reranker"]
    assert persisted["backend"] == backend
    assert persisted["calls"] == 0
    assert persisted["by_identity"] == {}
    assert persisted.get("last_probe") is None


@pytest.mark.parametrize("reverse", [False, True])
async def test_latest_backend_wins_across_processes_and_none_masks_old_probe(reverse: bool) -> None:
    from brain_v42.metrics.collector_db import _DbCollectorsMixin

    now = datetime.now(UTC)
    older = {
        "backend": "cohere",
        "calls": 1,
        "recent_errors": 1,
        "by_identity": {"cohere:m": {"operations": 1, "operations_by_outcome": {"http_error": 1}}},
        "last_probe": {
            "ok": False,
            "reason": "http_401",
            "at": now.timestamp(),
            "identity": "cohere:m",
        },
    }
    rows = [
        ("_process", 1, None, now - timedelta(seconds=1), {"_reranker": older}, {}, 0, True),
        (
            "_process",
            2,
            None,
            now,
            {"_reranker": {"backend": "none", "by_identity": {}}},
            {},
            0,
            True,
        ),
    ]
    session = AsyncMock()
    session.execute.return_value = MagicMock(
        all=MagicMock(return_value=list(reversed(rows)) if reverse else rows)
    )
    context = MagicMock(__aenter__=AsyncMock(return_value=session), __aexit__=AsyncMock())
    collector = _DbCollectorsMixin.__new__(_DbCollectorsMixin)
    collector._session_factory = MagicMock(return_value=context)
    stats = (await collector.collect_process_metrics())["tools"]["_reranker"]
    assert stats["backend"] == "none"
    assert stats["by_identity"] == {}
    assert stats.get("last_probe") is None
