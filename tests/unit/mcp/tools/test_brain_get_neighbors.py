"""Unit tests for brain_get_neighbors MCP tool.

Exposes GraphService.get_neighbors() to the LLM as a 1-2 hop traversal
around any entity. Returns a markdown ``### Related`` section.

Tool is only registered when graph_svc is provided to register_tools()
(graceful degradation when Neo4j is disabled).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastmcp import FastMCP

from brain_v42.mcp.tools.brain_tools import register_tools
from tests.unit.mcp._tool_error_adapter import capture_tool_errors


def _make_mcp_with_graph(graph_svc: MagicMock | None) -> FastMCP:
    """Create test FastMCP instance, optionally with graph_svc wired."""
    mcp = FastMCP("test-brain")
    register_tools(
        mcp,
        decision_svc=MagicMock(),
        learning_svc=MagicMock(),
        snippet_svc=MagicMock(),
        runbook_svc=MagicMock(),
        adr_svc=MagicMock(),
        project_context_svc=MagicMock(),
        brain_svc=MagicMock(),
        graph_svc=graph_svc,
    )
    return mcp


async def _get_tool_fn(mcp: FastMCP, name: str):
    tool = await mcp.get_tool(name)
    return capture_tool_errors(tool.fn)


# ── Registration ──────────────────────────────────────────────────────────────


class TestRegistration:
    @pytest.mark.asyncio
    async def test_registered_when_graph_svc_provided(self) -> None:
        graph = MagicMock()
        mcp = _make_mcp_with_graph(graph)
        tool = await mcp.get_tool("brain_get_neighbors")
        assert tool is not None

    @pytest.mark.asyncio
    async def test_not_registered_when_graph_svc_missing(self) -> None:
        mcp = _make_mcp_with_graph(None)
        tool = await mcp.get_tool("brain_get_neighbors")
        assert tool is None


# ── Behavior ──────────────────────────────────────────────────────────────────


class TestBrainGetNeighbors:
    @pytest.mark.asyncio
    async def test_delegates_to_graph_svc_with_parsed_uuid(self) -> None:
        graph = MagicMock()
        graph.get_neighbors = AsyncMock(return_value=[])
        mcp = _make_mcp_with_graph(graph)
        fn = await _get_tool_fn(mcp, "brain_get_neighbors")

        eid = str(uuid4())
        await fn(entity_id=eid)

        graph.get_neighbors.assert_awaited_once()
        call_args = graph.get_neighbors.await_args
        # First positional or kwarg "id" must be a UUID parsed from the string.
        passed_id = call_args.kwargs.get("id") or call_args.args[0]
        assert str(passed_id) == eid

    @pytest.mark.asyncio
    async def test_returns_formatted_markdown_with_neighbors(self) -> None:
        graph = MagicMock()
        graph.get_neighbors = AsyncMock(
            return_value=[
                {
                    "id": "11111111-1111-1111-1111-111111111111",
                    "type": "Decision",
                    "rel": "MOTIVATED_BY",
                    "label": "Use PostgreSQL",
                },
                {
                    "id": "22222222-2222-2222-2222-222222222222",
                    "type": "ADR",
                    "rel": "IMPLEMENTS",
                    "label": "ADR-007 Pgvector adoption",
                },
            ]
        )
        mcp = _make_mcp_with_graph(graph)
        fn = await _get_tool_fn(mcp, "brain_get_neighbors")

        result = await fn(entity_id=str(uuid4()))

        assert "### Related" in result
        assert "Use PostgreSQL" in result
        assert "ADR-007 Pgvector adoption" in result
        assert "MOTIVATED_BY" in result
        assert "IMPLEMENTS" in result

    @pytest.mark.asyncio
    async def test_returns_empty_message_when_no_neighbors(self) -> None:
        graph = MagicMock()
        graph.get_neighbors = AsyncMock(return_value=[])
        mcp = _make_mcp_with_graph(graph)
        fn = await _get_tool_fn(mcp, "brain_get_neighbors")

        result = await fn(entity_id=str(uuid4()))

        assert "no neighbors" in result.lower() or "no related" in result.lower()

    @pytest.mark.asyncio
    async def test_passes_rel_types_filter_to_graph_svc(self) -> None:
        graph = MagicMock()
        graph.get_neighbors = AsyncMock(return_value=[])
        mcp = _make_mcp_with_graph(graph)
        fn = await _get_tool_fn(mcp, "brain_get_neighbors")

        await fn(entity_id=str(uuid4()), rel_types=["SUPERSEDES", "MOTIVATED_BY"])

        kw = graph.get_neighbors.await_args.kwargs
        assert kw.get("rel_types") == ["SUPERSEDES", "MOTIVATED_BY"]

    @pytest.mark.asyncio
    async def test_passes_depth_to_graph_svc(self) -> None:
        graph = MagicMock()
        graph.get_neighbors = AsyncMock(return_value=[])
        mcp = _make_mcp_with_graph(graph)
        fn = await _get_tool_fn(mcp, "brain_get_neighbors")

        await fn(entity_id=str(uuid4()), depth=2)

        kw = graph.get_neighbors.await_args.kwargs
        assert kw.get("depth") == 2

    @pytest.mark.asyncio
    async def test_clamps_depth_to_max_3(self) -> None:
        """depth > 3 is capped to 3 to bound traversal cost."""
        graph = MagicMock()
        graph.get_neighbors = AsyncMock(return_value=[])
        mcp = _make_mcp_with_graph(graph)
        fn = await _get_tool_fn(mcp, "brain_get_neighbors")

        await fn(entity_id=str(uuid4()), depth=10)

        kw = graph.get_neighbors.await_args.kwargs
        assert kw.get("depth") == 3

    @pytest.mark.asyncio
    async def test_clamps_depth_to_min_1(self) -> None:
        """depth < 1 is bumped to 1."""
        graph = MagicMock()
        graph.get_neighbors = AsyncMock(return_value=[])
        mcp = _make_mcp_with_graph(graph)
        fn = await _get_tool_fn(mcp, "brain_get_neighbors")

        await fn(entity_id=str(uuid4()), depth=0)

        kw = graph.get_neighbors.await_args.kwargs
        assert kw.get("depth") == 1

    @pytest.mark.asyncio
    async def test_invalid_uuid_returns_error(self) -> None:
        graph = MagicMock()
        graph.get_neighbors = AsyncMock(return_value=[])
        mcp = _make_mcp_with_graph(graph)
        fn = await _get_tool_fn(mcp, "brain_get_neighbors")

        result = await fn(entity_id="not-a-uuid")

        assert result and result[0].isalnum()
        graph.get_neighbors.assert_not_awaited()
