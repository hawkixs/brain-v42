"""MCP process counters must reach the sidecar through the persisted process row."""

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from brain_v42.credentials.reasons import emit_refusal, reset_refusal_counts
from brain_v42.metrics.collector import MetricsCollector
from brain_v42.metrics.flusher import MetricsFlusher
from tests.unit.metrics.test_embedding_usage_process_metrics import _collect


@pytest.fixture(autouse=True)
def _counts() -> Iterator[None]:
    reset_refusal_counts()
    yield
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
            tools = json.loads(row["tool_stats"])
            if row["agent_name"] != "_process":
                assert "_mcp_auth_refused" not in tools
        tools = json.loads(
            next(row["tool_stats"] for row in rows if row["agent_name"] == "_process")
        )
        return tools["_mcp_auth_refused"]

    assert await flush_counts() == {}
    emit_refusal("family_denied", status=403, client_id="reader", tool="brain_learn")
    assert await flush_counts() == {"family_denied": 1}
    assert await flush_counts() == {"family_denied": 1}
    emit_refusal("family_denied", status=403)
    emit_refusal("missing_token", status=401)
    persisted = await flush_counts()
    assert persisted == {"family_denied": 2, "missing_token": 1}

    # A different process has no in-memory counter; it reads only the JSONB row.
    reset_refusal_counts()
    result = await _collect(
        [
            (
                "_process",
                1,
                datetime.now(UTC),
                datetime.now(UTC),
                {"_mcp_auth_refused": persisted},
                {},
                0,
                True,
            ),
            ("reader", 1, None, None, {"_mcp_auth_refused": {"family_denied": 99}}, {}, 0, True),
        ]
    )
    assert result["mcp_auth_refused"] == persisted
    assert "_mcp_auth_refused" not in result["tools"]


async def test_sidecar_aggregation_has_structural_zero_without_any_process_row() -> None:
    assert (await _collect([]))["mcp_auth_refused"] == {}


async def test_sidecar_aggregation_keeps_presence_when_database_is_unavailable() -> None:
    collector = MetricsCollector(
        engine=MagicMock(), session_factory=MagicMock(side_effect=OSError("unavailable"))
    )
    assert (await collector.collect_process_metrics())["mcp_auth_refused"] == {}
