"""Keep both refusal maps present through persistence and sidecar scrapes."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from brain_v42.metrics.collector import MetricsCollector
from brain_v42.metrics.flusher import MetricsFlusher
from brain_v42.metrics.server import MetricsServer
from tests.unit.metrics.test_embedding_usage_process_metrics import _collect
from tests.unit.metrics.test_pseudo_tools_filter import _MOCK_SETTINGS, _make_collector


async def test_process_counters_start_empty_and_are_persisted_cumulatively() -> None:
    from brain_v42.credentials.agent_unresolved import reset_agent_unresolved_counts
    from brain_v42.credentials.elevation_refusals import (
        emit_elevation_refusal,
        reset_elevation_refusal_counts,
    )
    from brain_v42.credentials.reasons import reset_refusal_counts

    reset_refusal_counts()
    reset_elevation_refusal_counts()
    reset_agent_unresolved_counts()
    try:
        assert MetricsFlusher._process_pseudo_tools({})["_mcp_auth_refused"] == {}
        assert MetricsFlusher._process_pseudo_tools({})["_elevation_refused"] == {}
        emit_elevation_refusal(
            "session_not_open",
            status=409,
            session_id="session",
            requesting_client_id="hook",
            peer=None,
        )
        for _ in range(2):
            assert MetricsFlusher._process_pseudo_tools({})["_elevation_refused"] == {
                "session_not_open": 1,
            }
        collector = MetricsCollector(engine=MagicMock(), session_factory=MagicMock())
        collector.record_tool_call("brain_search", 1, agent="reader")
        session = AsyncMock()
        session.__aenter__.return_value = session
        await MetricsFlusher(collector, MagicMock(return_value=session))._flush()
        rows = [
            call.args[1]
            for call in session.execute.await_args_list
            if len(call.args) == 2 and isinstance(call.args[1], dict)
        ]
        process = [row for row in rows if row["agent_name"] == "_process"]
        assert len(process) == 1
        assert json.loads(process[0]["tool_stats"])["_elevation_refused"] == {
            "session_not_open": 1,
        }
        assert all(
            "_elevation_refused" not in json.loads(row["tool_stats"])
            for row in rows
            if row["agent_name"] != "_process"
        )
    finally:
        reset_elevation_refusal_counts()
        reset_agent_unresolved_counts()
        reset_refusal_counts()


async def test_aggregation_ignores_agent_copies_and_tool_reducers() -> None:
    result = await _collect(
        [
            ("_process", 1, None, None, {"_elevation_refused": {"invalid_window": 2}}, {}, 0, True),
            ("reader", 1, None, None, {"_elevation_refused": {"invalid_window": 99}}, {}, 0, True),
        ]
    )
    assert result["elevation_refused"] == {"invalid_window": 2}
    assert "_elevation_refused" not in result["tools"]


async def test_aggregation_keeps_both_maps_before_flush_and_on_database_failure() -> None:
    failed = MetricsCollector(
        engine=MagicMock(), session_factory=MagicMock(side_effect=OSError("unavailable"))
    )
    for result in (await _collect([]), await failed.collect_process_metrics()):
        assert result["mcp_auth_refused"] == {}
        assert result["elevation_refused"] == {}


@pytest.mark.parametrize("counts", [{}, {"session_not_operator": 1}])
async def test_metrics_route_reads_persisted_elevation_counts(
    counts: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("brain_v42.metrics.collector.get_settings", lambda: _MOCK_SETTINGS)
    collector = _make_collector()
    collector.collect_process_metrics = AsyncMock(
        return_value={
            "active_processes": 0,
            "mcp_auth_refused": {},
            "elevation_refused": counts,
        }
    )
    embedding = MagicMock()
    embedding.healthcheck = AsyncMock(return_value=True)
    server = MetricsServer(collector, embedding, port=0, host="127.0.0.1")
    response = await server._handle_metrics(MagicMock())
    assert json.loads(response.body)["mcp_auth_refused"] == {}
    assert json.loads(response.body)["elevation_refused"] == counts
