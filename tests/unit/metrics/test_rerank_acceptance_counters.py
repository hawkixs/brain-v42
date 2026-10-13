"""Search counters and emitter generations survive the persistence boundary."""

from unittest.mock import MagicMock

import pytest

from brain_v42.config import Settings
from brain_v42.metrics.collector import MetricsCollector
from brain_v42.metrics.flusher import MetricsFlusher


def test_chain_search_counters_and_stable_generation(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(
        postgres_url="postgresql+asyncpg://test:test@localhost/test", _env_file=None
    )
    monkeypatch.setattr("brain_v42.metrics.collector.get_settings", lambda: settings)
    monkeypatch.setattr("brain_v42.metrics.collector.time.time", lambda: 1000.0)
    collector = MetricsCollector(MagicMock(), MagicMock())
    collector.on_chain("primary", "cohere:primary")
    collector.on_handover()
    collector.on_chain("fallback", "cohere:fallback")
    collector.on_handover()
    collector.on_chain("failed_all", None)
    collector.record_search_rerank(eligible=True, rrf=False)
    collector.record_search_rerank(eligible=True, rrf=True)
    collector.record_search_rerank(eligible=False, rrf=False)
    monkeypatch.setattr("brain_v42.metrics.collector.time.time", lambda: 1001.0)
    snapshot = collector.get_metrics()
    expected_chain = {
        "operations": 3,
        "served_by": {"cohere:primary": 1, "cohere:fallback": 1},
        "handovers": 2,
        "failed_all": 1,
    }
    expected_search = {
        "rerank_searches": 3,
        "rerank_eligible": 2,
        "rerank_rrf": 1,
        "emitter_started_at": 1000.0,
    }
    assert snapshot["reranker"]["chain"] == expected_chain
    assert snapshot["search"] == expected_search
    flush = collector.get_flush_data()["_process"]
    assert flush["reranker"]["chain"] == expected_chain
    assert flush["search"] == expected_search
    tools = MetricsFlusher._process_pseudo_tools(flush)
    assert tools["_reranker"]["chain"] == expected_chain
    assert tools["_search_rerank"] == expected_search
    assert collector.get_metrics()["search"]["emitter_started_at"] == 1000.0
    successor = MetricsCollector(MagicMock(), MagicMock())
    assert successor.get_metrics()["search"]["emitter_started_at"] == 1001.0
    # Snapshots must not hand out the collector's mutable served_by state.
    snapshot["reranker"]["chain"]["served_by"].clear()
    assert collector.get_metrics()["reranker"]["chain"] == expected_chain


def test_search_generation_flushes_before_first_search(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(
        postgres_url="postgresql+asyncpg://test:test@localhost/test", _env_file=None
    )
    monkeypatch.setattr("brain_v42.metrics.collector.get_settings", lambda: settings)
    collector = MetricsCollector(MagicMock(), MagicMock())
    search = MetricsFlusher._process_pseudo_tools(collector.get_flush_data()["_process"])[
        "_search_rerank"
    ]
    assert search["rerank_searches"] == search["rerank_eligible"] == search["rerank_rrf"] == 0
    assert search["emitter_started_at"] > 0
