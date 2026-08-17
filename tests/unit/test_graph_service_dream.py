"""Tests for GraphService dream-related methods."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from brain_v42.services.graph_service import GraphService


@pytest.fixture
def mock_driver():
    return AsyncMock()


@pytest.fixture
def graph(mock_driver):
    return GraphService(driver=mock_driver, timeout=5.0)


class TestFindUnlinkedNodes:
    @pytest.mark.asyncio
    async def test_returns_unlinked_ids(self, graph):
        graph._run_read = AsyncMock(
            return_value=[
                {"id": "aaa-111"},
                {"id": "bbb-222"},
            ]
        )
        result = await graph.find_unlinked_nodes(limit=10)
        assert result == ["aaa-111", "bbb-222"]
        query_arg = graph._run_read.call_args[0][0]
        assert "RELATED_TO" in query_arg
        assert "NOT" in query_arg

    @pytest.mark.asyncio
    async def test_filters_by_entity_type(self, graph):
        graph._run_read = AsyncMock(return_value=[])
        await graph.find_unlinked_nodes(entity_type="Learning", limit=5)
        params = graph._run_read.call_args[0][1]
        assert params["type"] == "Learning"
        assert params["limit"] == 5

    @pytest.mark.asyncio
    async def test_returns_empty_on_no_results(self, graph):
        graph._run_read = AsyncMock(return_value=[])
        result = await graph.find_unlinked_nodes()
        assert result == []


class TestGetAllRelatedEdges:
    @pytest.mark.asyncio
    async def test_returns_edge_pairs(self, graph):
        graph._run_read = AsyncMock(
            return_value=[
                {"src": "aaa-111", "tgt": "bbb-222"},
                {"src": "ccc-333", "tgt": "ddd-444"},
            ]
        )
        result = await graph.get_all_related_edges()
        assert result == [("aaa-111", "bbb-222"), ("ccc-333", "ddd-444")]

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_edges(self, graph):
        graph._run_read = AsyncMock(return_value=[])
        result = await graph.get_all_related_edges()
        assert result == []
