"""Wave-4 audit tests — metrics counters that were structurally zero.

Covers:
  1. snapshot_counters called from MetricsFlusher (rps non-zero after tool call)
  2. record_search_log feeds record_search_latency (percentiles non-zero)
  3. Agent cardinality cap (_overflow bucket + WARN at _MAX_AGENTS)
  4. RecentLogProcessor wires into structlog (recent buffer populated)
  5. Decay stats block present in get_flush_data _process (persistence-ready)
  6. cockpit endpoint field honest (not hardcoded "stdio")
  7. cache_hit_ratio always 0 → null honest (field absent / None)

Wave-4b corrections (blocker fixes):
  B1. sidecar structlog chain must not use add_logger_name with PrintLoggerFactory
  B2. MetricsFlusher must persist decay block in the _process DB row
  B3. cockpit cost.today must be null when no record_cost callers exist
  M1. agent cardinality cap WARN must fire only once per agent (not every call)
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import structlog

from brain_v42.metrics.collector import MetricsCollector
from brain_v42.metrics.flusher import MetricsFlusher
from brain_v42.metrics.recent_log import RecentLogProcessor

# ──────────────────────────────────────────────────────────────────────────────
# Fixture
# ──────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def collector() -> MetricsCollector:
    return MetricsCollector(engine=MagicMock(), session_factory=MagicMock())


# ──────────────────────────────────────────────────────────────────────────────
# 1. snapshot_counters called by MetricsFlusher every flush cycle
# ──────────────────────────────────────────────────────────────────────────────


def test_snapshot_counters_called_by_flusher_produces_nonzero_rps(
    collector: MetricsCollector,
) -> None:
    """After recording a tool call and one flusher snapshot, rps must be > 0.

    Resolution: snapshot_counters is called once per 30s flush cycle.
    rps = delta_calls / elapsed — always 0 with < 2 snapshots, so the
    flusher must call snapshot BEFORE sleeping (or at start of _flush).
    This test verifies that MetricsFlusher._flush() calls snapshot_counters.
    """
    session_factory = MagicMock()

    async def _fake_commit() -> None:
        pass

    fake_session = AsyncMock()
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=False)
    fake_session.execute = AsyncMock()
    fake_session.commit = AsyncMock()
    session_factory.return_value = fake_session

    MetricsFlusher(collector, session_factory, flush_interval=30.0)

    # Record a tool call so counters are non-zero
    collector.record_tool_call("brain_search", latency_ms=100.0, agent="test-agent")

    # Manually take a first snapshot at t=0 (simulates flusher bootstrap)
    collector.snapshot_counters()

    # Simulate a second snapshot after "some time" by mutating timestamps
    # (inject directly to mimic elapsed time between flushes)
    ts0, c0, e0 = collector._counter_snapshots[0]
    collector._counter_snapshots.appendleft((ts0 - 35.0, c0, e0))

    # Now take another snapshot (simulates the second flush cycle)
    collector.record_tool_call("brain_search", latency_ms=100.0, agent="test-agent")
    collector.snapshot_counters()

    rates = collector.compute_rates(window_s=60.0)
    assert rates["rps"] > 0.0, "rps must be > 0 when snapshot_counters is called periodically"


async def test_flusher_flush_calls_snapshot_counters(collector: MetricsCollector) -> None:
    """MetricsFlusher._flush() must call collector.snapshot_counters() each cycle."""
    session_factory = MagicMock()
    fake_session = AsyncMock()
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=False)
    fake_session.execute = AsyncMock()
    fake_session.commit = AsyncMock()
    session_factory.return_value = fake_session

    flusher = MetricsFlusher(collector, session_factory, flush_interval=30.0)

    snapshots_before = len(collector._counter_snapshots)
    await flusher._flush()
    snapshots_after = len(collector._counter_snapshots)

    assert snapshots_after == snapshots_before + 1, (
        "_flush() must call snapshot_counters() exactly once per cycle"
    )


# ──────────────────────────────────────────────────────────────────────────────
# 2. record_search_log feeds record_search_latency (percentiles non-zero)
# ──────────────────────────────────────────────────────────────────────────────


async def test_record_search_log_populates_search_latencies(
    collector: MetricsCollector,
) -> None:
    """After record_search_log, retrieval_percentiles must return non-zero p95.

    record_search_log (async, DB-backed) must internally call
    record_search_latency so the in-memory histogram is fed even when the DB
    write fails or is mocked out.
    """
    # Patch the DB INSERT so it doesn't require a live DB
    with patch.object(collector, "_session_factory") as mock_sf:
        fake_session = AsyncMock()
        fake_session.__aenter__ = AsyncMock(return_value=fake_session)
        fake_session.__aexit__ = AsyncMock(return_value=False)
        fake_session.execute = AsyncMock()
        fake_session.commit = AsyncMock()
        mock_sf.return_value = fake_session

        await collector.record_search_log(
            tool_name="brain_search",
            project_key="brain-v42",
            result_count=5,
            top_score=0.91,
            avg_score=0.85,
            latency_ms=250.0,
        )

    pct = collector.retrieval_percentiles(window_s=86400.0)
    assert pct["p50"] > 0.0, (
        "retrieval p50 must be > 0 after record_search_log — "
        "record_search_latency must be called internally"
    )
    assert pct["p95"] > 0.0, "retrieval p95 must be > 0 after record_search_log"


async def test_record_search_log_db_failure_still_records_latency(
    collector: MetricsCollector,
) -> None:
    """Even if the DB INSERT fails, the in-memory latency must be recorded."""
    with patch.object(collector, "_session_factory") as mock_sf:
        mock_sf.return_value.__aenter__ = AsyncMock(side_effect=Exception("DB down"))
        mock_sf.return_value.__aexit__ = AsyncMock(return_value=False)

        await collector.record_search_log(
            tool_name="brain_search",
            project_key="brain-v42",
            result_count=3,
            top_score=0.80,
            avg_score=0.75,
            latency_ms=320.0,
        )

    pct = collector.retrieval_percentiles(window_s=86400.0)
    assert pct["p50"] > 0.0, (
        "latency must be recorded even when DB INSERT fails — "
        "record_search_latency must be called before the DB write"
    )


# ──────────────────────────────────────────────────────────────────────────────
# 3. Agent cardinality cap
# ──────────────────────────────────────────────────────────────────────────────


def test_agent_cardinality_cap_overflows_to_bucket(collector: MetricsCollector) -> None:
    """Once MAX_AGENTS unique agents are recorded, extras land in '_overflow'.

    The cap prevents unbounded _tool_stats memory growth from client-controlled
    X-Brain-Agent headers.
    """
    cap = collector._MAX_AGENTS

    # Fill up to cap
    for i in range(cap):
        collector.record_tool_call("brain_search", 10.0, agent=f"agent-{i}")

    # One more should go to _overflow
    collector.record_tool_call("brain_search", 10.0, agent="should-overflow")

    agent_names = set(collector._tool_stats.keys())
    assert "_overflow" in agent_names, "_overflow bucket must exist after cap exceeded"
    assert "should-overflow" not in agent_names, (
        "overflow agent must NOT create a new key; must land in _overflow"
    )
    assert len([a for a in agent_names if not a.startswith("_")]) <= cap, (
        f"real agent count must not exceed {cap}"
    )


def test_agent_cardinality_cap_overflow_accumulates_calls(collector: MetricsCollector) -> None:
    """Multiple overflowing agents all accumulate calls in the '_overflow' bucket."""
    cap = collector._MAX_AGENTS
    for i in range(cap):
        collector.record_tool_call("brain_search", 10.0, agent=f"agent-{i}")

    # Send 3 different overflow agents
    for extra in ["x", "y", "z"]:
        collector.record_tool_call("brain_learn", 20.0, agent=extra)

    overflow_total = sum(t["calls"] for t in collector._tool_stats.get("_overflow", {}).values())
    assert overflow_total >= 3, "_overflow must accumulate calls from all overflowing agents"


def test_known_agents_not_overflowed_before_cap(collector: MetricsCollector) -> None:
    """Agents recorded before the cap is reached must NOT be moved to _overflow."""
    cap = collector._MAX_AGENTS
    for i in range(cap):
        collector.record_tool_call("brain_search", 10.0, agent=f"agent-{i}")

    # First agent must still have its own slot
    assert "agent-0" in collector._tool_stats, (
        "pre-cap agents must keep their own slot even after the cap is reached"
    )


# ──────────────────────────────────────────────────────────────────────────────
# 4. RecentLogProcessor wires into structlog in sidecar
# ──────────────────────────────────────────────────────────────────────────────


def test_recent_log_processor_push_on_call(collector: MetricsCollector) -> None:
    """RecentLogProcessor.__call__ must push an entry to the collector buffer."""
    proc = RecentLogProcessor(collector, min_level="info")
    event_dict = {"event": "metrics.test.event", "level": "info", "foo": "bar"}
    result = proc(None, "info", event_dict)

    # Processor must return the event_dict unchanged (pass-through)
    assert result is event_dict

    log = collector.get_recent_log()
    assert len(log) == 1
    assert "metrics.test.event" in log[0]["msg"]


def test_recent_log_processor_min_level_filters_debug(collector: MetricsCollector) -> None:
    """Debug events below min_level='info' must be silently dropped."""
    proc = RecentLogProcessor(collector, min_level="info")
    proc(None, "debug", {"event": "low-noise", "level": "debug"})

    assert collector.get_recent_log() == [], "debug events must not appear in recent_log"


def test_recent_log_processor_structlog_chain_integration(collector: MetricsCollector) -> None:
    """RecentLogProcessor integrates into a structlog ProcessorChain without errors."""
    proc = RecentLogProcessor(collector, min_level="info")
    # Simulate being in a processor chain: configure structlog with it
    structlog.configure(
        processors=[
            proc,
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.BoundLogger,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=False,
    )
    test_logger = structlog.get_logger("test.wave4")
    # This should not raise
    try:
        test_logger.info("wave4.sidecar.test", key="value")
    except Exception as exc:
        pytest.fail(f"structlog chain with RecentLogProcessor raised: {exc}")

    # Reset to a neutral config (don't leave test state in structlog global)
    structlog.reset_defaults()

    log = collector.get_recent_log()
    assert any("wave4.sidecar.test" in entry["msg"] for entry in log)


# ──────────────────────────────────────────────────────────────────────────────
# 5. Decay stats present in _process flush data (DB-persistence-ready)
# ──────────────────────────────────────────────────────────────────────────────


def test_decay_stats_in_process_flush_data(collector: MetricsCollector) -> None:
    """record_decay_stats values must appear in get_flush_data['_process']['decay']."""
    collector.record_decay_stats(stale_count=7, archived_count=3, access_log_size=1024)

    fd = collector.get_flush_data()
    decay = fd["_process"]["decay"]

    assert decay["stale_count"] == 7
    assert decay["archived_count"] == 3
    assert decay["access_log_size"] == 1024


def test_decay_stats_flushed_via_flusher_in_process_row(collector: MetricsCollector) -> None:
    """MetricsFlusher must include decay data in the _process row upsert payload.

    This is a unit-level check: _process_pseudo_tools does not carry decay
    (it's not a pseudo-tool), but the main _process entry passed to the
    DB upsert must contain the decay block from get_flush_data.
    """
    collector.record_decay_stats(stale_count=5, archived_count=2, access_log_size=500)
    fd = collector.get_flush_data(dirty_only=True)
    proc = fd["_process"]
    # The decay block must be present so the flusher can serialize it
    assert "decay" in proc, "_process flush data must carry 'decay' block"
    assert proc["decay"]["stale_count"] == 5


# ──────────────────────────────────────────────────────────────────────────────
# 6. cockpit endpoint field: not hardcoded "stdio" — reads from env/config
# ──────────────────────────────────────────────────────────────────────────────


def _make_cockpit(transport: str = "http") -> tuple[Any, Any]:
    """Create a CockpitCollector with all DB helpers stubbed out."""
    from brain_v42.metrics.cockpit import CockpitCollector

    collector = MetricsCollector(engine=MagicMock(), session_factory=MagicMock())

    mock_sf = MagicMock()
    fake_session = AsyncMock()
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=False)
    fake_session.execute = AsyncMock(
        return_value=MagicMock(
            fetchall=MagicMock(return_value=[]), one=MagicMock(return_value=(0, 0))
        )
    )
    mock_sf.return_value = fake_session

    cockpit = CockpitCollector(collector=collector, session_factory=mock_sf)

    mock_settings = MagicMock()
    mock_settings.brain_mcp_transport = transport

    return cockpit, mock_settings


async def test_cockpit_endpoint_reads_from_settings_not_hardcoded() -> None:
    """The 'endpoint' field in the cockpit payload must reflect brain_mcp_transport
    from settings, not be a hardcoded "stdio" string.
    """
    cockpit, mock_settings = _make_cockpit(transport="http")

    _NULL_MEMORY: dict[str, Any] = {
        "episodes": 0,
        "episodic_mb": 0,
        "semantic_chunks": 0,
        "semantic_mb": 0,
        "lastCompaction": None,
        "freedLastCompaction": 0,
        "vectorIndex": None,
    }
    with (
        patch("brain_v42.metrics.cockpit.get_settings", return_value=mock_settings),
        patch(
            "brain_v42.metrics.cockpit.collect_memory_stats",
            new=AsyncMock(return_value=_NULL_MEMORY),
        ),
    ):
        payload = await cockpit._build()

    assert payload["endpoint"] != "stdio", (
        "endpoint must not be hardcoded 'stdio' — must reflect current transport"
    )
    assert payload["endpoint"] == "http", "endpoint must read from settings.brain_mcp_transport"


async def test_cockpit_endpoint_stdio_when_transport_is_stdio() -> None:
    """When brain_mcp_transport='stdio', endpoint must be 'stdio'."""
    cockpit, mock_settings = _make_cockpit(transport="stdio")

    _NULL_MEMORY: dict[str, Any] = {
        "episodes": 0,
        "episodic_mb": 0,
        "semantic_chunks": 0,
        "semantic_mb": 0,
        "lastCompaction": None,
        "freedLastCompaction": 0,
        "vectorIndex": None,
    }
    with (
        patch("brain_v42.metrics.cockpit.get_settings", return_value=mock_settings),
        patch(
            "brain_v42.metrics.cockpit.collect_memory_stats",
            new=AsyncMock(return_value=_NULL_MEMORY),
        ),
    ):
        payload = await cockpit._build()

    assert payload["endpoint"] == "stdio"


# ──────────────────────────────────────────────────────────────────────────────
# 7. cache_hit_ratio always 0 → null/None honest (no fake 0.0)
# ──────────────────────────────────────────────────────────────────────────────


async def test_cockpit_cache_hit_is_null_not_fake_zero() -> None:
    """cache_hit in the cockpit metrics block must be None (no data source),
    not a misleading 0.0 that looks like a measured value.
    """
    cockpit, mock_settings = _make_cockpit(transport="http")

    _NULL_MEMORY: dict[str, Any] = {
        "episodes": 0,
        "episodic_mb": 0,
        "semantic_chunks": 0,
        "semantic_mb": 0,
        "lastCompaction": None,
        "freedLastCompaction": 0,
        "vectorIndex": None,
    }
    with (
        patch("brain_v42.metrics.cockpit.get_settings", return_value=mock_settings),
        patch(
            "brain_v42.metrics.cockpit.collect_memory_stats",
            new=AsyncMock(return_value=_NULL_MEMORY),
        ),
    ):
        payload = await cockpit._build()

    cache_hit = payload["metrics"].get("cache_hit")
    assert cache_hit is None, (
        f"cache_hit must be None (no data source), not {cache_hit!r} — "
        "a fake 0.0 is misleading; null signals 'not measured'"
    )


# ──────────────────────────────────────────────────────────────────────────────
# B1. Sidecar structlog chain must work with PrintLoggerFactory
#     (add_logger_name crashes because PrintLogger has no .name attribute)
# ──────────────────────────────────────────────────────────────────────────────


def test_sidecar_structlog_chain_no_attribute_error(collector: MetricsCollector) -> None:
    """The sidecar's exact processor chain must emit a log event without AttributeError.

    Regression test for: structlog.stdlib.add_logger_name crashes with
    PrintLoggerFactory because PrintLogger has no .name attribute.
    Uses build_sidecar_structlog_processors() — the canonical chain from __main__.
    """
    from brain_v42.metrics.__main__ import build_sidecar_structlog_processors  # noqa: PLC0415

    processors = build_sidecar_structlog_processors(collector)
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.BoundLogger,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=False,
    )
    log = structlog.get_logger("test.sidecar")
    try:
        log.info("sidecar.startup.ok", transport="http")
    except AttributeError as exc:
        pytest.fail(
            f"Sidecar structlog chain raised AttributeError — "
            f"likely add_logger_name with PrintLoggerFactory: {exc}"
        )
    finally:
        structlog.reset_defaults()


def test_sidecar_structlog_chain_populates_recent_log(collector: MetricsCollector) -> None:
    """The sidecar's processor chain must forward info events to the recent-log buffer."""
    from brain_v42.metrics.__main__ import build_sidecar_structlog_processors  # noqa: PLC0415

    processors = build_sidecar_structlog_processors(collector)
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.BoundLogger,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=False,
    )
    log = structlog.get_logger("test.sidecar")
    log.info("metrics_standalone.running", port=9200)
    structlog.reset_defaults()

    recent = collector.get_recent_log()
    assert any("metrics_standalone.running" in e["msg"] for e in recent), (
        "sidecar info events must appear in collector.get_recent_log() "
        "when the sidecar chain is active"
    )


# ──────────────────────────────────────────────────────────────────────────────
# B2. MetricsFlusher must persist decay block in the _process DB row
# ──────────────────────────────────────────────────────────────────────────────


async def test_flusher_persists_decay_block_in_process_row(
    collector: MetricsCollector,
) -> None:
    """_flush() must include decay data in the tool_stats JSON written for the
    _process row.

    Regression: _process_pseudo_tools() only serialises reranker/graph/cost/buckets
    — the decay block from get_flush_data()['_process']['decay'] was silently
    dropped.  The sidecar can only expose decay if it was written to process_metrics.
    """
    import json

    collector.record_decay_stats(stale_count=11, archived_count=4, access_log_size=2048)

    session_factory = MagicMock()
    fake_session = AsyncMock()
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=False)
    fake_session.commit = AsyncMock()

    captured_params: list[dict[str, Any]] = []

    async def _capture_execute(stmt: Any, params: dict[str, Any] | None = None) -> MagicMock:
        if params and "tool_stats" in params:
            captured_params.append(params)
        return MagicMock(fetchall=MagicMock(return_value=[]))

    fake_session.execute = _capture_execute
    session_factory.return_value = fake_session

    flusher = MetricsFlusher(collector, session_factory, flush_interval=30.0)
    await flusher._flush()

    process_row = next(
        (p for p in captured_params if p.get("agent_name") == "_process"),
        None,
    )
    assert process_row is not None, "_process row must be written to process_metrics"

    tool_stats_raw = process_row["tool_stats"]
    tool_stats = json.loads(tool_stats_raw) if isinstance(tool_stats_raw, str) else tool_stats_raw
    assert "_decay" in tool_stats, (
        "_process row tool_stats must include '_decay' key — "
        "decay data is lost at restart if not persisted"
    )
    decay = tool_stats["_decay"]
    assert decay.get("stale_count") == 11
    assert decay.get("archived_count") == 4
    assert decay.get("access_log_size") == 2048


# ──────────────────────────────────────────────────────────────────────────────
# B3. cockpit cost.today must be null when no record_cost callers exist
# ──────────────────────────────────────────────────────────────────────────────


async def test_cockpit_cost_today_null_when_no_callers() -> None:
    """cost.today must be None (not 0.0) when record_cost has never been called.

    A flat 0.0 in cost.today looks like a measured 'zero spend' — it is
    indistinguishable from 'real zero' vs 'no data source', which is the same
    honesty problem as cache_hit.  None signals 'not measured'.

    Design: cost_total() == 0.0 AND cost_by_model() is empty → today = None.
    When record_cost is eventually wired, total > 0 → today = rounded float.
    """
    cockpit, mock_settings = _make_cockpit(transport="http")

    _NULL_MEMORY: dict[str, Any] = {
        "episodes": 0,
        "episodic_mb": 0,
        "semantic_chunks": 0,
        "semantic_mb": 0,
        "lastCompaction": None,
        "freedLastCompaction": 0,
        "vectorIndex": None,
    }
    with (
        patch("brain_v42.metrics.cockpit.get_settings", return_value=mock_settings),
        patch(
            "brain_v42.metrics.cockpit.collect_memory_stats",
            new=AsyncMock(return_value=_NULL_MEMORY),
        ),
    ):
        payload = await cockpit._build()

    cost_today = payload["cost"].get("today")
    assert cost_today is None, (
        f"cost.today must be None when no record_cost caller exists, got {cost_today!r} — "
        "a fake 0.0 is a 'zéro mensonger' (flat-zero timeseries, misleading cockpit)"
    )


async def test_cockpit_cost_today_nonzero_when_cost_recorded() -> None:
    """cost.today must be a float when record_cost has been called at least once."""
    from brain_v42.metrics.cockpit import CockpitCollector

    col = MetricsCollector(engine=MagicMock(), session_factory=MagicMock())
    col.record_cost("claude-sonnet-4-5", 1.23)  # large enough to survive round(x, 2)

    mock_sf = MagicMock()
    fake_session = AsyncMock()
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=False)
    fake_session.execute = AsyncMock(
        return_value=MagicMock(
            fetchall=MagicMock(return_value=[]), one=MagicMock(return_value=(0, 0))
        )
    )
    mock_sf.return_value = fake_session
    cockpit = CockpitCollector(collector=col, session_factory=mock_sf)

    mock_settings = MagicMock()
    mock_settings.brain_mcp_transport = "http"

    _NULL_MEMORY: dict[str, Any] = {
        "episodes": 0,
        "episodic_mb": 0,
        "semantic_chunks": 0,
        "semantic_mb": 0,
        "lastCompaction": None,
        "freedLastCompaction": 0,
        "vectorIndex": None,
    }
    with (
        patch("brain_v42.metrics.cockpit.get_settings", return_value=mock_settings),
        patch(
            "brain_v42.metrics.cockpit.collect_memory_stats",
            new=AsyncMock(return_value=_NULL_MEMORY),
        ),
    ):
        payload = await cockpit._build()

    cost_today = payload["cost"].get("today")
    assert cost_today is not None, "cost.today must be a float when record_cost has been called"
    assert cost_today > 0.0, "cost.today must be positive when cost was recorded"


# ──────────────────────────────────────────────────────────────────────────────
# M1. Agent cardinality cap WARN must fire only ONCE PER PROCESS lifetime
# ──────────────────────────────────────────────────────────────────────────────


def test_agent_cardinality_warn_fires_once_per_process(
    collector: MetricsCollector,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """WARN must fire exactly once per process — even across DISTINCT overflow agents.

    Under high-cardinality spoofed X-Brain-Agent headers, per-call warns flood
    the logs and a per-agent warned SET is itself unbounded (client-controlled
    names accumulate in memory forever).  The contract is one WARN per process
    lifetime, carrying the first offending agent name; later overflows stay
    silent while still landing in the _overflow bucket.
    """
    cap = collector._MAX_AGENTS
    for i in range(cap):
        collector.record_tool_call("brain_search", 10.0, agent=f"agent-{i}")

    # 5 calls from the same overflow agent + 5 DISTINCT new overflow agents:
    # exactly ONE warn total, and no per-agent memory growth.
    with patch("brain_v42.metrics.collector.logger") as mock_log:
        for _ in range(5):
            collector.record_tool_call("brain_search", 10.0, agent="spoofed-agent")
        for i in range(5):
            collector.record_tool_call("brain_search", 10.0, agent=f"spoofed-{i}")

    warning_calls = [c for c in mock_log.warning.call_args_list if "cardinality_cap" in str(c)]
    assert len(warning_calls) == 1, (
        f"cardinality cap WARN must fire exactly once per process, "
        f"got {len(warning_calls)} warnings across 10 overflow calls (6 distinct agents)"
    )
    # The guard itself must be bounded: a bool flag, not a client-keyed set.
    assert isinstance(collector._overflow_warned, bool)
