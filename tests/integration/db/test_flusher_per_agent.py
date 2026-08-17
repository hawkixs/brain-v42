"""Integration tests for MetricsFlusher per-agent upserts (Task 3.2, C2/H7).

Asserts the flusher writes ONE row per agent on the bare PK (agent_name):
- two real agents (red-shrik, red-codex) + one ``_process`` row,
- process-globals (embedding_stats, RSS, pseudo-tools) ONLY on ``_process``,
- per-agent rows carry their own tool_stats (no embedding, no pseudo-tools),
- a second flush of only agent B does NOT re-upsert idle agent A (dirty-set
  wiring via mark_flushed),
- ``stop()`` does NOT blanket-delete other agents' rows.

Uses a controlled, test-private pid stored in rows for back-compat queries, and
cleans up by agent_name in teardown so the test is idempotent and never
pollutes brain_test for downstream tasks.

Requires:
    POSTGRES_URL=postgresql+asyncpg://brain:brain@localhost:5433/brain_test
    BRAIN_V42_TEST_DB_URL=postgresql+asyncpg://brain:brain@localhost:5433/brain_test
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brain_v42.metrics.collector import MetricsCollector
from brain_v42.metrics.flusher import MetricsFlusher

pytestmark = pytest.mark.integration

# A high, test-private pid that no real process will own on the shared box.
_TEST_PID = 999_999_001


async def _fetch_rows(
    session_factory: async_sessionmaker[AsyncSession],
    pid: int,  # kept for call-site compatibility; no longer used in the query
) -> dict[str, dict[str, Any]]:
    """Return process_metrics rows for our test agent names, keyed by agent_name.

    pid parameter is kept so existing call sites don't need updating, but the
    query now filters by the fixed set of test agent_names since pid is no
    longer written by the flusher (migration 026 collapsed the PK to agent_name).
    """
    async with session_factory() as session:
        result = await session.execute(
            text(
                "SELECT agent_name, tool_stats, embedding_stats, memory_rss_bytes, updated_at "
                "FROM process_metrics WHERE agent_name = ANY(:names)"
            ),
            {"names": list(_TEST_AGENT_NAMES)},
        )
        rows = result.mappings().all()
    return {r["agent_name"]: dict(r) for r in rows}


_TEST_AGENT_NAMES = ("red-shrik", "red-codex", "_process")


@pytest_asyncio.fixture
async def flusher_setup(
    session_factory: async_sessionmaker[AsyncSession],
) -> Any:
    """Build a collector + flusher for per-agent upsert tests; clean rows in teardown.

    pid is stored on the flusher instance for back-compat but is no longer part
    of the PK (migration 026) or the INSERT (flusher was updated).  Teardown
    cleans by agent_name instead of pid.
    """
    collector = MetricsCollector(engine=MagicMock(), session_factory=session_factory)
    flusher = MetricsFlusher(collector, session_factory, flush_interval=30.0)
    flusher._pid = _TEST_PID  # kept so _run_loop / logging still have a pid

    # Pre-clean in case a prior crashed run left rows behind.
    async with session_factory() as session:
        await session.execute(
            text("DELETE FROM process_metrics WHERE agent_name = ANY(:names)"),
            {"names": list(_TEST_AGENT_NAMES)},
        )
        await session.commit()

    try:
        yield collector, flusher
    finally:
        async with session_factory() as session:
            await session.execute(
                text("DELETE FROM process_metrics WHERE agent_name = ANY(:names)"),
                {"names": list(_TEST_AGENT_NAMES)},
            )
            await session.commit()


@pytest.mark.asyncio
async def test_flush_writes_one_row_per_agent(
    flusher_setup: tuple[MetricsCollector, MetricsFlusher],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """ONE flush → exactly 3 rows keyed (agent_name, pid): two agents + _process.

    embedding_stats + pseudo-tools live ONLY on _process; per-agent rows carry
    their own tool_stats and an EMPTY embedding_stats.
    """
    collector, flusher = flusher_setup

    collector.record_tool_call("brain_search", latency_ms=12.0, agent="red-shrik")
    collector.record_tool_call("brain_log_decision", latency_ms=8.0, agent="red-codex")
    collector.record_embedding_request(latency_ms=20.0)
    collector.record_reranker_call(latency_ms=5.0, candidate_count=7)

    await flusher._flush()

    rows = await _fetch_rows(session_factory, _TEST_PID)
    assert set(rows.keys()) == {"red-shrik", "red-codex", "_process"}, (
        f"expected 3 rows (red-shrik, red-codex, _process), got {sorted(rows.keys())}"
    )

    # Per-agent rows: own tool_stats, empty embedding, no pseudo-tools, rss=0.
    shrik = rows["red-shrik"]
    assert "brain_search" in shrik["tool_stats"]
    assert shrik["embedding_stats"] == {}
    assert "_reranker" not in shrik["tool_stats"]
    assert shrik["memory_rss_bytes"] == 0

    codex = rows["red-codex"]
    assert "brain_log_decision" in codex["tool_stats"]
    assert codex["embedding_stats"] == {}

    # _process row: embedding populated, reranker pseudo-tool present, rss set.
    proc = rows["_process"]
    assert proc["embedding_stats"]["total_requests"] == 1
    assert "_reranker" in proc["tool_stats"]
    assert proc["tool_stats"]["_reranker"]["calls"] == 1
    # _process must NOT carry real-agent tools.
    assert "brain_search" not in proc["tool_stats"]


@pytest.mark.asyncio
async def test_second_flush_only_dirty_agent_ages_idle(
    flusher_setup: tuple[MetricsCollector, MetricsFlusher],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A second flush after activity on ONLY agent B leaves A's row untouched.

    Proves the dirty-set wiring: mark_flushed() clears flushed agents, so an
    idle A is not re-upserted and its updated_at does not advance.
    """
    collector, flusher = flusher_setup

    collector.record_tool_call("brain_search", latency_ms=12.0, agent="red-shrik")
    collector.record_tool_call("brain_search", latency_ms=9.0, agent="red-codex")
    await flusher._flush()

    rows_1 = await _fetch_rows(session_factory, _TEST_PID)
    a_updated_1 = rows_1["red-shrik"]["updated_at"]

    # Only agent B is active in the next cycle.
    collector.record_tool_call("brain_search", latency_ms=11.0, agent="red-codex")
    await flusher._flush()

    rows_2 = await _fetch_rows(session_factory, _TEST_PID)
    # A's row still exists and was NOT re-upserted (timestamp unchanged).
    assert rows_2["red-shrik"]["updated_at"] == a_updated_1, (
        "idle agent A was re-upserted — dirty-set wiring broken"
    )
    # B's row advanced.
    assert rows_2["red-codex"]["updated_at"] > rows_1["red-codex"]["updated_at"], (
        "active agent B was not re-upserted"
    )


@pytest.mark.asyncio
async def test_stop_does_not_delete_other_agents_rows(
    flusher_setup: tuple[MetricsCollector, MetricsFlusher],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """stop() must NOT blanket-delete rows by pid — other agents stay valid."""
    collector, flusher = flusher_setup

    collector.record_tool_call("brain_search", latency_ms=12.0, agent="red-shrik")
    collector.record_tool_call("brain_search", latency_ms=9.0, agent="red-codex")
    await flusher._flush()

    rows_before = await _fetch_rows(session_factory, _TEST_PID)
    assert {"red-shrik", "red-codex", "_process"} <= set(rows_before.keys())

    await flusher.stop()

    rows_after = await _fetch_rows(session_factory, _TEST_PID)
    assert set(rows_after.keys()) == set(rows_before.keys()), (
        "stop() deleted rows — the per-shutdown blanket DELETE WHERE pid must be gone"
    )
