"""Unit tests for brain_search MCP tool (consolidates former brain_what_do_i_know_about).

brain_what_do_i_know_about has been removed — use brain_search(group_by_type=True) instead.
Uses FastMCP real instance + AsyncMock for BrainService — no real DB or ONNX.
Tests verify correct delegation, parameter passing, type filtering, and serialization.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from brain_v42.mcp.tools.brain_tools import register_tools
from brain_v42.models.brain import (
    KnowledgeByType,
    KnowledgeType,
    SearchResponse,
    SearchResult,
    WhatDoIKnowResponse,
)
from tests.unit.mcp._tool_error_adapter import capture_tool_errors

# ── Helpers ────────────────────────────────────────────────────────────────────

_FAKE_ITEMS: dict[str, dict] = {
    "learning": {
        "id": str(uuid4()),
        "topic": "test topic",
        "insight": "test insight",
        "source_type": "experience",
        "confidence": "medium",
        "tags": [],
        "metadata": {},
        "created_at": datetime(2026, 3, 1, 12, 0, 0).isoformat(),
        "updated_at": datetime(2026, 3, 1, 12, 0, 0).isoformat(),
    },
    "decision": {
        "id": str(uuid4()),
        "title": "Test Decision",
        "description": "test description",
        "reasoning": "test reasoning",
        "tags": [],
        "metadata": {},
        "created_at": datetime(2026, 3, 1, 12, 0, 0).isoformat(),
        "updated_at": datetime(2026, 3, 1, 12, 0, 0).isoformat(),
    },
}


def _make_search_result(type: KnowledgeType = "learning", score: float = 0.9) -> SearchResult:
    item = _FAKE_ITEMS.get(type, _FAKE_ITEMS["learning"])
    return SearchResult(type=type, score=score, item=item)


def _make_search_response(
    query: str = "test query",
    results: list[SearchResult] | None = None,
    types_searched: list[KnowledgeType] | None = None,
) -> SearchResponse:
    r = results or [_make_search_result()]
    t = types_searched or ["learning", "decision"]
    return SearchResponse(query=query, results=r, total=len(r), types_searched=t)


def _make_what_do_i_know_response(
    topic: str = "PostgreSQL",
    total: int = 2,
) -> WhatDoIKnowResponse:
    by_type = KnowledgeByType(
        decisions=[_make_search_result("decision", 0.95)],
        learnings=[_make_search_result("learning", 0.88)],
    )
    return WhatDoIKnowResponse(
        topic=topic,
        by_type=by_type,
        total=total,
        types_searched=["decision", "learning", "snippet", "runbook", "adr"],
    )


def _make_oversized_body_results() -> tuple[SearchResult, SearchResult]:
    """Return search hits whose bodies would overflow an agent context if rendered."""
    decision = {
        **_FAKE_ITEMS["decision"],
        "description": "OVERSIZED_DESCRIPTION " * 500,
        "reasoning": "OVERSIZED_REASONING " * 500,
        "consequences": "OVERSIZED_CONSEQUENCES " * 500,
    }
    learning = {
        **_FAKE_ITEMS["learning"],
        "insight": "OVERSIZED_INSIGHT " * 500,
    }
    return (
        SearchResult(type="decision", score=0.95, item=decision),
        SearchResult(type="learning", score=0.90, item=learning),
    )


def _make_mcp_with_brain_svc() -> tuple[FastMCP, MagicMock]:
    """Create a test FastMCP instance with a mocked brain_svc."""
    mcp = FastMCP("test-brain")
    mock_brain_svc = MagicMock()
    register_tools(
        mcp,
        decision_svc=MagicMock(),
        learning_svc=MagicMock(),
        snippet_svc=MagicMock(),
        runbook_svc=MagicMock(),
        adr_svc=MagicMock(),
        project_context_svc=MagicMock(),
        brain_svc=mock_brain_svc,
    )
    return mcp, mock_brain_svc


async def _get_tool_fn(mcp: FastMCP, name: str):
    """Get the underlying function of a registered MCP tool."""
    tool = await mcp.get_tool(name)
    return capture_tool_errors(tool.fn)


# ── brain_search tests ─────────────────────────────────────────────────────────


class TestBrainSearch:
    """Tests for brain_search MCP tool."""

    @pytest.mark.asyncio
    async def test_brain_search_registered(self) -> None:
        """brain_search tool is registered via register_tools()."""
        mcp, _ = _make_mcp_with_brain_svc()
        tool = await mcp.get_tool("brain_search")
        assert tool is not None

    @pytest.mark.asyncio
    async def test_brain_search_delegates_to_brain_svc_search(self) -> None:
        """brain_search calls brain_svc.search() with the correct arguments."""
        mcp, mock_svc = _make_mcp_with_brain_svc()
        response = _make_search_response(query="async patterns")
        mock_svc.search = AsyncMock(return_value=response)

        fn = await _get_tool_fn(mcp, "brain_search")
        await fn(query="async patterns")

        mock_svc.search.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_brain_search_passes_query_correctly(self) -> None:
        """brain_search passes the query string to brain_svc.search()."""
        mcp, mock_svc = _make_mcp_with_brain_svc()
        response = _make_search_response(query="pgvector similarity")
        mock_svc.search = AsyncMock(return_value=response)

        fn = await _get_tool_fn(mcp, "brain_search")
        await fn(query="pgvector similarity")

        call_kwargs = mock_svc.search.call_args.kwargs
        assert call_kwargs["query"] == "pgvector similarity"

    @pytest.mark.asyncio
    async def test_brain_search_passes_project_key(self) -> None:
        """brain_search passes project_key to brain_svc.search()."""
        mcp, mock_svc = _make_mcp_with_brain_svc()
        mock_svc.search = AsyncMock(return_value=_make_search_response())

        fn = await _get_tool_fn(mcp, "brain_search")
        await fn(query="test", project_key="brain-v42")

        call_kwargs = mock_svc.search.call_args.kwargs
        assert call_kwargs["project_key"] == "brain-v42"

    @pytest.mark.asyncio
    async def test_brain_search_passes_limit(self) -> None:
        """brain_search passes limit to brain_svc.search()."""
        mcp, mock_svc = _make_mcp_with_brain_svc()
        mock_svc.search = AsyncMock(return_value=_make_search_response())

        fn = await _get_tool_fn(mcp, "brain_search")
        await fn(query="test", limit=5)

        call_kwargs = mock_svc.search.call_args.kwargs
        assert call_kwargs["limit"] == 5

    @pytest.mark.asyncio
    async def test_brain_search_default_limit_is_20(self) -> None:
        """brain_search uses limit=20 by default."""
        mcp, mock_svc = _make_mcp_with_brain_svc()
        mock_svc.search = AsyncMock(return_value=_make_search_response())

        fn = await _get_tool_fn(mcp, "brain_search")
        await fn(query="test")

        call_kwargs = mock_svc.search.call_args.kwargs
        assert call_kwargs["limit"] == 20

    @pytest.mark.asyncio
    async def test_brain_search_passes_none_types_when_not_specified(self) -> None:
        """brain_search passes types=None to brain_svc.search() when not provided."""
        mcp, mock_svc = _make_mcp_with_brain_svc()
        mock_svc.search = AsyncMock(return_value=_make_search_response())

        fn = await _get_tool_fn(mcp, "brain_search")
        await fn(query="test")

        call_kwargs = mock_svc.search.call_args.kwargs
        assert call_kwargs["types"] is None

    @pytest.mark.asyncio
    async def test_brain_search_passes_valid_types(self) -> None:
        """brain_search passes valid type strings to brain_svc.search()."""
        mcp, mock_svc = _make_mcp_with_brain_svc()
        mock_svc.search = AsyncMock(return_value=_make_search_response())

        fn = await _get_tool_fn(mcp, "brain_search")
        await fn(query="test", types=["decision", "learning"])

        call_kwargs = mock_svc.search.call_args.kwargs
        assert call_kwargs["types"] == ["decision", "learning"]

    @pytest.mark.asyncio
    async def test_brain_search_rejects_mixed_invalid_types(self) -> None:
        """The MCP schema rejects a list containing any unknown knowledge type."""
        mcp, mock_svc = _make_mcp_with_brain_svc()
        mock_svc.search = AsyncMock(return_value=_make_search_response())

        async with Client(mcp) as client:
            with pytest.raises(ToolError) as exc_info:
                await client.call_tool(
                    "brain_search",
                    {
                        "query": "test",
                        "types": ["decision", "invalid_type", "runbook"],
                    },
                )

        assert "invalid_type" in str(exc_info.value)
        mock_svc.search.assert_not_called()

    @pytest.mark.asyncio
    async def test_brain_search_rejects_all_invalid_types(self) -> None:
        """The MCP schema rejects a list made only of unknown knowledge types."""
        mcp, mock_svc = _make_mcp_with_brain_svc()
        mock_svc.search = AsyncMock(return_value=_make_search_response())

        async with Client(mcp) as client:
            with pytest.raises(ToolError) as exc_info:
                await client.call_tool(
                    "brain_search",
                    {"query": "test", "types": ["invalid1", "invalid2"]},
                )

        assert "invalid1" in str(exc_info.value)
        mock_svc.search.assert_not_called()

    @pytest.mark.asyncio
    async def test_brain_search_returns_formatted_markdown_string(self) -> None:
        """brain_search returns a formatted markdown string with search results."""
        mcp, mock_svc = _make_mcp_with_brain_svc()
        response = _make_search_response(query="test")
        mock_svc.search = AsyncMock(return_value=response)

        fn = await _get_tool_fn(mcp, "brain_search")
        result = await fn(query="test")

        assert isinstance(result, str)
        assert "test" in result
        assert "1 result" in result

    @pytest.mark.asyncio
    async def test_brain_search_returns_string_type(self) -> None:
        """brain_search returns a string, not a dict."""
        mcp, mock_svc = _make_mcp_with_brain_svc()
        response = _make_search_response()
        mock_svc.search = AsyncMock(return_value=response)

        fn = await _get_tool_fn(mcp, "brain_search")
        result = await fn(query="test")

        assert isinstance(result, str)

    @pytest.mark.asyncio
    async def test_brain_search_omits_related_section_by_default(self) -> None:
        """By default the graph 'Related' block is suppressed to save tokens.

        Neighbour context is only useful once the LLM has picked a hit; for
        the general search call we keep the output lean and let the LLM
        opt in via ``include_related=True`` (or reach for
        ``brain_get_neighbors`` on a specific id).
        """
        from brain_v42.models.brain import SearchResponse

        mcp, mock_svc = _make_mcp_with_brain_svc()
        response = SearchResponse(
            query="x",
            results=[_make_search_result()],
            total=1,
            types_searched=["learning"],
            related=[{"id": "n1", "type": "Decision", "rel": "RELATED_TO", "title": "neighbour"}],
        )
        mock_svc.search = AsyncMock(return_value=response)

        fn = await _get_tool_fn(mcp, "brain_search")
        result = await fn(query="x")

        assert "### Related" not in result
        assert "neighbour" not in result

    @pytest.mark.asyncio
    async def test_brain_search_includes_related_section_when_opt_in(self) -> None:
        from brain_v42.models.brain import SearchResponse

        mcp, mock_svc = _make_mcp_with_brain_svc()
        response = SearchResponse(
            query="x",
            results=[_make_search_result()],
            total=1,
            types_searched=["learning"],
            related=[{"id": "n1", "type": "Decision", "rel": "RELATED_TO", "title": "neighbour"}],
        )
        mock_svc.search = AsyncMock(return_value=response)

        fn = await _get_tool_fn(mcp, "brain_search")
        result = await fn(query="x", include_related=True)

        assert "### Related" in result
        assert "neighbour" in result

    @pytest.mark.asyncio
    async def test_brain_search_empty_types_list_treated_as_none(self) -> None:
        """brain_search treats an empty types list as None (no filtering)."""
        mcp, mock_svc = _make_mcp_with_brain_svc()
        mock_svc.search = AsyncMock(return_value=_make_search_response())

        fn = await _get_tool_fn(mcp, "brain_search")
        await fn(query="test", types=[])

        call_kwargs = mock_svc.search.call_args.kwargs
        # Empty list after filtering = treated as None (search all types)
        assert call_kwargs["types"] is None

    @pytest.mark.asyncio
    async def test_brain_search_does_not_pass_use_semantic_kwarg(self) -> None:
        """brain_search does NOT pass use_semantic to brain_svc.search() (not in BrainService API)."""
        mcp, mock_svc = _make_mcp_with_brain_svc()
        mock_svc.search = AsyncMock(return_value=_make_search_response())

        fn = await _get_tool_fn(mcp, "brain_search")
        await fn(query="test")

        call_kwargs = mock_svc.search.call_args.kwargs
        assert "use_semantic" not in call_kwargs

    @pytest.mark.asyncio
    async def test_brain_search_passes_tags_to_brain_svc(self) -> None:
        """brain_search passes tags to brain_svc.search()."""
        mcp, mock_svc = _make_mcp_with_brain_svc()
        mock_svc.search = AsyncMock(return_value=_make_search_response())
        fn = await _get_tool_fn(mcp, "brain_search")
        await fn(query="Dream Scan report", tags=["dream:scan"])
        call_kwargs = mock_svc.search.call_args.kwargs
        assert call_kwargs["tags"] == ["dream:scan"]

    @pytest.mark.asyncio
    async def test_brain_search_default_tags_is_none(self) -> None:
        """brain_search defaults tags=None when not specified."""
        mcp, mock_svc = _make_mcp_with_brain_svc()
        mock_svc.search = AsyncMock(return_value=_make_search_response())
        fn = await _get_tool_fn(mcp, "brain_search")
        await fn(query="test")
        call_kwargs = mock_svc.search.call_args.kwargs
        assert call_kwargs["tags"] is None

    @pytest.mark.asyncio
    async def test_default_search_omits_oversized_item_bodies(self) -> None:
        """The flat search path must not scale with decision/learning body size."""
        mcp, mock_svc = _make_mcp_with_brain_svc()
        results = list(_make_oversized_body_results())
        mock_svc.search = AsyncMock(return_value=_make_search_response(results=results))

        fn = await _get_tool_fn(mcp, "brain_search")
        result = await fn(query="payload budget")

        assert "OVERSIZED_DESCRIPTION" not in result
        assert "OVERSIZED_REASONING" not in result
        assert "OVERSIZED_CONSEQUENCES" not in result
        assert "OVERSIZED_INSIGHT" not in result
        assert len(result) < 2_000

    @pytest.mark.asyncio
    async def test_search_full_opt_in_returns_complete_item_bodies(self) -> None:
        """The documented full opt-in restores every decision/learning body."""
        mcp, mock_svc = _make_mcp_with_brain_svc()
        results = list(_make_oversized_body_results())
        mock_svc.search = AsyncMock(return_value=_make_search_response(results=results))

        fn = await _get_tool_fn(mcp, "brain_search")
        result = await fn(query="payload budget", full=True)

        assert result.count("OVERSIZED_DESCRIPTION") == 500
        assert result.count("OVERSIZED_REASONING") == 500
        assert result.count("OVERSIZED_CONSEQUENCES") == 500
        assert result.count("OVERSIZED_INSIGHT") == 500

    @pytest.mark.asyncio
    async def test_search_schema_exposes_full_opt_in_with_compact_default(self) -> None:
        """FastMCP must advertise the opt-in without changing the default payload."""
        mcp, _ = _make_mcp_with_brain_svc()

        tool = await mcp.get_tool("brain_search")

        assert tool is not None
        assert tool.parameters["properties"]["full"] == {
            "default": False,
            "type": "boolean",
        }


# ── brain_search group_by_type tests ──────────────────────────────────────────


class TestBrainSearchGroupByType:
    """Tests for brain_search with group_by_type=True (replaces brain_what_do_i_know_about)."""

    @pytest.mark.asyncio
    async def test_brain_what_do_i_know_about_not_registered(self) -> None:
        """brain_what_do_i_know_about is no longer registered (removed)."""
        mcp, _ = _make_mcp_with_brain_svc()
        tool = await mcp.get_tool("brain_what_do_i_know_about")
        assert tool is None

    @pytest.mark.asyncio
    async def test_group_by_type_calls_what_do_i_know_about_service(self) -> None:
        """brain_search(group_by_type=True) calls brain_svc.what_do_i_know_about()."""
        mcp, mock_svc = _make_mcp_with_brain_svc()
        response = _make_what_do_i_know_response(topic="PostgreSQL")
        mock_svc.what_do_i_know_about = AsyncMock(return_value=response)
        mock_svc.search = AsyncMock()

        fn = await _get_tool_fn(mcp, "brain_search")
        await fn(query="PostgreSQL", group_by_type=True)

        mock_svc.what_do_i_know_about.assert_awaited_once()
        mock_svc.search.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_group_by_type_passes_query_as_topic(self) -> None:
        """brain_search(group_by_type=True) passes query as topic to what_do_i_know_about."""
        mcp, mock_svc = _make_mcp_with_brain_svc()
        mock_svc.what_do_i_know_about = AsyncMock(
            return_value=_make_what_do_i_know_response(topic="asyncio")
        )

        fn = await _get_tool_fn(mcp, "brain_search")
        await fn(query="asyncio", group_by_type=True)

        call_kwargs = mock_svc.what_do_i_know_about.call_args.kwargs
        assert call_kwargs["topic"] == "asyncio"

    @pytest.mark.asyncio
    async def test_group_by_type_passes_project_key(self) -> None:
        """brain_search(group_by_type=True) passes project_key to what_do_i_know_about."""
        mcp, mock_svc = _make_mcp_with_brain_svc()
        mock_svc.what_do_i_know_about = AsyncMock(return_value=_make_what_do_i_know_response())

        fn = await _get_tool_fn(mcp, "brain_search")
        await fn(query="test", group_by_type=True, project_key="brain-v42")

        call_kwargs = mock_svc.what_do_i_know_about.call_args.kwargs
        assert call_kwargs["project_key"] == "brain-v42"

    @pytest.mark.asyncio
    async def test_group_by_type_returns_grouped_formatted_string(self) -> None:
        """brain_search(group_by_type=True) returns grouped markdown string."""
        mcp, mock_svc = _make_mcp_with_brain_svc()
        response = _make_what_do_i_know_response(topic="PostgreSQL", total=2)
        mock_svc.what_do_i_know_about = AsyncMock(return_value=response)

        fn = await _get_tool_fn(mcp, "brain_search")
        result = await fn(query="PostgreSQL", group_by_type=True)

        assert isinstance(result, str)
        assert "PostgreSQL" in result
        assert "2 items" in result

    @pytest.mark.asyncio
    async def test_group_by_type_contains_type_sections(self) -> None:
        """brain_search(group_by_type=True) result contains type section headers."""
        mcp, mock_svc = _make_mcp_with_brain_svc()
        response = _make_what_do_i_know_response()
        mock_svc.what_do_i_know_about = AsyncMock(return_value=response)

        fn = await _get_tool_fn(mcp, "brain_search")
        result = await fn(query="test", group_by_type=True)

        assert isinstance(result, str)
        assert "Decisions" in result
        assert "Learnings" in result

    @pytest.mark.asyncio
    async def test_group_by_type_false_uses_normal_search(self) -> None:
        """brain_search(group_by_type=False) uses brain_svc.search() (not what_do_i_know_about)."""
        mcp, mock_svc = _make_mcp_with_brain_svc()
        response = _make_search_response(query="test")
        mock_svc.search = AsyncMock(return_value=response)
        mock_svc.what_do_i_know_about = AsyncMock()

        fn = await _get_tool_fn(mcp, "brain_search")
        await fn(query="test", group_by_type=False)

        mock_svc.search.assert_awaited_once()
        mock_svc.what_do_i_know_about.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_group_by_type_default_is_false(self) -> None:
        """brain_search defaults to group_by_type=False (uses normal search)."""
        mcp, mock_svc = _make_mcp_with_brain_svc()
        response = _make_search_response(query="test")
        mock_svc.search = AsyncMock(return_value=response)
        mock_svc.what_do_i_know_about = AsyncMock()

        fn = await _get_tool_fn(mcp, "brain_search")
        await fn(query="test")

        mock_svc.search.assert_awaited_once()
        mock_svc.what_do_i_know_about.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_grouped_search_omits_oversized_item_bodies_by_default(self) -> None:
        """The grouped path has the same per-item body bound as flat search."""
        mcp, mock_svc = _make_mcp_with_brain_svc()
        decision, learning = _make_oversized_body_results()
        response = WhatDoIKnowResponse(
            topic="payload budget",
            by_type=KnowledgeByType(decisions=[decision], learnings=[learning]),
            total=2,
            types_searched=["decision", "learning"],
        )
        mock_svc.what_do_i_know_about = AsyncMock(return_value=response)

        fn = await _get_tool_fn(mcp, "brain_search")
        result = await fn(query="payload budget", group_by_type=True)

        assert "OVERSIZED_DESCRIPTION" not in result
        assert "OVERSIZED_REASONING" not in result
        assert "OVERSIZED_CONSEQUENCES" not in result
        assert "OVERSIZED_INSIGHT" not in result
        assert len(result) < 2_000

    @pytest.mark.asyncio
    async def test_grouped_search_full_opt_in_returns_complete_item_bodies(self) -> None:
        """The full opt-in applies equally to the grouped path."""
        mcp, mock_svc = _make_mcp_with_brain_svc()
        decision, learning = _make_oversized_body_results()
        response = WhatDoIKnowResponse(
            topic="payload budget",
            by_type=KnowledgeByType(decisions=[decision], learnings=[learning]),
            total=2,
            types_searched=["decision", "learning"],
        )
        mock_svc.what_do_i_know_about = AsyncMock(return_value=response)

        fn = await _get_tool_fn(mcp, "brain_search")
        result = await fn(query="payload budget", group_by_type=True, full=True)

        assert result.count("OVERSIZED_DESCRIPTION") == 500
        assert result.count("OVERSIZED_REASONING") == 500
        assert result.count("OVERSIZED_CONSEQUENCES") == 500
        assert result.count("OVERSIZED_INSIGHT") == 500
