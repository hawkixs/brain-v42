"""Tests for the memory-stats DB helper used by the cockpit endpoint."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from brain_v42.metrics.memory_stats import collect_memory_stats


@pytest.fixture
def session_factory() -> MagicMock:
    session = AsyncMock()
    session.__aenter__.return_value = session
    session.__aexit__.return_value = None
    factory = MagicMock()
    factory.return_value = session
    return factory


async def test_returns_null_shape_on_exception(session_factory: MagicMock) -> None:
    session_factory.return_value.execute.side_effect = Exception("boom")
    stats = await collect_memory_stats(session_factory)
    assert stats == {
        "episodes": 0,
        "episodic_mb": 0,
        "semantic_chunks": 0,
        "semantic_mb": 0,
        "lastCompaction": None,
        "freedLastCompaction": 0,
        "vectorIndex": None,
    }


async def test_aggregates_from_pg_queries(session_factory: MagicMock) -> None:
    exec_mock = session_factory.return_value.execute
    exec_mock.side_effect = [
        MagicMock(one=MagicMock(return_value=(14820, 482 * 1024 * 1024))),  # episodic
        MagicMock(one=MagicMock(return_value=(38420, 1240 * 1024 * 1024))),  # semantic
        MagicMock(one=MagicMock(return_value=(datetime(2026, 4, 19, 14, 31, 51, tzinfo=UTC), 38))),
        MagicMock(
            scalar=MagicMock(
                return_value="CREATE INDEX ... USING hnsw (embedding vector_cosine_ops) WITH (m='16', ef_construction='200')"
            )
        ),
    ]
    stats = await collect_memory_stats(session_factory)
    assert stats["episodes"] == 14820
    assert stats["episodic_mb"] == 482
    assert stats["semantic_chunks"] == 38420
    assert stats["semantic_mb"] == 1240
    assert stats["lastCompaction"] == "14:31:51"
    assert stats["freedLastCompaction"] == 38
    assert stats["vectorIndex"] == "hnsw (m=16, ef=200)"


async def test_null_compaction_when_no_rows(session_factory: MagicMock) -> None:
    exec_mock = session_factory.return_value.execute
    exec_mock.side_effect = [
        MagicMock(one=MagicMock(return_value=(0, 0))),
        MagicMock(one=MagicMock(return_value=(0, 0))),
        MagicMock(one=MagicMock(return_value=(None, 0))),
        MagicMock(scalar=MagicMock(return_value=None)),
    ]
    stats = await collect_memory_stats(session_factory)
    assert stats["lastCompaction"] is None
    assert stats["freedLastCompaction"] == 0
    assert stats["vectorIndex"] is None
