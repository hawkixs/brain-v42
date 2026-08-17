"""Integration contract test — /metrics `cross_process` block shape (Plan 2, Task 4.2).

Strategy: build a real MetricsCollector + MetricsServer and call GET /metrics via
the aiohttp test client (same pattern as tests/unit/test_metrics_server.py).
`collect_process_metrics` is NOT mocked — it runs against brain_test with seeded rows
so that server.py's full assembly logic (including `_reranker`/`_graph` pop and
top-level relocation, lines 90-122) is exercised.

Why not call `collect_process_metrics` directly?
The `_handle_metrics` handler mutates `process_agg["tools"]` in-place
(``agg_tools.pop("_reranker", None)`` / ``pop("_graph", None)``) BEFORE assigning
the dict to ``metrics["cross_process"]``.  So in the real /metrics payload,
`_reranker`/`_graph` are ABSENT from `cross_process.tools` and appear instead at
top-level `reranker`/`graph`.  Bypassing server.py would assert a shape the real
endpoint does NOT produce.

Seeds two real agents + `_process` (with a `_reranker` pseudo-tool) for a unique
synthetic pid, then deletes them in teardown (idempotent).
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Controlled test data — unique pid avoids colliding with test_collector_db
# ---------------------------------------------------------------------------

_CONTRACT_PID = 999_042  # distinct from 999_001 used in test_collector_db_active_agents

_CONTRACT_ROWS: list[dict[str, Any]] = [
    {
        "agent_name": "red-monitor",
        "pid": _CONTRACT_PID,
        "tool_stats": {
            "brain_search": {
                "calls": 20,
                "errors": 2,
                "recent_errors": 1,
                "total_latency": 1000.0,
                "total_candidates": 0,
            },
            "brain_learn": {
                "calls": 4,
                "errors": 0,
                "recent_errors": 0,
                "total_latency": 200.0,
                "total_candidates": 0,
            },
        },
        "embedding_stats": {},
        "memory_rss_bytes": 0,
    },
    {
        "agent_name": "red-data",
        "pid": _CONTRACT_PID,
        "tool_stats": {
            "brain_search": {
                "calls": 6,
                "errors": 0,
                "recent_errors": 0,
                "total_latency": 300.0,
                "total_candidates": 0,
            },
        },
        "embedding_stats": {},
        "memory_rss_bytes": 0,
    },
    {
        # _process row: carries embedding stats + _reranker pseudo-tool.
        # Seeding _reranker here exercises server.py's pop-and-relocate path
        # (lines 96-105): _reranker is removed from cross_process.tools and
        # placed at the top-level `reranker` key instead.
        "agent_name": "_process",
        "pid": _CONTRACT_PID,
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
            "total_requests": 30,
            "total_errors": 1,
            "gpu_busy_errors": 0,
            "unreachable_errors": 1,
            "recent_errors": 0,
            "total_latency": 600.0,
        },
        "memory_rss_bytes": 8192,
    },
]

_MOCK_SETTINGS = MagicMock(
    embedding_service_url="http://localhost:8003",
    embedding_dimension=1024,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def contract_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


# ---------------------------------------------------------------------------
# Seed / teardown helpers
# ---------------------------------------------------------------------------


async def _seed_contract_rows(engine: AsyncEngine) -> None:
    """Insert test rows for _CONTRACT_PID. Idempotent (DELETE first)."""
    async with engine.begin() as conn:
        # Wipe the whole table, not just our pid — the contract below asserts GLOBAL
        # counts, so a row left by an earlier run inflates them. The 60 s read window
        # used to hide leftovers within a minute; since 2026-08-10 it matches the 1 h
        # retention, so the isolation has to be explicit.
        await conn.execute(text("DELETE FROM process_metrics"))
        for row in _CONTRACT_ROWS:
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


async def _cleanup_contract_rows(engine: AsyncEngine) -> None:
    """Delete all rows for _CONTRACT_PID. Idempotent teardown."""
    async with engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM process_metrics WHERE pid = :pid"),
            {"pid": _CONTRACT_PID},
        )


# ---------------------------------------------------------------------------
# Contract test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cross_process_contract_shape(
    aiohttp_client: Any,
    engine: AsyncEngine,
    contract_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Lock the `cross_process` block shape produced by the real /metrics assembly.

    Uses the aiohttp test client against the real MetricsServer._build_app() so
    that server.py's full handler (_handle_metrics) runs — including the
    `_reranker`/`_graph` pop-and-relocate logic that mutates cross_process.tools.

    Seeds (pid 999042):
      - red-monitor: brain_search 20c/2e + brain_learn 4c — 24 total calls
      - red-data:    brain_search 6c/0e — 6 total calls
      - _process:    _reranker pseudo-tool (7 calls) + embedding total_requests=30, rss=8192

    Contract assertions (real /metrics payload, cross_process block):
      1. active_processes (int) — back-compat
      2. active_agents    (int) — excludes _process
      3. by_agent         (dict) — keyed by real agent names; _process absent
      4. Per-agent entry shape: {calls:int, errors:int, recent_errors:int, avg_latency_ms:float}
      5. cross_process.tools does NOT contain _reranker (server.py relocated it to top-level)
      6. Top-level `reranker` key IS present (relocation target)
      7. Embedding block present with required keys
    """
    await _seed_contract_rows(engine)

    try:
        from brain_v42.metrics.collector import MetricsCollector
        from brain_v42.metrics.server import MetricsServer

        with patch("brain_v42.metrics.collector.get_settings", return_value=_MOCK_SETTINGS):
            collector = MetricsCollector(
                engine=engine,
                session_factory=contract_session_factory,
            )
            # Stub async methods that are NOT under test (DB stats, search quality,
            # dream metrics) to avoid hitting other tables and to keep the test focused.
            # collect_process_metrics is NOT stubbed — it reads the real seeded rows.
            collector.collect_db_stats = AsyncMock(  # type: ignore[method-assign]
                return_value={
                    "pool": {"active": 0, "idle": 0, "overflow": 0, "max": 15},
                    "tables": {},
                    "db_size_bytes": 0,
                    "dimension_mismatches": 0,
                }
            )
            collector.collect_search_quality = AsyncMock(  # type: ignore[method-assign]
                return_value={
                    "searches_total": 0,
                    "searches_with_zero_results": 0,
                    "avg_score": 0.0,
                }
            )
            collector.collect_dream_metrics = AsyncMock(  # type: ignore[method-assign]
                return_value={}
            )

            mock_embedding_svc = MagicMock()
            mock_embedding_svc.healthcheck = AsyncMock(return_value=True)

            server = MetricsServer(collector, mock_embedding_svc, port=0, host="127.0.0.1")
            client = await aiohttp_client(server._build_app())

            resp = await client.get("/metrics")
            assert resp.status == 200
            payload = await resp.json()

        # ------------------------------------------------------------------
        # Extract the cross_process block (this IS what red-monitor reads)
        # ------------------------------------------------------------------
        assert "cross_process" in payload, "top-level 'cross_process' key missing from /metrics"
        cross_process = payload["cross_process"]

        # ------------------------------------------------------------------
        # 1. active_processes — int, back-compat field
        # ------------------------------------------------------------------
        assert "active_processes" in cross_process, (
            "cross_process block is missing required field 'active_processes'"
        )
        assert isinstance(cross_process["active_processes"], int), (
            f"active_processes must be int, got {type(cross_process['active_processes'])}"
        )
        assert cross_process["active_processes"] == 1  # one distinct pid seeded

        # ------------------------------------------------------------------
        # 2. active_agents — int, excludes _process
        # ------------------------------------------------------------------
        assert "active_agents" in cross_process, (
            "cross_process block is missing required field 'active_agents' (Plan 2 regression)"
        )
        assert isinstance(cross_process["active_agents"], int), (
            f"active_agents must be int, got {type(cross_process['active_agents'])}"
        )
        assert cross_process["active_agents"] == 2  # red-monitor + red-data

        # ------------------------------------------------------------------
        # 3. by_agent — dict, keyed by real agents only
        # ------------------------------------------------------------------
        assert "by_agent" in cross_process, (
            "cross_process block is missing required field 'by_agent' (Plan 2 regression)"
        )
        by_agent = cross_process["by_agent"]
        assert isinstance(by_agent, dict), f"by_agent must be a dict, got {type(by_agent)}"
        assert "_process" not in by_agent, "_process sentinel must not appear in by_agent"
        assert "red-monitor" in by_agent, "red-monitor missing from by_agent"
        assert "red-data" in by_agent, "red-data missing from by_agent"

        # ------------------------------------------------------------------
        # 4. Per-agent entry shape contract
        # ------------------------------------------------------------------
        required_agent_fields = {"calls", "errors", "recent_errors", "avg_latency_ms"}
        for agent_name, entry in by_agent.items():
            missing = required_agent_fields - entry.keys()
            assert not missing, f"by_agent['{agent_name}'] missing required fields: {missing}"
            assert isinstance(entry["calls"], int), f"by_agent['{agent_name}'].calls must be int"
            assert isinstance(entry["errors"], int), f"by_agent['{agent_name}'].errors must be int"
            assert isinstance(entry["recent_errors"], int), (
                f"by_agent['{agent_name}'].recent_errors must be int"
            )
            assert isinstance(entry["avg_latency_ms"], float), (
                f"by_agent['{agent_name}'].avg_latency_ms must be float"
            )

        # ------------------------------------------------------------------
        # 5. Sanity-check seeded values for each agent
        # ------------------------------------------------------------------
        monitor = by_agent["red-monitor"]
        assert monitor["calls"] == 24  # brain_search 20 + brain_learn 4
        assert monitor["errors"] == 2
        assert monitor["recent_errors"] == 1
        # avg = (1000 + 200) / 24 = 50.0 ms
        assert monitor["avg_latency_ms"] == pytest.approx(50.0, abs=0.01)

        data_agent = by_agent["red-data"]
        assert data_agent["calls"] == 6
        assert data_agent["errors"] == 0
        # avg = 300 / 6 = 50.0 ms
        assert data_agent["avg_latency_ms"] == pytest.approx(50.0, abs=0.01)

        # ------------------------------------------------------------------
        # 6. cross_process.tools: _reranker ABSENT (server.py popped it to top-level)
        #    Top-level `reranker` key IS present (the relocation target).
        # ------------------------------------------------------------------
        assert "tools" in cross_process, "cross_process missing 'tools' block"
        assert "_reranker" not in cross_process["tools"], (
            "_reranker must NOT be in cross_process.tools — "
            "server.py pops it to the top-level 'reranker' key"
        )
        assert "reranker" in payload, (
            "top-level 'reranker' key missing — server.py should have relocated it from "
            "cross_process.tools._reranker"
        )

        # ------------------------------------------------------------------
        # 7. Embedding block present with required keys
        # ------------------------------------------------------------------
        assert "embedding" in cross_process, "cross_process missing 'embedding' block"
        emb = cross_process["embedding"]
        required_emb_fields = {
            "total_requests",
            "total_errors",
            "gpu_busy_errors",
            "unreachable_errors",
            "recent_errors",
            "avg_latency_ms",
        }
        missing_emb = required_emb_fields - emb.keys()
        assert not missing_emb, f"cross_process.embedding missing required fields: {missing_emb}"
        assert emb["total_requests"] == 30  # from _process row only
        assert emb["unreachable_errors"] == 1
        assert emb["avg_latency_ms"] == pytest.approx(20.0, abs=0.01)  # 600 / 30

        # ------------------------------------------------------------------
        # 8. Other top-level keys that must be present
        # ------------------------------------------------------------------
        assert "total_memory_rss_bytes" in cross_process, (
            "cross_process missing 'total_memory_rss_bytes'"
        )
        assert cross_process["total_memory_rss_bytes"] == 8192

    finally:
        await _cleanup_contract_rows(engine)
