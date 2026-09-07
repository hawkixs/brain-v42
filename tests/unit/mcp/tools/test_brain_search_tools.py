"""Unit tests for brain_search MCP tool (consolidates former brain_what_do_i_know_about).

brain_what_do_i_know_about has been removed — use brain_search(group_by_type=True) instead.
Uses FastMCP real instance + AsyncMock for BrainService — no real DB or ONNX.
Tests verify correct delegation, parameter passing, type filtering, and serialization.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from structlog.testing import capture_logs

from brain_v42.mcp.tools.brain_tools import register_tools
from brain_v42.models.brain import (
    ALL_TYPES,
    KnowledgeByType,
    KnowledgeType,
    SearchDiagnostics,
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
    "plan": {
        "id": str(uuid4()),
        "plan_id": str(uuid4()),
        "section_title": "Test Plan Section",
        "section_path": "1/2",
        "content": "test plan content",
        "section_order": 0,
        "word_count": 3,
        "project_key": "brain-v42",
        "plan_type": "plan",
        "status": "active",
        "tags": [],
        "created_at": datetime(2026, 3, 1, 12, 0, 0).isoformat(),
    },
}


def _make_search_result(type: KnowledgeType = "learning", score: float = 0.9) -> SearchResult:
    item = _FAKE_ITEMS.get(type, _FAKE_ITEMS["learning"])
    return SearchResult(type=type, score=score, score_kind="cross_encoder", item=item)


def _make_diagnostics(**overrides: Any) -> SearchDiagnostics:
    defaults: dict[str, Any] = {
        "candidates_before_threshold": 1,
        "best_raw_score": 0.9,
        "min_score_requested": 0.2,
        "min_score_effective": 0.2,
        "tags_filtered_out": 0,
        "types_searched": ["learning", "decision"],
        "project_key_requested": None,
        "project_key_effective": None,
        "project_key_injected_by_dream_scope": False,
        "include_archived": False,
        "rerank_mode": "reranked",
        "degraded": False,
    }
    defaults.update(overrides)
    return SearchDiagnostics(**defaults)


def _make_search_response(
    query: str = "test query",
    results: list[SearchResult] | None = None,
    types_searched: list[KnowledgeType] | None = None,
    diagnostics: SearchDiagnostics | None = None,
) -> SearchResponse:
    r = results or [_make_search_result()]
    t = types_searched or ["learning", "decision"]
    return SearchResponse(
        query=query,
        results=r,
        total=len(r),
        types_searched=t,
        diagnostics=diagnostics or _make_diagnostics(),
    )


def _make_what_do_i_know_response(
    topic: str = "PostgreSQL",
    total: int = 2,
    diagnostics: SearchDiagnostics | None = None,
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
        diagnostics=diagnostics or _make_diagnostics(),
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
        SearchResult(type="decision", score=0.95, score_kind="cross_encoder", item=decision),
        SearchResult(type="learning", score=0.90, score_kind="cross_encoder", item=learning),
    )


def _make_mcp_with_brain_svc(metrics_collector: Any | None = None) -> tuple[FastMCP, MagicMock]:
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
        metrics_collector=metrics_collector,
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
    async def test_group_by_type_forwards_types_to_service(self) -> None:
        """brain_search(group_by_type=True, types=[...]) forwards types to what_do_i_know_about.

        The fake service below mirrors BrainService.what_do_i_know_about's real
        contract (types_searched reflects exactly what was received, defaulting
        to ALL_TYPES only when the caller passed none) so that the assertion on
        the returned response's types_searched is meaningful rather than an
        echo of a hard-coded mock.
        """
        mcp, mock_svc = _make_mcp_with_brain_svc()
        captured: dict[str, WhatDoIKnowResponse] = {}

        async def fake_what_do_i_know_about(
            *, topic: str, types: list[KnowledgeType] | None = None, **_kwargs: object
        ) -> WhatDoIKnowResponse:
            types_searched = types if types is not None else list(ALL_TYPES)
            response = WhatDoIKnowResponse(
                topic=topic,
                by_type=KnowledgeByType(),
                total=0,
                types_searched=types_searched,
            )
            captured["response"] = response
            return response

        mock_svc.what_do_i_know_about = AsyncMock(side_effect=fake_what_do_i_know_about)

        fn = await _get_tool_fn(mcp, "brain_search")
        with capture_logs() as logs:
            await fn(query="test", types=["decision"], group_by_type=True)

        call_kwargs = mock_svc.what_do_i_know_about.call_args.kwargs
        assert call_kwargs["types"] == ["decision"]
        assert captured["response"].types_searched == ["decision"]

        events = [log for log in logs if log["event"] == "mcp.brain_search.grouped"]
        assert events[0]["types_requested"] == ["decision"]

    @pytest.mark.asyncio
    async def test_group_by_type_without_types_forwards_none(self) -> None:
        """Positive witness: group_by_type=True without types still forwards types=None."""
        mcp, mock_svc = _make_mcp_with_brain_svc()
        mock_svc.what_do_i_know_about = AsyncMock(return_value=_make_what_do_i_know_response())

        fn = await _get_tool_fn(mcp, "brain_search")
        await fn(query="test", group_by_type=True)

        call_kwargs = mock_svc.what_do_i_know_about.call_args.kwargs
        assert call_kwargs["types"] is None

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


# ── grouped search_log metrics must count every type, including plans (lot G3) ─


class TestGroupedSearchLogScoresCoverAllTypes:
    """The grouped branch's search_log metrics must derive top_score/avg_score
    from every KnowledgeByType attribute, not a hardcoded subset. A search that
    matches only 'plans' must still record a non-null top_score/avg_score.
    """

    @pytest.mark.asyncio
    async def test_grouped_metrics_score_non_null_when_only_plans_match(self) -> None:
        """A plans-only grouped hit must feed non-null top_score/avg_score.

        Regression for the all_scores comprehension that listed
        ['decisions', 'learnings', 'snippets', 'runbooks', 'adrs'] and forgot
        'plans': result_count counted the plan hit while top_score/avg_score
        stayed None.
        """
        metrics_collector = MagicMock()
        metrics_collector.record_search_log = AsyncMock()
        mcp, mock_svc = _make_mcp_with_brain_svc(metrics_collector=metrics_collector)
        plan_result = _make_search_result("plan", 0.77)
        response = WhatDoIKnowResponse(
            topic="plan only",
            by_type=KnowledgeByType(plans=[plan_result]),
            total=1,
            types_searched=["plan"],
        )
        mock_svc.what_do_i_know_about = AsyncMock(return_value=response)

        fn = await _get_tool_fn(mcp, "brain_search")
        await fn(query="plan only", group_by_type=True, types=["plan"])

        metrics_collector.record_search_log.assert_awaited_once()
        call_kwargs = metrics_collector.record_search_log.await_args.kwargs
        assert call_kwargs["result_count"] == 1
        assert call_kwargs["top_score"] == 0.77
        assert call_kwargs["avg_score"] == 0.77


# ── brain_search received-parameters telemetry (lot G3) ────────────────────────


class TestBrainSearchTelemetry:
    """The mcp.brain_search[.grouped] structlog events must journal the shape of
    the parameters the tool RECEIVED (types requested, tags presence/count,
    min_score, group_by_type, include_archived) — never the raw query text.

    Zero-schema: no new table, no migration, no new event name. These are the
    two existing logger.info() calls in brain_search().
    """

    @pytest.mark.asyncio
    async def test_flat_search_logs_received_parameters_with_types_and_tags(self) -> None:
        """A flat search call with types + tags journals their shape, not the query."""
        mcp, mock_svc = _make_mcp_with_brain_svc()
        mock_svc.search = AsyncMock(return_value=_make_search_response())

        fn = await _get_tool_fn(mcp, "brain_search")
        with capture_logs() as logs:
            await fn(
                query="a secret sounding query",
                types=["decision", "learning"],
                project_group="platform",
                tags=["dream:scan", "sec2"],
                min_score=0.42,
                include_archived=True,
                include_related=True,
                full=True,
                limit=10,
            )

        events = [log for log in logs if log["event"] == "mcp.brain_search"]
        assert len(events) == 1
        event = events[0]
        assert event["query_length"] == len("a secret sounding query")
        assert event["types_requested"] == ["decision", "learning"]
        assert event["project_group"] == "platform"
        assert event["tags_present"] is True
        assert event["tags_count"] == 2
        assert event["min_score"] == 0.42
        assert event["include_archived"] is True
        assert event["include_related"] is True
        assert event["full"] is True
        assert event["group_by_type"] is False
        assert event["limit"] == 10
        rendered = repr(logs)
        assert "a secret sounding query" not in rendered

    @pytest.mark.asyncio
    async def test_flat_search_logs_received_parameters_without_types_or_tags(self) -> None:
        """A bare call journals the absence of types/tags and the tool's defaults."""
        mcp, mock_svc = _make_mcp_with_brain_svc()
        mock_svc.search = AsyncMock(return_value=_make_search_response())

        fn = await _get_tool_fn(mcp, "brain_search")
        with capture_logs() as logs:
            await fn(query="test")

        events = [log for log in logs if log["event"] == "mcp.brain_search"]
        assert len(events) == 1
        event = events[0]
        assert event["types_requested"] is None
        assert event["tags_present"] is False
        assert event["tags_count"] == 0
        assert event["min_score"] == 0.2
        assert event["include_archived"] is False
        assert event["group_by_type"] is False
        assert event["limit"] == 20

    @pytest.mark.asyncio
    async def test_grouped_search_logs_received_parameters(self) -> None:
        """group_by_type=True journals the same received-parameter shape.

        tags/include_related are journaled as their EFFECTIVE value, not the
        requested one: what_do_i_know_about() has no tags parameter at all and
        never renders a "### Related" section, so grouped mode structurally
        ignores both — logging the caller's raw request here would claim an
        effect that never happened. See
        test_grouped_search_logs_effective_tags_and_include_related_as_ignored
        for the dedicated regression.
        """
        mcp, mock_svc = _make_mcp_with_brain_svc()
        mock_svc.what_do_i_know_about = AsyncMock(return_value=_make_what_do_i_know_response())

        fn = await _get_tool_fn(mcp, "brain_search")
        with capture_logs() as logs:
            await fn(
                query="PostgreSQL",
                group_by_type=True,
                types=["decision"],
                project_group="platform",
                tags=["ops"],
                min_score=0.5,
                include_archived=True,
                include_related=True,
                full=True,
                limit=7,
            )

        events = [log for log in logs if log["event"] == "mcp.brain_search.grouped"]
        assert len(events) == 1
        event = events[0]
        assert event["types_requested"] == ["decision"]
        assert event["project_group"] == "platform"
        assert event["tags_present"] is False
        assert event["tags_count"] == 0
        assert event["min_score"] == 0.5
        assert event["include_archived"] is True
        assert event["include_related"] is False
        assert event["full"] is True
        assert event["group_by_type"] is True
        assert event["limit"] == 7

    @pytest.mark.asyncio
    async def test_grouped_search_logs_effective_tags_and_include_related_as_ignored(
        self,
    ) -> None:
        """Regression: grouped mode must never echo the requested tags/
        include_related as if they were honoured — it structurally ignores
        both (what_do_i_know_about has no tags param, no related section)."""
        mcp, mock_svc = _make_mcp_with_brain_svc()
        mock_svc.what_do_i_know_about = AsyncMock(return_value=_make_what_do_i_know_response())

        fn = await _get_tool_fn(mcp, "brain_search")
        with capture_logs() as logs:
            await fn(
                query="x",
                group_by_type=True,
                tags=["a", "b", "c"],
                include_related=True,
            )

        event = next(log for log in logs if log["event"] == "mcp.brain_search.grouped")
        assert event["tags_present"] is False
        assert event["tags_count"] == 0
        assert event["include_related"] is False

    @pytest.mark.asyncio
    async def test_grouped_search_logs_the_ignored_request_alongside_the_effective_fields(
        self,
    ) -> None:
        """Fix round (lot G2): the effective fields alone made a call that
        passed tags/include_related byte-indistinguishable from one that
        never did — a W11-style investigation could no longer count how
        often callers hit the silent drop. New fields preserve the caller's
        actual request without changing the effective (always-False/0) ones.
        """
        mcp, mock_svc = _make_mcp_with_brain_svc()
        mock_svc.what_do_i_know_about = AsyncMock(return_value=_make_what_do_i_know_response())

        fn = await _get_tool_fn(mcp, "brain_search")
        with capture_logs() as logs:
            await fn(
                query="x",
                group_by_type=True,
                tags=["a", "b", "c"],
                include_related=True,
            )

        event = next(log for log in logs if log["event"] == "mcp.brain_search.grouped")
        assert event["tags_ignored"] is True
        assert event["tags_requested_count"] == 3
        assert event["include_related_ignored"] is True
        # Effective fields are untouched by this fix.
        assert event["tags_present"] is False
        assert event["tags_count"] == 0
        assert event["include_related"] is False

    @pytest.mark.asyncio
    async def test_grouped_search_logs_ignored_request_as_false_when_none_requested(
        self,
    ) -> None:
        """A bare grouped call (no tags, no include_related) logs the new
        fields as False/0 too — 'ignored' only means 'discarded', never
        implies something was actually asked for."""
        mcp, mock_svc = _make_mcp_with_brain_svc()
        mock_svc.what_do_i_know_about = AsyncMock(return_value=_make_what_do_i_know_response())

        fn = await _get_tool_fn(mcp, "brain_search")
        with capture_logs() as logs:
            await fn(query="x", group_by_type=True)

        event = next(log for log in logs if log["event"] == "mcp.brain_search.grouped")
        assert event["tags_ignored"] is False
        assert event["tags_requested_count"] == 0
        assert event["include_related_ignored"] is False

    @pytest.mark.asyncio
    async def test_flat_search_logs_diagnostics_fields(self) -> None:
        """The flat event journals the 7 new diagnostics fields from the
        service response — computed already, not recomputed at the tool layer."""
        mcp, mock_svc = _make_mcp_with_brain_svc()
        diagnostics = _make_diagnostics(
            candidates_before_threshold=5,
            best_raw_score=0.14,
            tags_filtered_out=2,
            rerank_mode="rrf_fallback",
            degraded=True,
            min_score_effective=0.0,
            project_key_effective="brain-v42",
        )
        mock_svc.search = AsyncMock(
            return_value=_make_search_response(results=[], diagnostics=diagnostics)
        )

        fn = await _get_tool_fn(mcp, "brain_search")
        with capture_logs() as logs:
            await fn(query="x")

        event = next(log for log in logs if log["event"] == "mcp.brain_search")
        assert event["candidates_before_threshold"] == 5
        assert event["best_raw_score"] == pytest.approx(0.14)
        assert event["tags_filtered_out"] == 2
        assert event["rerank_mode"] == "rrf_fallback"
        assert event["degraded"] is True
        assert event["min_score_effective"] == pytest.approx(0.0)
        assert event["project_key_effective"] == "brain-v42"

    @pytest.mark.asyncio
    async def test_grouped_search_logs_diagnostics_fields(self) -> None:
        """The grouped event journals the same 7 diagnostics fields."""
        mcp, mock_svc = _make_mcp_with_brain_svc()
        diagnostics = _make_diagnostics(
            candidates_before_threshold=3,
            best_raw_score=None,
            tags_filtered_out=0,
            rerank_mode=None,
            degraded=False,
            min_score_effective=0.2,
            project_key_effective="dream-owned",
        )
        mock_svc.what_do_i_know_about = AsyncMock(
            return_value=_make_what_do_i_know_response(total=0, diagnostics=diagnostics)
        )

        fn = await _get_tool_fn(mcp, "brain_search")
        with capture_logs() as logs:
            await fn(query="x", group_by_type=True)

        event = next(log for log in logs if log["event"] == "mcp.brain_search.grouped")
        assert event["candidates_before_threshold"] == 3
        assert event["best_raw_score"] is None
        assert event["tags_filtered_out"] == 0
        assert event["rerank_mode"] is None
        assert event["degraded"] is False
        assert event["min_score_effective"] == pytest.approx(0.2)
        assert event["project_key_effective"] == "dream-owned"

    @pytest.mark.asyncio
    async def test_neither_event_leaks_raw_query_or_raw_tags(self) -> None:
        """Both events log only shapes/counts of query and tags — never the raw values.

        Two-sided guard: covers both logger.info() calls (flat "mcp.brain_search"
        and grouped "mcp.brain_search.grouped"), and both leak vectors (the raw
        query text and the raw tag values). The check is applied identically to
        BOTH events against the event's full repr — not a per-value type dispatch
        on str/list — so a leak nested inside a dict or any other container
        (e.g. `ctx={"q": query}` or `tag_audit={"tags": tags}`) cannot escape it
        by hiding behind a type the dispatch didn't check. Mutation-proof: this
        fails if a future edit adds `topic=query`, `tags=tags`, or any other
        echo of the raw payload — nested or not — to either event.
        """
        mcp, mock_svc = _make_mcp_with_brain_svc()
        mock_svc.search = AsyncMock(return_value=_make_search_response())
        mock_svc.what_do_i_know_about = AsyncMock(return_value=_make_what_do_i_know_response())

        raw_query = "a very unique needle query 7f3c9a2e"
        raw_tags = ["needle-tag-alpha", "needle-tag-beta"]

        fn = await _get_tool_fn(mcp, "brain_search")
        with capture_logs() as logs:
            await fn(query=raw_query, tags=raw_tags, group_by_type=False)
            await fn(query=raw_query, tags=raw_tags, group_by_type=True)

        flat_events = [log for log in logs if log["event"] == "mcp.brain_search"]
        grouped_events = [log for log in logs if log["event"] == "mcp.brain_search.grouped"]
        assert len(flat_events) == 1
        assert len(grouped_events) == 1

        for event in (flat_events[0], grouped_events[0]):
            blob = repr(event)
            assert raw_query not in blob
            for raw_tag in raw_tags:
                assert raw_tag not in blob
