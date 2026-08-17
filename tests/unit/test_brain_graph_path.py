"""Unit tests for brain_graph_path MCP tool.

TDD: Written BEFORE implementation — all tests must fail RED first.

Uses the same pattern as tests/unit/mcp/tools/test_brain_search_tools.py:
real FastMCP instance + register_tools() with MagicMock services.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastmcp import FastMCP

from brain_v42.mcp.tools.brain_tools import register_tools
from tests.unit.mcp._tool_error_adapter import capture_tool_errors


def _make_mcp_with_graph_svc(
    mock_graph: MagicMock | None = None,
) -> tuple[FastMCP, MagicMock]:
    """Create a test FastMCP instance with a mocked graph_svc."""
    mcp = FastMCP("test-brain-graph-path")
    if mock_graph is None:
        mock_graph = MagicMock()
        mock_graph.get_path = AsyncMock(return_value=[])
    register_tools(
        mcp,
        decision_svc=MagicMock(),
        learning_svc=MagicMock(),
        snippet_svc=MagicMock(),
        runbook_svc=MagicMock(),
        adr_svc=MagicMock(),
        project_context_svc=MagicMock(),
        brain_svc=MagicMock(),
        graph_svc=mock_graph,
    )
    return mcp, mock_graph


async def _get_tool_fn(mcp: FastMCP, name: str):
    """Get the underlying function of a registered MCP tool."""
    tool = await mcp.get_tool(name)
    return capture_tool_errors(tool.fn)


class TestBrainGraphPath:
    @pytest.mark.asyncio
    async def test_returns_error_on_invalid_source_uuid(self) -> None:
        """source_id='not-uuid' → returns format_error string."""
        mcp, _ = _make_mcp_with_graph_svc()
        fn = await _get_tool_fn(mcp, "brain_graph_path")
        result = await fn(
            source_id="not-uuid",
            target_id=str(uuid4()),
        )
        assert result == "Invalid entity UUID"

    @pytest.mark.asyncio
    async def test_returns_error_on_invalid_target_uuid(self) -> None:
        """target_id='not-uuid' → returns format_error string."""
        mcp, _ = _make_mcp_with_graph_svc()
        fn = await _get_tool_fn(mcp, "brain_graph_path")
        result = await fn(
            source_id=str(uuid4()),
            target_id="not-uuid",
        )
        assert result == "Invalid entity UUID"

    @pytest.mark.asyncio
    async def test_returns_no_path_message_when_graph_returns_empty(self) -> None:
        """get_path returns [] → result contains 'No path found'."""
        mock_graph = MagicMock()
        mock_graph.get_path = AsyncMock(return_value=[])
        mcp, _ = _make_mcp_with_graph_svc(mock_graph)
        fn = await _get_tool_fn(mcp, "brain_graph_path")
        result = await fn(
            source_id=str(uuid4()),
            target_id=str(uuid4()),
        )
        assert "No path found" in result

    @pytest.mark.asyncio
    async def test_returns_formatted_path_when_graph_returns_nodes(self) -> None:
        """get_path returns 2-node list → result equals formatted path."""
        mock_graph = MagicMock()
        mock_graph.get_path = AsyncMock(
            return_value=[
                {"type": "Decision", "label": "A", "rel_to_next": "USES"},
                {"type": "Learning", "label": "B"},
            ]
        )
        mcp, _ = _make_mcp_with_graph_svc(mock_graph)
        fn = await _get_tool_fn(mcp, "brain_graph_path")
        result = await fn(
            source_id=str(uuid4()),
            target_id=str(uuid4()),
        )
        assert result == "[Decision] A --USES--> [Learning] B"

    @pytest.mark.asyncio
    async def test_clamps_depth_above_6(self) -> None:
        """max_depth=99 → get_path called with max_depth=6."""
        mock_graph = MagicMock()
        mock_graph.get_path = AsyncMock(return_value=[])
        mcp, _ = _make_mcp_with_graph_svc(mock_graph)
        fn = await _get_tool_fn(mcp, "brain_graph_path")
        await fn(
            source_id=str(uuid4()),
            target_id=str(uuid4()),
            max_depth=99,
        )
        call_kwargs = mock_graph.get_path.call_args.kwargs
        assert call_kwargs["max_depth"] == 6

    @pytest.mark.asyncio
    async def test_clamps_depth_below_1(self) -> None:
        """max_depth=0 → get_path called with max_depth=1."""
        mock_graph = MagicMock()
        mock_graph.get_path = AsyncMock(return_value=[])
        mcp, _ = _make_mcp_with_graph_svc(mock_graph)
        fn = await _get_tool_fn(mcp, "brain_graph_path")
        await fn(
            source_id=str(uuid4()),
            target_id=str(uuid4()),
            max_depth=0,
        )
        call_kwargs = mock_graph.get_path.call_args.kwargs
        assert call_kwargs["max_depth"] == 1

    @pytest.mark.asyncio
    async def test_passes_rel_types_through(self) -> None:
        """rel_types=['RELATED_TO'] → get_path received rel_types=['RELATED_TO']."""
        mock_graph = MagicMock()
        mock_graph.get_path = AsyncMock(return_value=[])
        mcp, _ = _make_mcp_with_graph_svc(mock_graph)
        fn = await _get_tool_fn(mcp, "brain_graph_path")
        await fn(
            source_id=str(uuid4()),
            target_id=str(uuid4()),
            rel_types=["RELATED_TO"],
        )
        call_kwargs = mock_graph.get_path.call_args.kwargs
        assert call_kwargs["rel_types"] == ["RELATED_TO"]
