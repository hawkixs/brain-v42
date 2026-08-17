"""Unit tests for brain_reindex_plans MCP tool."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest


class MockMCP:
    """Collecting mock for FastMCP."""

    def __init__(self) -> None:
        self.registered: dict[str, Any] = {}

    def tool(self, **kwargs: Any) -> Any:
        def decorator(fn: Any) -> Any:
            self.registered[fn.__name__] = fn
            return fn

        return decorator


@pytest.fixture
def plan_indexer() -> AsyncMock:
    """Mock PlanIndexer with async methods."""
    indexer = AsyncMock()
    indexer.index_project = AsyncMock(return_value=None)
    indexer.index_all_projects = AsyncMock(return_value={})
    indexer.dedupe_plans = AsyncMock(return_value=0)
    return indexer


@pytest.fixture
def tools(plan_indexer: AsyncMock) -> dict[str, Any]:
    """Register plan tools and return the registered tool dict."""
    from brain_v42.mcp.tools.plan_tools import register_plan_tools

    mcp = MockMCP()
    register_plan_tools(mcp, plan_indexer=plan_indexer)
    return mcp.registered


class TestBrainReindexPlans:
    @pytest.mark.asyncio
    async def test_tool_is_registered(self, tools: dict[str, Any]) -> None:
        """brain_reindex_plans is registered as an MCP tool."""
        assert "brain_reindex_plans" in tools

    @pytest.mark.asyncio
    async def test_single_project_with_results(
        self,
        tools: dict[str, Any],
        plan_indexer: AsyncMock,
    ) -> None:
        """brain_reindex_plans returns stats for a single project."""
        plan_indexer.index_project.return_value = {
            "indexed": 3,
            "skipped": 1,
            "linked": 2,
            "errors": 0,
            "chunks_created": 0,
        }

        result = await tools["brain_reindex_plans"](project_key="brain-v42")

        assert isinstance(result, str)
        assert "brain-v42" in result
        assert "3 indexed" in result
        assert "1 skipped" in result
        assert "2 linked" in result
        plan_indexer.index_project.assert_called_once_with("brain-v42")

    @pytest.mark.asyncio
    async def test_single_project_reports_errors_without_hiding_other_counters(
        self,
        tools: dict[str, Any],
        plan_indexer: AsyncMock,
    ) -> None:
        """Omitting the indexer's errors counter from MCP output must make this fail."""
        plan_indexer.index_project.return_value = {
            "indexed": 3,
            "skipped": 1,
            "linked": 2,
            "errors": 4,
            "chunks_created": 5,
        }

        result = await tools["brain_reindex_plans"](project_key="brain-v42")

        assert result == (
            "## Plan Indexing Results\n\n"
            "**brain-v42**: 3 indexed, 1 skipped, 2 linked, "
            "5 chunks created, 4 errors"
        )

    @pytest.mark.asyncio
    async def test_single_project_no_paths_configured(
        self,
        tools: dict[str, Any],
        plan_indexer: AsyncMock,
    ) -> None:
        """brain_reindex_plans returns message when no scan paths configured."""
        plan_indexer.index_project.return_value = None

        result = await tools["brain_reindex_plans"](project_key="unknown_proj")

        assert isinstance(result, str)
        assert "No plan_scan_paths configured" in result
        assert "unknown_proj" in result

    @pytest.mark.asyncio
    async def test_all_projects(
        self,
        tools: dict[str, Any],
        plan_indexer: AsyncMock,
    ) -> None:
        """brain_reindex_plans indexes all projects when no project_key given."""
        plan_indexer.index_all_projects.return_value = {
            "brain-v42": {
                "indexed": 2,
                "skipped": 0,
                "linked": 2,
                "errors": 0,
                "chunks_created": 0,
            },
            "red_monitor": {
                "indexed": 1,
                "skipped": 3,
                "linked": 1,
                "errors": 0,
                "chunks_created": 0,
            },
        }

        result = await tools["brain_reindex_plans"](project_key=None)

        assert isinstance(result, str)
        assert "brain-v42" in result
        assert "red_monitor" in result
        assert "2 indexed" in result
        assert "1 indexed" in result
        plan_indexer.index_all_projects.assert_called_once()

    @pytest.mark.asyncio
    async def test_all_projects_empty(
        self,
        tools: dict[str, Any],
        plan_indexer: AsyncMock,
    ) -> None:
        """brain_reindex_plans returns header only when no projects have scan paths."""
        plan_indexer.index_all_projects.return_value = {}

        result = await tools["brain_reindex_plans"](project_key=None)

        assert isinstance(result, str)
        assert "Plan Indexing Results" in result

    @pytest.mark.asyncio
    async def test_default_project_key_is_none(
        self,
        tools: dict[str, Any],
        plan_indexer: AsyncMock,
    ) -> None:
        """brain_reindex_plans defaults project_key to None (all projects)."""
        plan_indexer.index_all_projects.return_value = {}

        result = await tools["brain_reindex_plans"]()

        plan_indexer.index_all_projects.assert_called_once()
        assert isinstance(result, str)

    @pytest.mark.asyncio
    async def test_single_project_runs_dedupe(
        self,
        tools: dict[str, Any],
        plan_indexer: AsyncMock,
    ) -> None:
        """brain_reindex_plans must invoke dedupe_plans for the requested project."""
        plan_indexer.index_project.return_value = {
            "indexed": 1,
            "skipped": 0,
            "linked": 0,
            "errors": 0,
            "chunks_created": 0,
        }
        plan_indexer.dedupe_plans.return_value = 3

        result = await tools["brain_reindex_plans"](project_key="brain-v42")

        plan_indexer.dedupe_plans.assert_awaited_once_with("brain-v42")
        # Reported count should surface in the output for visibility.
        assert "3 duplicates removed" in result

    @pytest.mark.asyncio
    async def test_all_projects_runs_dedupe_per_project(
        self,
        tools: dict[str, Any],
        plan_indexer: AsyncMock,
    ) -> None:
        """When indexing all projects, dedupe_plans must run for each result."""
        plan_indexer.index_all_projects.return_value = {
            "proj_a": {
                "indexed": 1,
                "skipped": 0,
                "linked": 0,
                "errors": 0,
                "chunks_created": 0,
            },
            "proj_b": {
                "indexed": 0,
                "skipped": 2,
                "linked": 0,
                "errors": 0,
                "chunks_created": 0,
            },
        }
        plan_indexer.dedupe_plans.side_effect = [2, 0]

        result = await tools["brain_reindex_plans"](project_key=None)

        assert plan_indexer.dedupe_plans.await_count == 2
        called_keys = {call.args[0] for call in plan_indexer.dedupe_plans.await_args_list}
        assert called_keys == {"proj_a", "proj_b"}
        # Only proj_a (deleted=2) should report removed duplicates.
        assert "proj_a" in result and "2 duplicates removed" in result
