"""Publish author-binding candidates from persisted process snapshots only."""

import json
from collections.abc import Iterator
from importlib import import_module, util
from unittest.mock import AsyncMock, MagicMock

import pytest

from brain_v42.metrics.collector import MetricsCollector
from brain_v42.metrics.flusher import MetricsFlusher
from brain_v42.metrics.server import MetricsServer
from tests.unit.metrics.test_embedding_usage_process_metrics import _collect
from tests.unit.metrics.test_pseudo_tools_filter import _MOCK_SETTINGS, _make_collector

KEY = "brain_ticket_reply:unauthenticated"


@pytest.fixture(autouse=True)
def counters(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    if util.find_spec("brain_v42.credentials.author_binding") is None:
        yield
        return
    module = import_module("brain_v42.credentials.author_binding")
    module.reset_author_binding_counts()
    observer = import_module("brain_v42.mcp.author_binding")
    monkeypatch.setattr(observer, "get_current_principal", lambda: None)
    monkeypatch.setattr(observer, "get_current_actor_project", lambda: None)
    yield
    module.reset_author_binding_counts()


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
                assert "_author_binding_candidates" not in json.loads(row["tool_stats"])
        tools = json.loads(
            next(row["tool_stats"] for row in rows if row["agent_name"] == "_process")
        )
        return tools["_author_binding_candidates"]

    assert await flush_counts() == {}
    module = import_module("brain_v42.mcp.author_binding")
    try:
        module.observe_declared_project("brain_ticket_reply", "project")
        assert await flush_counts() == {KEY: 1}
        assert await flush_counts() == {KEY: 1}
        module.observe_declared_project("brain_ticket_reply", "project")
        assert await flush_counts() == {KEY: 2}
    finally:
        module.reset_author_binding_counts()


async def test_aggregation_sums_processes_and_ignores_agent_copies() -> None:
    other = "brain_delivery_claim:mismatch"
    result = await _collect(
        [
            (
                "_process",
                1,
                None,
                None,
                {"_author_binding_candidates": {KEY: 2, other: 1}},
                {},
                0,
                True,
            ),
            (
                "_process",
                2,
                None,
                None,
                {"_author_binding_candidates": {KEY: 3, other: 4}},
                {},
                0,
                True,
            ),
            ("reader", 1, None, None, {"_author_binding_candidates": {KEY: 99}}, {}, 0, True),
        ]
    )
    assert result["author_binding_candidates"] == {KEY: 5, other: 5}
    assert "_author_binding_candidates" not in result["tools"]


async def test_aggregation_keeps_empty_map_before_flush_and_on_database_failure() -> None:
    failed = MetricsCollector(
        engine=MagicMock(), session_factory=MagicMock(side_effect=OSError("unavailable"))
    )
    for result in (await _collect([]), await failed.collect_process_metrics()):
        assert result["author_binding_candidates"] == {}


@pytest.mark.parametrize("counts", [None, {}, {KEY: 2}])
async def test_metrics_route_reads_persisted_counts_with_structural_empty_default(
    counts: dict[str, int] | None, monkeypatch: pytest.MonkeyPatch, counters: None
) -> None:
    monkeypatch.setattr("brain_v42.metrics.collector.get_settings", lambda: _MOCK_SETTINGS)
    collector = _make_collector()
    aggregate: dict[str, object] = {"active_processes": 0}
    if counts is not None:
        aggregate["author_binding_candidates"] = counts
    collector.collect_process_metrics = AsyncMock(return_value=aggregate)
    # A sidecar observation must never replace the persisted MCP counts.
    import_module("brain_v42.mcp.author_binding").observe_declared_project(
        "brain_ticket_reply", "project"
    )
    embedding = MagicMock()
    embedding.healthcheck = AsyncMock(return_value=True)
    server = MetricsServer(collector, embedding, port=0, host="127.0.0.1")
    response = await server._handle_metrics(MagicMock())
    assert json.loads(response.body)["author_binding_candidates"] == (counts or {})
    assert "author_binding_candidates" not in aggregate
