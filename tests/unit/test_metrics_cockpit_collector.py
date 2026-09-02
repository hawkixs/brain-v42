"""Tests for CockpitCollector.snapshot() — Phase 1 scope."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from brain_v42.metrics.client_activity import ClientActivityRegistry
from brain_v42.metrics.client_observation import ClientObservation
from brain_v42.metrics.cockpit import CockpitCollector
from brain_v42.metrics.codex_telemetry import ACTIVITY_TTL_SECONDS, CodexConversationRegistry
from brain_v42.metrics.collector import MetricsCollector

_CODEX_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "codex_otlp_logs.json"

_MOCK_SETTINGS = MagicMock(
    embedding_service_url="http://localhost:8003",
    embedding_dimension=1536,
)


@pytest.fixture(autouse=True)
def _patch_settings():
    with patch("brain_v42.metrics.collector.get_settings", return_value=_MOCK_SETTINGS):
        yield


_NULL_MEMORY = {
    "episodes": 0,
    "episodic_mb": 0,
    "semantic_chunks": 0,
    "semantic_mb": 0,
    "lastCompaction": None,
    "freedLastCompaction": 0,
    "vectorIndex": None,
}


def _mk_session_factory() -> MagicMock:
    """Build a MagicMock session_factory whose execute returns empty rows.

    The cockpit collector issues a retrieval-stats query against search_log;
    return zeros by default so tests focusing on other sections don't crash.
    """
    session = AsyncMock()
    session.__aenter__.return_value = session
    session.__aexit__.return_value = None
    session.execute.return_value = MagicMock(
        one=MagicMock(return_value=(0, 0)),
        fetchall=MagicMock(return_value=[]),
    )
    factory = MagicMock()
    factory.return_value = session
    return factory


@pytest.fixture
def collector() -> MetricsCollector:
    c = MetricsCollector(engine=MagicMock(), session_factory=MagicMock())
    for latency in [50, 100, 150, 200, 250]:
        c.record_tool_call("brain_search", latency_ms=float(latency))
    c.record_search(similarity_score=0.8, result_count=5)
    c.record_search_latency(40.0)
    c.push_recent_log("info", "bootstrap ok")
    return c


@pytest.fixture
def cockpit(collector: MetricsCollector) -> CockpitCollector:
    return CockpitCollector(
        collector=collector,
        session_factory=_mk_session_factory(),
    )


async def test_snapshot_has_required_top_level_keys(cockpit: CockpitCollector) -> None:
    with patch(
        "brain_v42.metrics.cockpit.collect_memory_stats",
        new=AsyncMock(return_value=dict(_NULL_MEMORY)),
    ):
        snap = await cockpit.snapshot()
    for key in [
        "version",
        "pid",
        "uptime_s",
        "endpoint",
        "metrics",
        "activeConvs",
        "tools",
        "skills",
        "memory",
        "retrieval",
        "latencyBuckets",
        "cost",
        "handoff",
        "rpsHistory",
        "p95History",
        "errHistory",
        "costHistory",
        "recent",
    ]:
        assert key in snap, f"missing top-level key: {key}"


async def test_metrics_block_has_all_required_fields(cockpit: CockpitCollector) -> None:
    with patch(
        "brain_v42.metrics.cockpit.collect_memory_stats",
        new=AsyncMock(return_value=dict(_NULL_MEMORY)),
    ):
        snap = await cockpit.snapshot()
    m = snap["metrics"]
    assert set(m.keys()) >= {
        "rps",
        "p50",
        "p95",
        "p99",
        "err_rate",
        "cache_hit",
        "active_convs",
        "ctx_tokens",
        "memory_mb",
    }
    assert isinstance(m["p95"], (int, float))
    assert m["active_convs"] == 0
    assert m["ctx_tokens"] == 0


async def test_tools_block_has_p95_and_lastErr(
    cockpit: CockpitCollector, collector: MetricsCollector
) -> None:
    collector.record_tool_call("brain_search", latency_ms=500.0, error=True)
    with patch(
        "brain_v42.metrics.cockpit.collect_memory_stats",
        new=AsyncMock(return_value=dict(_NULL_MEMORY)),
    ):
        snap = await cockpit.snapshot()
    tools = snap["tools"]
    assert isinstance(tools, list)
    assert any(t["name"] == "brain_search" for t in tools)
    brain_search = next(t for t in tools if t["name"] == "brain_search")
    assert "p95" in brain_search
    assert "err" in brain_search
    assert "calls24h" in brain_search


async def test_tier3_stubs_are_empty_arrays(cockpit: CockpitCollector) -> None:
    with patch(
        "brain_v42.metrics.cockpit.collect_memory_stats",
        new=AsyncMock(return_value=dict(_NULL_MEMORY)),
    ):
        snap = await cockpit.snapshot()
    assert snap["activeConvs"] == []
    assert snap["skills"] == []
    assert snap["handoff"] == []


async def test_codex_projection_is_cached_then_expires_after_registry_ttl(
    collector: MetricsCollector,
) -> None:
    registry_now = [100.0]
    cockpit_now = [1_000.0]
    registry = CodexConversationRegistry(
        secret=b"x" * 32,
        clock=lambda: registry_now[0],
        wall_clock=lambda: datetime(2026, 7, 20, 9, 7),
    )
    registry.ingest_otlp_json(_CODEX_FIXTURE.read_bytes())
    cockpit = CockpitCollector(
        collector=collector,
        session_factory=_mk_session_factory(),
        codex_registry=registry,
    )

    with (
        patch(
            "brain_v42.metrics.cockpit.collect_memory_stats",
            new=AsyncMock(return_value=dict(_NULL_MEMORY)),
        ),
        patch("brain_v42.metrics.cockpit.time.monotonic", side_effect=lambda: cockpit_now[0]),
    ):
        fresh = await cockpit.snapshot()
        registry_now[0] += ACTIVITY_TTL_SECONDS
        cockpit_now[0] += 0.5
        cached = await cockpit.snapshot()
        cockpit_now[0] += 0.51
        expired = await cockpit.snapshot()

    assert fresh["metrics"]["active_convs"] == 1
    assert fresh["metrics"]["ctx_tokens"] == 1234
    assert fresh["activeConvs"][0]["started"] == "09:07"
    assert cached is fresh
    assert expired["metrics"]["active_convs"] == 0
    assert expired["metrics"]["ctx_tokens"] == 0
    assert expired["activeConvs"] == []


async def test_phase2_latency_buckets_populated(
    cockpit: CockpitCollector, collector: MetricsCollector
) -> None:
    collector.record_tool_call("brain_search", latency_ms=50.0)
    collector.record_tool_call("brain_search", latency_ms=400.0)
    with patch(
        "brain_v42.metrics.cockpit.collect_memory_stats",
        new=AsyncMock(return_value=dict(_NULL_MEMORY)),
    ):
        snap = await cockpit.snapshot()
    buckets = {b["range"]: b["count"] for b in snap["latencyBuckets"]}
    assert len(buckets) == 6  # 6 fixed ranges
    assert buckets["< 100ms"] >= 1
    assert buckets["300-600ms"] >= 1


async def test_phase2_cost_today_and_by_model_populated(
    cockpit: CockpitCollector, collector: MetricsCollector
) -> None:
    collector.record_cost(model="sonnet-4.5", cost_usd=2.0)
    collector.record_cost(model="haiku-4.5", cost_usd=0.5)
    with patch(
        "brain_v42.metrics.cockpit.collect_memory_stats",
        new=AsyncMock(return_value=dict(_NULL_MEMORY)),
    ):
        snap = await cockpit.snapshot()
    assert snap["cost"]["today"] == pytest.approx(2.5)
    # byModel sorted by descending value
    assert snap["cost"]["byModel"][0] == {"m": "sonnet-4.5", "v": 2.0, "pct": 0.8}
    assert snap["cost"]["byModel"][1] == {"m": "haiku-4.5", "v": 0.5, "pct": 0.2}


async def test_phase2_histories_query_metrics_timeseries(
    collector: MetricsCollector,
) -> None:
    # Build a session_factory whose execute returns distinct rows per call.
    session = AsyncMock()
    session.__aenter__.return_value = session
    session.__aexit__.return_value = None
    # Order of DB calls in _build: retrieval stats, then 4 histories (rps/p95/err_rate/cost).
    session.execute.side_effect = [
        MagicMock(one=MagicMock(return_value=(0, 0))),  # retrieval search_log summary
        MagicMock(fetchall=MagicMock(return_value=[(2.1,), (2.4,), (2.7,)])),  # rps
        MagicMock(fetchall=MagicMock(return_value=[(312,), (298,), (280,)])),  # p95
        MagicMock(fetchall=MagicMock(return_value=[(0.004,), (0.002,), (0.001,)])),  # err_rate
        MagicMock(fetchall=MagicMock(return_value=[(0.4,), (0.62,)])),  # cost
    ]
    factory = MagicMock(return_value=session)
    c = CockpitCollector(collector=collector, session_factory=factory)
    with patch(
        "brain_v42.metrics.cockpit.collect_memory_stats",
        new=AsyncMock(return_value=dict(_NULL_MEMORY)),
    ):
        snap = await c.snapshot()
    assert snap["rpsHistory"] == [2.1, 2.4, 2.7]
    assert snap["p95History"] == [312.0, 298.0, 280.0]
    assert snap["errHistory"] == [0.004, 0.002, 0.001]
    assert snap["costHistory"] == [0.4, 0.62]


async def test_phase2_deferred_cost_fields_remain_zero(
    cockpit: CockpitCollector, collector: MetricsCollector
) -> None:
    collector.record_cost(model="sonnet-4.5", cost_usd=1.0)
    with patch(
        "brain_v42.metrics.cockpit.collect_memory_stats",
        new=AsyncMock(return_value=dict(_NULL_MEMORY)),
    ):
        snap = await cockpit.snapshot()
    # yesterday / week / month require 7-day rollup — deferred.
    assert snap["cost"]["yesterday"] == 0.0
    assert snap["cost"]["week"] == 0.0
    assert snap["cost"]["month"] == 0.0


async def test_snapshot_cached_within_ttl(cockpit: CockpitCollector) -> None:
    with patch(
        "brain_v42.metrics.cockpit.collect_memory_stats",
        new=AsyncMock(return_value=dict(_NULL_MEMORY)),
    ) as mock_mem:
        await cockpit.snapshot()
        await cockpit.snapshot()
        await cockpit.snapshot()
    assert mock_mem.call_count == 1


def _merged_registry() -> ClientActivityRegistry:
    """A registry carrying both halves at once.

    A Codex conversation coming from the OTLP, and a brain-side observation with no
    declared session — the nominal case measured on 2026-08-06. Both are needed for
    "clients arrives" and "activeConvs survives" to prove each other on the same
    payload.
    """
    registry = ClientActivityRegistry(secret=b"\x04" * 32)
    registry.ingest_otlp_json(_CODEX_FIXTURE.read_bytes())
    registry.record_observations((ClientObservation(actor="codex", session_id=None, calls=5),))
    return registry


async def test_cockpit_payload_gains_a_clients_key(collector: MetricsCollector) -> None:
    cockpit = CockpitCollector(
        collector=collector,
        session_factory=_mk_session_factory(),
        codex_registry=_merged_registry(),
    )

    with patch(
        "brain_v42.metrics.cockpit.collect_memory_stats",
        new=AsyncMock(return_value=dict(_NULL_MEMORY)),
    ):
        snap = await cockpit.snapshot()

    rows = {row["kind"]: row for row in snap["clients"]}
    assert set(rows) == {"session", "unattributed"}
    # Each "is None" has its positive control in the other row of the SAME payload:
    # without it, an entirely null payload would go green.
    assert rows["session"]["agent"] == "codex"
    assert rows["session"]["tokens"] == 1234
    assert rows["session"]["brain_calls"] is None
    assert rows["unattributed"]["actor"] == "codex"
    assert rows["unattributed"]["brain_calls"] == 5
    assert rows["unattributed"]["tokens"] is None


async def test_legacy_codex_keys_survive_next_to_clients(collector: MetricsCollector) -> None:
    """An additive contract: the shipped red-monitor dashboard still reads activeConvs.

    It switches to ``clients`` later; until then, adding the new key must remove
    nothing and move nothing.
    """
    cockpit = CockpitCollector(
        collector=collector,
        session_factory=_mk_session_factory(),
        codex_registry=_merged_registry(),
    )

    with patch(
        "brain_v42.metrics.cockpit.collect_memory_stats",
        new=AsyncMock(return_value=dict(_NULL_MEMORY)),
    ):
        snap = await cockpit.snapshot()

    assert len(snap["clients"]) == 2
    assert snap["metrics"]["active_convs"] == 1
    assert snap["metrics"]["ctx_tokens"] == 1234
    assert len(snap["activeConvs"]) == 1
    assert snap["activeConvs"][0]["agent"] == "codex"
    assert snap["activeConvs"][0]["tokens"] == 1234


async def test_cockpit_reports_an_empty_clients_list_without_a_registry(
    cockpit: CockpitCollector,
) -> None:
    """With no registry, ``clients`` exists and is the empty list, not a KeyError.

    Positive control for the two emptiness assertions: the two previous tests
    populate ``clients`` and ``activeConvs`` through the same code.
    """
    with patch(
        "brain_v42.metrics.cockpit.collect_memory_stats",
        new=AsyncMock(return_value=dict(_NULL_MEMORY)),
    ):
        snap = await cockpit.snapshot()

    assert snap["clients"] == []
    assert snap["activeConvs"] == []
    assert snap["metrics"]["active_convs"] == 0
    assert snap["metrics"]["ctx_tokens"] == 0


async def test_recent_section_matches_collector_buffer(
    cockpit: CockpitCollector, collector: MetricsCollector
) -> None:
    collector.push_recent_log("warn", "something odd")
    with patch(
        "brain_v42.metrics.cockpit.collect_memory_stats",
        new=AsyncMock(return_value=dict(_NULL_MEMORY)),
    ):
        snap = await cockpit.snapshot()
    msgs = [r["msg"] for r in snap["recent"]]
    assert "something odd" in msgs
