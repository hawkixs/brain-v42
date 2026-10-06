"""Backend counters keep retries separate from search fallback operations."""

from unittest.mock import MagicMock

import pytest

from brain_v42.config import Settings
from brain_v42.metrics.collector import MetricsCollector
from brain_v42.metrics.flusher import MetricsFlusher


@pytest.fixture
def collector(monkeypatch):
    settings = Settings(
        postgres_url="postgresql+asyncpg://test:test@localhost/test", _env_file=None
    )
    monkeypatch.setattr("brain_v42.metrics.collector.get_settings", lambda: settings)
    return MetricsCollector(MagicMock(), MagicMock())


def test_identity_counters_latencies_and_labels(collector) -> None:
    identity = "cohere:voyageai/rerank-3-lite"
    for outcome, latency in [("http_429", 10), ("http_503", 20), ("ok", 30)]:
        collector.on_attempt(identity, outcome, latency)
    collector.on_retry(identity, "http_429")
    collector.on_retry(identity, "http_503")
    collector.on_operation(identity, "ok", 150)
    collector.on_operation(identity, "budget_exhausted", 250)
    collector.on_operation("shim", "parse_error", 5)
    collector.on_operation("shim", "http_error", 10)
    collector.on_operation("shim", "transport_error", 15)
    collector.on_attempt("shim", "ok", 5)
    stats = collector.get_metrics()["reranker"]["by_identity"]
    cohere = stats[identity]
    assert cohere["backend"] == "cohere"
    assert cohere["model"] == "voyageai/rerank-3-lite"
    assert cohere["operations"] == 2
    assert cohere["operations_by_outcome"] == {"ok": 1, "budget_exhausted": 1}
    assert cohere["attempts"] == 3
    assert cohere["retries"] == 2
    assert cohere["status_429"] == 1
    assert cohere["status_5xx"] == 1
    assert cohere["budget_exhausted"] == 1
    assert cohere["fallback_rate"] == 0.5
    assert cohere["attempt_latency_ms"]["p50"] == 20
    assert cohere["attempt_latency_ms"]["p95"] == 30
    assert cohere["operation_latency_ms"]["p50"] == 250
    assert cohere["operation_latency_ms"]["p95"] == 250
    assert stats["shim"]["backend"] == "shim"
    assert stats["shim"]["model"] == ""
    assert stats["shim"]["fallback_rate"] == 1.0
    flushed = collector.get_flush_data()["_process"]["reranker"]
    assert flushed["by_identity"] == stats
    assert flushed["total_calls"] == 0


def test_probe_state_age_and_no_operations(collector, monkeypatch) -> None:
    clock = [100.0]
    monkeypatch.setattr("brain_v42.metrics.collector.time.monotonic", lambda: clock[0])
    before = collector.get_metrics()["reranker"]
    assert before["last_probe_ok"] is None
    assert before["last_probe_age_s"] is None
    collector.on_probe("cohere:m", False, "http_401")
    clock[0] += 3
    after = collector.get_metrics()["reranker"]
    assert after["last_probe_ok"] is False
    assert after["last_probe_reason"] == "http_401"
    assert after["last_probe_age_s"] == 3
    assert after["by_identity"] == {}
    probe = collector.get_flush_data()["_process"]["reranker"]["last_probe"]["cohere:m"]
    assert probe == {"ok": False, "reason": "http_401", "monotonic": 100.0}
    collector.record_rerank_probe("cohere:m", True, "ok")
    assert collector.get_metrics()["reranker"]["last_probe_ok"] is True


def test_record_methods_and_aliases_share_counters(collector) -> None:
    collector.record_rerank_attempt("shim", "ok", 5)
    collector.record_rerank_retry("shim", "http_503")
    collector.record_rerank_operation("shim", "ok", 10)
    collector.on_attempt("shim", "http_503", 15)
    collector.on_retry("shim", "http_503")
    collector.on_operation("shim", "http_error", 20)
    stats = collector.get_metrics()["reranker"]["by_identity"]["shim"]
    assert stats["attempts"] == 2
    assert stats["retries"] == 2
    assert stats["operations"] == 2
    assert stats["status_5xx"] == 1


@pytest.mark.parametrize("legacy_calls", [0, 2])
def test_flusher_retains_legacy_keys_and_flushes_operations_without_legacy_calls(
    legacy_calls,
) -> None:
    identities = {"cohere:m": {"operations": 2}}
    entry = {
        "reranker": {
            "total_calls": legacy_calls,
            "total_errors": 1,
            "recent_errors": 1,
            "total_latency": 12,
            "total_candidates": 3,
            "by_identity": identities,
        }
    }
    assert MetricsFlusher._process_pseudo_tools(entry)["_reranker"] == {
        "calls": legacy_calls,
        "errors": 1,
        "recent_errors": 1,
        "total_latency": 12,
        "total_candidates": 3,
        "by_identity": identities,
    }


def test_flusher_does_not_create_reranker_row_without_operations() -> None:
    tools = MetricsFlusher._process_pseudo_tools({"reranker": {"by_identity": {}}})
    assert "_reranker" not in tools
