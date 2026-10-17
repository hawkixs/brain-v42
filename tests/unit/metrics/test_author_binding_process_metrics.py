"""Publish author-binding candidates from persisted process snapshots only."""

import json
from collections.abc import Iterator
from importlib import import_module, util
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.exceptions import AuthorizationError

from brain_v42.credentials.reasons import reset_refusal_counts
from brain_v42.metrics.collector import MetricsCollector
from brain_v42.metrics.flusher import MetricsFlusher
from brain_v42.metrics.server import MetricsServer
from tests.unit.metrics.test_embedding_usage_process_metrics import _collect
from tests.unit.metrics.test_pseudo_tools_filter import _MOCK_SETTINGS, _make_collector

KEY = "brain_ticket_reply:unauthenticated"


@pytest.mark.parametrize("cause", ["scope_mismatch", "scope_unresolved", "scope_unauthenticated"])
async def test_scope_candidates_use_the_existing_persisted_counter(
    monkeypatch: pytest.MonkeyPatch, cause: str
) -> None:
    from brain_v42.mcp import tool_families
    from tests.unit.mcp.test_tool_families import _scope_context, _scoped_access

    access = (
        None
        if cause == "scope_unauthenticated"
        else _scoped_access("write", projects=["brain-v42"])
    )
    monkeypatch.setattr(tool_families, "get_access_token", lambda: access)
    arguments = {"project_key": "outside"} if cause == "scope_mismatch" else {}
    middleware = tool_families.FamilyAuthorizationMiddleware(scope_mode="observe")
    context = _scope_context("brain_learn", arguments)
    if access is None:
        with pytest.raises(AuthorizationError, match="family_denied"):
            await middleware.on_call_tool(context, AsyncMock())
    else:
        await middleware.on_call_tool(context, AsyncMock())
    collector = MetricsCollector(engine=MagicMock(), session_factory=MagicMock())
    session = AsyncMock()
    session.__aenter__.return_value = session
    await MetricsFlusher(collector, MagicMock(return_value=session))._flush()
    rows = [
        call.args[1]
        for call in session.execute.await_args_list
        if len(call.args) == 2 and isinstance(call.args[1], dict)
    ]
    process = next(row for row in rows if row["agent_name"] == "_process")
    assert json.loads(process["tool_stats"])["_author_binding_candidates"] == {
        f"brain_learn:{cause}": 1
    }


@pytest.fixture(autouse=True)
def counters(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    reset_refusal_counts()
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
    reset_refusal_counts()


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


async def test_aggregation_keeps_empty_map_before_flush_and_nulls_on_database_failure() -> None:
    # A measured empty source stays empty; an unmeasured one is null, never a
    # fabricated empty map (d1f43a66).
    assert (await _collect([]))["author_binding_candidates"] == {}
    failed = MetricsCollector(
        engine=MagicMock(), session_factory=MagicMock(side_effect=OSError("unavailable"))
    )
    assert (await failed.collect_process_metrics())["author_binding_candidates"] is None


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
