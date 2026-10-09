"""Expose accepted project fallbacks through the persisted MCP process snapshot."""

import json
from collections.abc import Iterator
from unittest.mock import AsyncMock, MagicMock

import pytest

from brain_v42.credentials.agent_unresolved import (
    count_agent_unresolved,
    reset_agent_unresolved_counts,
)
from brain_v42.metrics.collector import MetricsCollector
from brain_v42.metrics.flusher import MetricsFlusher
from brain_v42.metrics.server import MetricsServer
from tests.unit.metrics.test_embedding_usage_process_metrics import _collect
from tests.unit.metrics.test_pseudo_tools_filter import _MOCK_SETTINGS, _make_collector


@pytest.fixture(autouse=True)
def counters() -> Iterator[None]:
    reset_agent_unresolved_counts()
    yield
    reset_agent_unresolved_counts()


async def test_flusher_persists_empty_then_cumulative_counts_only_on_process_row() -> None:
    collector = MetricsCollector(engine=MagicMock(), session_factory=MagicMock())
    collector.record_tool_call("brain_search", 1, agent="reader")
    session = AsyncMock()
    session.__aenter__.return_value = session
    flusher = MetricsFlusher(collector, MagicMock(return_value=session))

    async def flush_counts() -> dict[str, int]:
        session.execute.reset_mock()
        await flusher._flush()
        rows = [
            call.args[1]
            for call in session.execute.await_args_list
            if len(call.args) == 2 and isinstance(call.args[1], dict)
        ]
        assert sum(row["agent_name"] == "_process" for row in rows) == 1
        for row in rows:
            if row["agent_name"] != "_process":
                assert "_mcp_auth_agent_unresolved" not in json.loads(row["tool_stats"])
        tools = json.loads(
            next(row["tool_stats"] for row in rows if row["agent_name"] == "_process")
        )
        return tools["_mcp_auth_agent_unresolved"]

    assert await flush_counts() == {}
    count_agent_unresolved("not_kebab")
    assert await flush_counts() == {"not_kebab": 1}
    assert await flush_counts() == {"not_kebab": 1}
    count_agent_unresolved("not_kebab")
    count_agent_unresolved("unknown_project")
    assert await flush_counts() == {"not_kebab": 2, "unknown_project": 1}


async def test_aggregation_sums_processes_and_ignores_agent_copies() -> None:
    result = await _collect(
        [
            (
                "_process",
                1,
                None,
                None,
                {"_mcp_auth_agent_unresolved": {"not_kebab": 2, "unknown_project": 1}},
                {},
                0,
                True,
            ),
            (
                "_process",
                2,
                None,
                None,
                {"_mcp_auth_agent_unresolved": {"not_kebab": 3, "unknown_project": 4}},
                {},
                0,
                True,
            ),
            (
                "reader",
                1,
                None,
                None,
                {"_mcp_auth_agent_unresolved": {"not_kebab": 99}},
                {},
                0,
                True,
            ),
        ]
    )
    assert result["mcp_auth_agent_unresolved"] == {"not_kebab": 5, "unknown_project": 5}
    assert "_mcp_auth_agent_unresolved" not in result["tools"]


async def test_aggregation_keeps_empty_map_before_flush_and_on_database_failure() -> None:
    failed = MetricsCollector(
        engine=MagicMock(), session_factory=MagicMock(side_effect=OSError("unavailable"))
    )
    for result in (await _collect([]), await failed.collect_process_metrics()):
        assert result["mcp_auth_agent_unresolved"] == {}


@pytest.mark.parametrize("counts", [None, {}, {"not_kebab": 2, "unknown_project": 1}])
async def test_metrics_route_reads_persisted_counts_with_structural_empty_default(
    counts: dict[str, int] | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("brain_v42.metrics.collector.get_settings", lambda: _MOCK_SETTINGS)
    collector = _make_collector()
    aggregate: dict[str, object] = {"active_processes": 0}
    if counts is not None:
        aggregate["mcp_auth_agent_unresolved"] = counts
    collector.collect_process_metrics = AsyncMock(return_value=aggregate)
    # The sidecar's own counter must not replace MCP's persisted observations.
    count_agent_unresolved("unknown_project")
    embedding = MagicMock()
    embedding.healthcheck = AsyncMock(return_value=True)
    server = MetricsServer(collector, embedding, port=0, host="127.0.0.1")
    response = await server._handle_metrics(MagicMock())
    assert json.loads(response.body)["mcp_auth_agent_unresolved"] == (counts or {})
    assert "mcp_auth_agent_unresolved" not in aggregate
