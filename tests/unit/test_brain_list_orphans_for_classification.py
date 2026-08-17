"""Unit tests for brain_list_orphans_for_classification MCP tool.

TDD: Written BEFORE implementation — all tests must fail RED first.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from tests.unit.mcp._tool_error_adapter import capture_tool_errors


class MockMCP:
    """Collecting mock for FastMCP."""

    def __init__(self) -> None:
        self.registered: dict[str, Any] = {}

    def tool(self, **kwargs: Any) -> Any:
        def decorator(fn: Any) -> Any:
            self.registered[fn.__name__] = capture_tool_errors(fn)
            return fn

        return decorator


@pytest.fixture
def session() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def session_factory(session: AsyncMock) -> MagicMock:
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=ctx)


@pytest.fixture
def mock_graph() -> AsyncMock:
    g = AsyncMock()
    g.find_orphans_for_classification = AsyncMock(return_value=[])
    return g


@pytest.fixture
def tools(
    session_factory: MagicMock,
    mock_graph: AsyncMock,
) -> dict[str, Any]:
    from brain_v42.mcp.tools.dream_tools import register_dream_tools

    mcp = MockMCP()
    register_dream_tools(
        mcp,
        session_factory=session_factory,
        auto_linker=None,
        graph_service=mock_graph,
    )
    return mcp.registered


class TestBrainListOrphansForClassification:
    @pytest.mark.asyncio
    async def test_tool_is_registered(self, tools: dict[str, Any]) -> None:
        """brain_list_orphans_for_classification is registered as an MCP tool."""
        assert "brain_list_orphans_for_classification" in tools

    @pytest.mark.asyncio
    async def test_returns_empty_json_when_graph_unavailable(
        self,
        session_factory: MagicMock,
    ) -> None:
        """Returns format_error string (NOT '[]') when graph_service is None."""
        from brain_v42.mcp.tools.dream_tools import register_dream_tools

        mcp = MockMCP()
        register_dream_tools(
            mcp,
            session_factory=session_factory,
            auto_linker=None,
            graph_service=None,
        )
        result = await mcp.registered["brain_list_orphans_for_classification"]()
        assert isinstance(result, str)
        assert result != "[]"
        assert "not configured" in result

    @pytest.mark.asyncio
    async def test_returns_empty_array_when_no_orphans(
        self,
        tools: dict[str, Any],
        mock_graph: AsyncMock,
    ) -> None:
        """Returns '[]' JSON string when graph_service returns empty list."""
        mock_graph.find_orphans_for_classification.return_value = []

        result = await tools["brain_list_orphans_for_classification"]()

        assert result == "[]"

    @pytest.mark.asyncio
    async def test_returns_json_with_metadata_for_orphans(
        self,
        session_factory: MagicMock,
        mock_graph: AsyncMock,
        session: AsyncMock,
    ) -> None:
        """Mock 2 orphans (Decision + Learning) → JSON parses to expected list."""
        decision_id = uuid4()
        learning_id = uuid4()

        mock_graph.find_orphans_for_classification.return_value = [
            {"id": str(decision_id), "labels": ["Decision"]},
            {"id": str(learning_id), "labels": ["Learning"]},
        ]

        # Mock PG rows returned per table query
        decision_row = MagicMock()
        decision_row.id = decision_id
        decision_row.topic = "Architecture choice"
        decision_row.tags = ["backend", "perf"]
        decision_row.project_key = "brain-v42"

        learning_row = MagicMock()
        learning_row.id = learning_id
        learning_row.topic = "Gotcha with async context"
        learning_row.tags = ["async"]
        learning_row.project_key = "brain-v42"

        # First call returns decision row; second returns learning row
        mock_result_decision = MagicMock()
        mock_result_decision.fetchall = MagicMock(return_value=[decision_row])
        mock_result_learning = MagicMock()
        mock_result_learning.fetchall = MagicMock(return_value=[learning_row])
        session.execute = AsyncMock(side_effect=[mock_result_decision, mock_result_learning])

        from brain_v42.mcp.tools.dream_tools import register_dream_tools

        mcp = MockMCP()
        register_dream_tools(
            mcp,
            session_factory=session_factory,
            auto_linker=None,
            graph_service=mock_graph,
        )

        result = await mcp.registered["brain_list_orphans_for_classification"]()

        assert isinstance(result, str)
        parsed = json.loads(result)
        assert isinstance(parsed, list)
        assert len(parsed) == 2

        ids_returned = {item["id"] for item in parsed}
        assert str(decision_id) in ids_returned
        assert str(learning_id) in ids_returned

        decision_item = next(i for i in parsed if i["id"] == str(decision_id))
        assert decision_item["type"] == "Decision"
        assert decision_item["topic"] == "Architecture choice"
        assert decision_item["tags"] == ["backend", "perf"]
        assert decision_item["project_key"] == "brain-v42"

        learning_item = next(i for i in parsed if i["id"] == str(learning_id))
        assert learning_item["type"] == "Learning"
        assert learning_item["topic"] == "Gotcha with async context"

    @pytest.mark.asyncio
    async def test_clamps_limit_to_max_50(
        self,
        tools: dict[str, Any],
        mock_graph: AsyncMock,
    ) -> None:
        """Passing limit=100 calls graph_service with limit=50 (max cap)."""
        mock_graph.find_orphans_for_classification.return_value = []

        await tools["brain_list_orphans_for_classification"](limit=100)

        mock_graph.find_orphans_for_classification.assert_called_once_with(limit=50)

    @pytest.mark.asyncio
    async def test_clamps_limit_to_min_1(
        self,
        tools: dict[str, Any],
        mock_graph: AsyncMock,
    ) -> None:
        """Passing limit=0 calls graph_service with limit=1 (min cap)."""
        mock_graph.find_orphans_for_classification.return_value = []

        await tools["brain_list_orphans_for_classification"](limit=0)

        mock_graph.find_orphans_for_classification.assert_called_once_with(limit=1)

    @pytest.mark.asyncio
    async def test_skips_orphans_with_no_known_label(
        self,
        session_factory: MagicMock,
        mock_graph: AsyncMock,
        session: AsyncMock,
    ) -> None:
        """Orphan with labels=['SomeFutureLabel'] is silently skipped, output is '[]'."""
        unknown_id = uuid4()

        mock_graph.find_orphans_for_classification.return_value = [
            {"id": str(unknown_id), "labels": ["SomeFutureLabel"]},
        ]

        session.execute = AsyncMock()

        from brain_v42.mcp.tools.dream_tools import register_dream_tools

        mcp = MockMCP()
        register_dream_tools(
            mcp,
            session_factory=session_factory,
            auto_linker=None,
            graph_service=mock_graph,
        )

        result = await mcp.registered["brain_list_orphans_for_classification"]()

        assert result == "[]"
        # No PG query should be issued for unrecognised labels
        session.execute.assert_not_called()
