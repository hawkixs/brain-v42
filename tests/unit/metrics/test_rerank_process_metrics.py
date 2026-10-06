"""The sidecar must retain rerank observations written by other processes."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from brain_v42.metrics.collector_db import _DbCollectorsMixin


async def _collect(rerankers: list[dict[str, Any]]) -> dict[str, Any]:
    rows = [
        ("_process", pid, None, None, {"_reranker": stats}, {}, 0, True)
        for pid, stats in enumerate(rerankers)
    ]
    session = AsyncMock()
    session.execute.return_value = MagicMock(all=MagicMock(return_value=rows))
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=session)
    context.__aexit__ = AsyncMock(return_value=None)
    collector = _DbCollectorsMixin.__new__(_DbCollectorsMixin)
    collector._session_factory = MagicMock(return_value=context)  # type: ignore[attr-defined]
    return await collector.collect_process_metrics()


@pytest.mark.parametrize("reverse", [False, True])
async def test_collect_process_metrics_preserves_identity_stats_and_latest_probe(
    reverse: bool,
) -> None:
    identity = "cohere:voyageai/rerank-3-lite"
    older = {"ok": True, "reason": "ok", "at": 1000.0, "identity": "shim"}
    latest = {"ok": False, "reason": "http_401", "at": 1002.0, "identity": identity}
    first = {
        "operations": 4,
        "attempts": 7,
        "retries": 3,
        "status_429": 1,
        "status_5xx": 2,
        "budget_exhausted": 1,
        "operations_by_outcome": {"ok": 3, "budget_exhausted": 1},
        "fallback_rate": 0.25,
        "backend": "cohere",
        "model": "voyageai/rerank-3-lite",
        "attempt_latency_ms": {"p50": 10.0, "p95": 50.0, "p99": 90.0},
        "operation_latency_ms": {"p50": 80.0, "p95": 100.0},
    }
    second = {
        **first,
        "operations": 2,
        "attempts": 3,
        "retries": 1,
        "status_429": 2,
        "status_5xx": 1,
        "budget_exhausted": 0,
        "operations_by_outcome": {"ok": 1, "http_error": 1},
        "fallback_rate": 0.5,
        "attempt_latency_ms": {"p50": 20.0, "p95": 40.0, "p99": 120.0},
        "operation_latency_ms": {"p50": 40.0, "p95": 200.0, "p99": 300.0},
    }
    rows = [
        {"calls": 2, "total_latency": 20.0, "by_identity": {identity: first}, "last_probe": older},
        {
            "calls": 1,
            "total_latency": 40.0,
            "by_identity": {identity: second},
            "last_probe": latest,
        },
        {"calls": 3, "errors": 1, "recent_errors": 1, "total_latency": 60.0, "total_candidates": 9},
    ]
    result = await _collect(list(reversed(rows)) if reverse else rows)
    stats = result["tools"]["_reranker"]
    assert stats["calls"] == 6
    assert stats["errors"] == 1
    assert stats["recent_errors"] == 1
    assert stats["avg_latency_ms"] == 20.0
    assert stats["total_candidates"] == 9
    assert stats["by_identity"] == {
        identity: {
            "operations": 6,
            "attempts": 10,
            "retries": 4,
            "status_429": 3,
            "status_5xx": 3,
            "budget_exhausted": 1,
            "operations_by_outcome": {"ok": 4, "budget_exhausted": 1, "http_error": 1},
            "fallback_rate": pytest.approx(1 / 3),
            "backend": "cohere",
            "model": "voyageai/rerank-3-lite",
            "attempt_latency_ms": {"p50": 20.0, "p95": 50.0, "p99": 120.0},
            "operation_latency_ms": {"p50": 80.0, "p95": 200.0, "p99": 300.0},
        }
    }
    assert stats["last_probe"] == latest


async def test_collect_process_metrics_keeps_legacy_reranker_shape() -> None:
    result = await _collect([{"calls": 2, "errors": 1, "recent_errors": 1, "total_latency": 20.0}])
    assert result["tools"]["_reranker"] == {
        "calls": 2,
        "errors": 1,
        "recent_errors": 1,
        "avg_latency_ms": 10.0,
    }


async def test_collect_process_metrics_handles_attempts_and_probe_without_operations() -> None:
    probe = {"ok": True, "reason": "ok", "at": 1000.0, "identity": "shim"}
    result = await _collect(
        [
            {"by_identity": {"shim": {"backend": "shim", "model": "", "attempts": 1}}},
            {"last_probe": probe},
        ]
    )
    stats = result["tools"]["_reranker"]
    assert stats["calls"] == 0
    assert stats["by_identity"]["shim"]["operations"] == 0
    assert stats["by_identity"]["shim"]["attempts"] == 1
    assert stats["by_identity"]["shim"]["fallback_rate"] == 0.0
    assert stats["last_probe"] == probe
