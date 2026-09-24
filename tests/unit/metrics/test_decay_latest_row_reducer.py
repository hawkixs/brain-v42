"""Ticket 04c09575 — the Decay card is always zero.

``_decay`` carries DB-wide GAUGES (``stale_count``/``archived_count``/
``access_log_size``), not per-process counters. Before this fix,
``collect_process_metrics`` folded every pseudo-tool — ``_decay`` included —
through the generic ``calls``/``errors``/``total_latency`` SUM reducer at
``collector_db.py``. ``_decay`` has none of those keys, so every field read
back as its ``.get(..., 0)`` default: the payload's ``decay`` block was
always ``{"calls": 0, "errors": 0, "recent_errors": 0, "avg_latency_ms": 0.0}``
in production, regardless of the real, non-zero values the flusher had
persisted (``flusher.py:117-127``).

The fix: ``_decay`` is a gauge, so cross-process aggregation must pick the
LATEST flushed row (by ``updated_at``), never sum — summing would multiply
a DB-wide count by the number of live processes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest


def _row(
    agent_name: str,
    updated_at: datetime,
    tool_stats: dict[str, Any],
) -> tuple[Any, ...]:
    """Match the SELECT column order in collect_process_metrics' SQL text."""
    return (agent_name, 1, None, updated_at, tool_stats, {}, 0, True)


async def _collect(rows: list[tuple[Any, ...]]) -> dict[str, Any]:
    from brain_v42.metrics.collector_db import _DbCollectorsMixin

    class _Result:
        def all(self) -> list[tuple[Any, ...]]:
            return rows

    class _Session:
        async def __aenter__(self) -> _Session:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def execute(self, statement: Any) -> _Result:
            return _Result()

    mixin = _DbCollectorsMixin.__new__(_DbCollectorsMixin)
    mixin._session_factory = MagicMock(return_value=_Session())  # type: ignore[attr-defined]
    result: dict[str, Any] = await mixin.collect_process_metrics()
    return result


@pytest.mark.asyncio
async def test_decay_reflects_the_latest_row_not_a_sum_and_not_zero() -> None:
    older = datetime(2026, 9, 24, 3, 0, 0, tzinfo=UTC)
    newer = datetime(2026, 9, 24, 4, 0, 0, tzinfo=UTC)

    result = await _collect(
        [
            _row(
                "_process",
                older,
                {"_decay": {"stale_count": 5, "archived_count": 2, "access_log_size": 100}},
            ),
            _row(
                "_process",
                newer,
                {"_decay": {"stale_count": 9, "archived_count": 4, "access_log_size": 250}},
            ),
        ]
    )

    # Latest row wins — not a sum (14/6/350) and not the generic-reducer zeros.
    assert result["decay"] == {
        "stale_count": 9,
        "archived_count": 4,
        "access_log_size": 250,
    }


@pytest.mark.asyncio
async def test_reranker_and_graph_keep_their_own_summed_shape_after_the_split() -> None:
    """Splitting _decay out of the generic loop must not disturb _reranker/_graph.

    Unlike _decay, these two ARE per-process counters, so summing across live
    processes remains correct for them.
    """
    t1 = datetime(2026, 9, 24, 3, 0, 0, tzinfo=UTC)
    t2 = datetime(2026, 9, 24, 4, 0, 0, tzinfo=UTC)

    result = await _collect(
        [
            _row(
                "_process",
                t1,
                {
                    "_decay": {"stale_count": 1, "archived_count": 0, "access_log_size": 10},
                    "_reranker": {
                        "calls": 2,
                        "errors": 0,
                        "recent_errors": 0,
                        "total_latency": 20.0,
                        "total_candidates": 40,
                    },
                    "_graph": {
                        "calls": 3,
                        "errors": 1,
                        "recent_errors": 0,
                        "total_latency": 9.0,
                    },
                },
            ),
            _row(
                "_process",
                t2,
                {
                    "_decay": {"stale_count": 9, "archived_count": 4, "access_log_size": 250},
                    "_reranker": {
                        "calls": 5,
                        "errors": 1,
                        "recent_errors": 1,
                        "total_latency": 50.0,
                        "total_candidates": 60,
                    },
                    "_graph": {
                        "calls": 4,
                        "errors": 0,
                        "recent_errors": 0,
                        "total_latency": 11.0,
                    },
                },
            ),
        ]
    )

    # _decay: latest-wins gauge.
    assert result["decay"] == {
        "stale_count": 9,
        "archived_count": 4,
        "access_log_size": 250,
    }
    # _reranker/_graph: still summed across processes, still shaped for server.py
    # to pop from result["tools"] into the top-level reranker/graph sections.
    assert result["tools"]["_reranker"] == {
        "calls": 7,
        "errors": 1,
        "recent_errors": 1,
        "avg_latency_ms": round(70.0 / 7, 1),
        "total_candidates": 100,
    }
    assert result["tools"]["_graph"] == {
        "calls": 7,
        "errors": 1,
        "recent_errors": 0,
        "avg_latency_ms": round(20.0 / 7, 1),
    }
    # _decay never leaks into the generic tools bag (that's server.py's job to
    # rely on — it now reads result["decay"] directly instead of popping it).
    assert "_decay" not in result["tools"]
