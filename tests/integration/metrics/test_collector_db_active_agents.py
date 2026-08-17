"""Integration test — collect_process_metrics: active_agents + by_agent breakdown.

Task 4.1 (Plan 2, Batch 4): verifies the updated collect_process_metrics returns:
- active_processes == distinct pids (back-compat)
- active_agents == distinct real agent_names (excludes _process)
- by_agent keyed by real agent_names with aggregated call stats
- embedding sourced from _process row ONLY (no ×N)
- tools aggregated across ALL rows (real tools + pseudo-tools from _process)
- total_memory_rss_bytes from _process only

Seeds three rows for one controlled pid and deletes them in teardown.
"""

from __future__ import annotations

from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Controlled test pid — unique enough to avoid polluting other tests
# ---------------------------------------------------------------------------

_TEST_PID = 999_001  # fixed synthetic pid; deleted in teardown

_TEST_ROWS: list[dict[str, Any]] = [
    {
        "agent_name": "red-shrik",
        "pid": _TEST_PID,
        "tool_stats": {
            "brain_search": {
                "calls": 10,
                "errors": 1,
                "recent_errors": 0,
                "total_latency": 500.0,
                "total_candidates": 0,
            }
        },
        "embedding_stats": {},
        "memory_rss_bytes": 0,
    },
    {
        "agent_name": "red-codex",
        "pid": _TEST_PID,
        "tool_stats": {
            "brain_search": {
                "calls": 5,
                "errors": 0,
                "recent_errors": 1,
                "total_latency": 200.0,
                "total_candidates": 0,
            },
            "brain_learn": {
                "calls": 3,
                "errors": 0,
                "recent_errors": 0,
                "total_latency": 90.0,
                "total_candidates": 0,
            },
        },
        "embedding_stats": {},
        "memory_rss_bytes": 0,
    },
    {
        "agent_name": "_process",
        "pid": _TEST_PID,
        "tool_stats": {
            "_reranker": {
                "calls": 7,
                "errors": 0,
                "recent_errors": 0,
                "total_latency": 140.0,
                "total_candidates": 0,
            }
        },
        "embedding_stats": {
            "total_requests": 42,
            "total_errors": 2,
            "gpu_busy_errors": 1,
            "unreachable_errors": 1,
            "recent_errors": 0,
            "total_latency": 840.0,
        },
        "memory_rss_bytes": 12345,
    },
]


# ---------------------------------------------------------------------------
# Session factory fixture bound to integration engine
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def test_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


# ---------------------------------------------------------------------------
# Seed / teardown helpers
# ---------------------------------------------------------------------------


async def _seed_rows(engine: AsyncEngine) -> None:
    """Insert the three controlled test rows into process_metrics."""
    import json

    async with engine.begin() as conn:
        # Wipe the whole table, not just our pid. The assertions below count agents
        # GLOBALLY, so any row left behind by an earlier run inflates them. Until
        # 2026-08-10 the 60 s read window hid such leftovers within a minute; the
        # window now matches the 1 h retention, so the isolation has to be explicit.
        await conn.execute(text("DELETE FROM process_metrics"))
        for row in _TEST_ROWS:
            # Use CAST() instead of :: cast syntax — asyncpg rejects :: with named params
            await conn.execute(
                text(
                    "INSERT INTO process_metrics "
                    "(agent_name, pid, tool_stats, embedding_stats, memory_rss_bytes, "
                    " started_at, updated_at) "
                    "VALUES (:agent_name, :pid, CAST(:tool_stats AS jsonb), "
                    "        CAST(:embedding_stats AS jsonb), "
                    "        :memory_rss_bytes, NOW(), NOW())"
                ),
                {
                    "agent_name": row["agent_name"],
                    "pid": row["pid"],
                    "tool_stats": json.dumps(row["tool_stats"]),
                    "embedding_stats": json.dumps(row["embedding_stats"]),
                    "memory_rss_bytes": row["memory_rss_bytes"],
                },
            )


async def _cleanup_rows(engine: AsyncEngine) -> None:
    """Delete all rows for _TEST_PID — idempotent teardown."""
    async with engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM process_metrics WHERE pid = :pid"),
            {"pid": _TEST_PID},
        )


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_collect_process_metrics_active_agents_and_by_agent(
    engine: AsyncEngine,
    test_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """TDD 4.1 — verify per-agent breakdown in collect_process_metrics output.

    Seeds:
      - red-shrik (pid 999001): brain_search 10 calls, 1 error, 500ms latency
      - red-codex (pid 999001): brain_search 5 calls, 0 errors, 200ms + brain_learn 3 calls, 90ms
      - _process  (pid 999001): _reranker pseudo-tool, embedding total_requests=42, rss=12345

    Asserts:
      - active_processes == 1 (distinct pids, back-compat)
      - active_agents == 2 (red-shrik + red-codex, excludes _process)
      - by_agent has exactly red-shrik and red-codex (NOT _process)
      - by_agent["red-shrik"].calls == 10 (only brain_search)
      - by_agent["red-codex"].calls == 8 (brain_search 5 + brain_learn 3)
      - embedding.total_requests == 42 (from _process ONLY, not ×3)
      - total_memory_rss_bytes == 12345 (from _process only)
      - tools contains brain_search AND _reranker (real + pseudo-tools aggregated)
    """
    # Seed data
    await _seed_rows(engine)

    try:
        from brain_v42.metrics.collector_db import _DbCollectorsMixin

        # Build a minimal mixin instance with our test session_factory
        mixin = _DbCollectorsMixin.__new__(_DbCollectorsMixin)
        mixin._session_factory = test_session_factory  # type: ignore[attr-defined]

        result = await mixin.collect_process_metrics()

        # --- back-compat: active_processes = distinct pids ---
        assert result["active_processes"] == 1, (
            f"expected active_processes=1 (distinct pids), got {result['active_processes']}"
        )

        # --- NEW: active_agents = distinct real agents (excludes _process) ---
        assert "active_agents" in result, (
            "active_agents key missing from collect_process_metrics result"
        )
        assert result["active_agents"] == 2, (
            f"expected active_agents=2 (red-shrik + red-codex), got {result['active_agents']}"
        )

        # --- NEW: by_agent breakdown ---
        assert "by_agent" in result, "by_agent key missing from collect_process_metrics result"
        by_agent = result["by_agent"]

        # Only real agents — _process must NOT appear
        assert "_process" not in by_agent, "_process must be excluded from by_agent"
        assert "red-shrik" in by_agent, "red-shrik missing from by_agent"
        assert "red-codex" in by_agent, "red-codex missing from by_agent"

        # red-shrik: 10 calls from brain_search, 1 error
        shrik = by_agent["red-shrik"]
        assert shrik["calls"] == 10, f"red-shrik calls expected 10, got {shrik['calls']}"
        assert shrik["errors"] == 1, f"red-shrik errors expected 1, got {shrik['errors']}"
        assert shrik["recent_errors"] == 0
        assert shrik["avg_latency_ms"] == pytest.approx(50.0), (
            f"red-shrik avg_latency expected 50.0ms (500/10), got {shrik['avg_latency_ms']}"
        )

        # red-codex: 8 calls total (brain_search 5 + brain_learn 3), 0 errors, 1 recent_error
        codex = by_agent["red-codex"]
        assert codex["calls"] == 8, f"red-codex calls expected 8, got {codex['calls']}"
        assert codex["errors"] == 0
        assert codex["recent_errors"] == 1
        # avg_latency: (200 + 90) / 8 = 36.25
        assert codex["avg_latency_ms"] == pytest.approx(36.25, abs=0.1), (
            f"red-codex avg_latency expected ~36.25ms, got {codex['avg_latency_ms']}"
        )

        # --- embedding from _process ONLY (not multiplied by 3) ---
        emb = result["embedding"]
        assert emb["total_requests"] == 42, (
            f"embedding.total_requests expected 42 (_process only), got {emb['total_requests']}"
        )
        assert emb["total_errors"] == 2
        assert emb["gpu_busy_errors"] == 1
        assert emb["avg_latency_ms"] == pytest.approx(20.0, abs=0.1), (
            "embedding avg_latency expected 20.0ms (840/42)"
        )

        # --- RSS from _process only ---
        assert result["total_memory_rss_bytes"] == 12345, (
            f"expected total_memory_rss_bytes=12345, got {result['total_memory_rss_bytes']}"
        )

        # --- tools: real tools + pseudo-tools from _process (disjoint, no ×N) ---
        tools = result["tools"]
        assert "brain_search" in tools, "brain_search missing from tools block"
        assert "_reranker" in tools, (
            "_reranker (pseudo-tool from _process) missing from tools block"
        )

        # brain_search should aggregate red-shrik (10) + red-codex (5) = 15
        assert tools["brain_search"]["calls"] == 15, (
            f"brain_search calls expected 15 (10+5), got {tools['brain_search']['calls']}"
        )

    finally:
        await _cleanup_rows(engine)
