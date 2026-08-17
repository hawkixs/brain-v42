"""Unit tests for BrainService — global semantic search orchestrator.

All tests use AsyncMock/MagicMock — no real DB or ONNX model.

Test classes:
1. TestBrainServiceSearch — search() fans out to all services (no type filter)
2. TestBrainServiceSearchTypeFilter — search(types=["decision"]) only queries decision service
3. TestBrainServiceSearchProjectKeyFilter — project_key passed to all services
4. TestBrainServiceSearchMergesAndSorts — results merged and sorted by score DESC
5. TestBrainServiceWhatDoIKnowAbout — grouped by type
6. TestBrainServiceEmptyResults — all services return empty → empty SearchResponse
7. TestBrainServiceServiceException — one service raises, others' results returned
8. TestBrainServiceEmbedCalledOnce — embed_text called once per search(), once per what_do_i_know
9. TestBrainServiceScoreThreshold — results below min_score filtered out
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, call

import pytest

from brain_v42.models.brain import (
    ALL_TYPES,
    KnowledgeByType,
    SearchResponse,
    SearchResult,
    WhatDoIKnowResponse,
)
from brain_v42.models.decision import Decision
from brain_v42.models.indexed_plan_chunk import IndexedPlanChunk
from brain_v42.models.learning import Learning
from brain_v42.models.snippet import Snippet
from brain_v42.services.brain_service import BrainService
from brain_v42.services.search.hybrid import HybridSearcher

# ---------------------------------------------------------------------------
# Helpers / Fixtures
# ---------------------------------------------------------------------------

FAKE_EMBEDDING = [0.1] * 1536
NOW = datetime.now(UTC)


def make_decision(**kwargs) -> Decision:
    defaults = {
        "id": uuid.uuid4(),
        "title": "Use PostgreSQL",
        "description": "We chose PG",
        "reasoning": "Maturity and pgvector",
        "project_key": "brain-v42",
        "created_at": NOW,
        "updated_at": NOW,
    }
    defaults.update(kwargs)
    return Decision(**defaults)


def make_learning(**kwargs) -> Learning:
    defaults = {
        "id": uuid.uuid4(),
        "topic": "TDD",
        "insight": "Red-Green-Refactor works",
        "project_key": "brain-v42",
        "created_at": NOW,
        "updated_at": NOW,
    }
    defaults.update(kwargs)
    return Learning(**defaults)


def make_snippet(**kwargs) -> Snippet:
    defaults = {
        "id": uuid.uuid4(),
        "title": "JSON parser",
        "intention": "Parse JSON from string",
        "code": "import json; json.loads(s)",
        "language": "python",
        "dependencies": [],
        "usage_example": None,
        "gotchas": None,
        "project_key": "brain-v42",
        "tags": [],
        "metadata": {},
        "use_count": 0,
        "last_used_at": None,
        "embedding": None,
        "created_at": NOW,
        "updated_at": NOW,
    }
    defaults.update(kwargs)
    return Snippet.model_validate(defaults)


def make_plan_chunk(**kwargs) -> IndexedPlanChunk:
    defaults = {
        "id": uuid.uuid4(),
        "plan_id": uuid.uuid4(),
        "section_title": "Implementation",
        "section_path": "implementation",
        "content": "Ship the feature.",
        "section_order": 1,
        "word_count": 3,
        "project_key": "brain-v42",
        "plan_type": "plan",
        "status": "active",
        "tags": [],
        "access_count": 0,
        "last_accessed_at": None,
        "created_at": NOW,
    }
    defaults.update(kwargs)
    return IndexedPlanChunk.model_validate(defaults)


def make_mock_services(
    *,
    decision_results: list[tuple] | None = None,
    learning_results: list[tuple] | None = None,
    snippet_results: list[tuple] | None = None,
    runbook_results: list[tuple] | None = None,
    adr_results: list[tuple] | None = None,
) -> tuple:
    """
    Returns (decision_svc, learning_svc, snippet_svc, runbook_svc, adr_svc, embedding_svc).
    Each service mock has async semantic_search that returns the provided results (or []).
    embedding_svc.embed_text is sync and returns FAKE_EMBEDDING.
    """
    decision_svc = MagicMock()
    decision_svc.semantic_search = AsyncMock(return_value=decision_results or [])

    learning_svc = MagicMock()
    learning_svc.semantic_search = AsyncMock(return_value=learning_results or [])

    snippet_svc = MagicMock()
    snippet_svc.semantic_search = AsyncMock(return_value=snippet_results or [])

    runbook_svc = MagicMock()
    runbook_svc.semantic_search = AsyncMock(return_value=runbook_results or [])

    adr_svc = MagicMock()
    adr_svc.semantic_search = AsyncMock(return_value=adr_results or [])

    embedding_svc = MagicMock()
    embedding_svc.embed = AsyncMock(return_value=FAKE_EMBEDDING)
    embedding_svc.embed_text = MagicMock(return_value=FAKE_EMBEDDING)

    return decision_svc, learning_svc, snippet_svc, runbook_svc, adr_svc, embedding_svc


def make_brain_service(
    *,
    decision_results: list[tuple] | None = None,
    learning_results: list[tuple] | None = None,
    snippet_results: list[tuple] | None = None,
    runbook_results: list[tuple] | None = None,
    adr_results: list[tuple] | None = None,
    min_score: float = 0.0,
) -> tuple[BrainService, tuple]:
    svcs = make_mock_services(
        decision_results=decision_results,
        learning_results=learning_results,
        snippet_results=snippet_results,
        runbook_results=runbook_results,
        adr_results=adr_results,
    )
    decision_svc, learning_svc, snippet_svc, runbook_svc, adr_svc, embedding_svc = svcs
    brain = BrainService(
        decision_svc=decision_svc,
        learning_svc=learning_svc,
        snippet_svc=snippet_svc,
        runbook_svc=runbook_svc,
        adr_svc=adr_svc,
        embedding_svc=embedding_svc,
        min_score=min_score,
    )
    return brain, svcs


# ---------------------------------------------------------------------------
# TestBrainServiceSearch — basic fan-out with no type filter
# ---------------------------------------------------------------------------


class TestBrainServiceSearch:
    async def test_search_fans_out_to_all_five_services(self) -> None:
        """search() with no type filter calls semantic_search on all 5 services."""
        brain, svcs = make_brain_service()
        decision_svc, learning_svc, snippet_svc, runbook_svc, adr_svc, _ = svcs

        response = await brain.search("test query")

        assert isinstance(response, SearchResponse)
        decision_svc.semantic_search.assert_awaited_once()
        learning_svc.semantic_search.assert_awaited_once()
        snippet_svc.semantic_search.assert_awaited_once()
        runbook_svc.semantic_search.assert_awaited_once()
        adr_svc.semantic_search.assert_awaited_once()

    async def test_search_returns_search_response(self) -> None:
        """search() returns a SearchResponse instance."""
        brain, _ = make_brain_service()
        response = await brain.search("query")
        assert isinstance(response, SearchResponse)

    async def test_search_response_has_correct_types_searched(self) -> None:
        """search() with no type filter has types_searched = ALL_TYPES."""
        brain, _ = make_brain_service()
        response = await brain.search("query")
        assert set(response.types_searched) == set(ALL_TYPES)

    async def test_search_response_has_query(self) -> None:
        """search() response has the original query string."""
        brain, _ = make_brain_service()
        response = await brain.search("my special query")
        assert response.query == "my special query"


# ---------------------------------------------------------------------------
# TestBrainServiceSearchTypeFilter — filter by type
# ---------------------------------------------------------------------------


class TestBrainServiceSearchTypeFilter:
    async def test_search_with_decision_type_only_calls_decision_svc(self) -> None:
        """search(types=['decision']) only calls DecisionService.semantic_search."""
        brain, svcs = make_brain_service()
        decision_svc, learning_svc, snippet_svc, runbook_svc, adr_svc, _ = svcs

        await brain.search("query", types=["decision"])

        decision_svc.semantic_search.assert_awaited_once()
        learning_svc.semantic_search.assert_not_awaited()
        snippet_svc.semantic_search.assert_not_awaited()
        runbook_svc.semantic_search.assert_not_awaited()
        adr_svc.semantic_search.assert_not_awaited()

    async def test_search_with_snippet_type_only_calls_snippet_svc(self) -> None:
        """search(types=['snippet']) only calls SnippetService.semantic_search."""
        brain, svcs = make_brain_service()
        decision_svc, learning_svc, snippet_svc, runbook_svc, adr_svc, _ = svcs

        await brain.search("query", types=["snippet"])

        snippet_svc.semantic_search.assert_awaited_once()
        decision_svc.semantic_search.assert_not_awaited()
        learning_svc.semantic_search.assert_not_awaited()
        runbook_svc.semantic_search.assert_not_awaited()
        adr_svc.semantic_search.assert_not_awaited()

    async def test_search_with_multiple_types_calls_correct_services(self) -> None:
        """search(types=['decision', 'adr']) calls only decision and adr services."""
        brain, svcs = make_brain_service()
        decision_svc, learning_svc, snippet_svc, runbook_svc, adr_svc, _ = svcs

        await brain.search("query", types=["decision", "adr"])

        decision_svc.semantic_search.assert_awaited_once()
        adr_svc.semantic_search.assert_awaited_once()
        learning_svc.semantic_search.assert_not_awaited()
        snippet_svc.semantic_search.assert_not_awaited()
        runbook_svc.semantic_search.assert_not_awaited()

    async def test_search_type_filter_in_types_searched(self) -> None:
        """search(types=['learning']) sets types_searched=['learning']."""
        brain, _ = make_brain_service()
        response = await brain.search("query", types=["learning"])
        assert response.types_searched == ["learning"]


# ---------------------------------------------------------------------------
# TestBrainServiceSearchProjectKeyFilter — project_key passed to services
# ---------------------------------------------------------------------------


class TestBrainServiceSearchProjectKeyFilter:
    async def test_search_passes_project_key_to_all_services(self) -> None:
        """search(query, project_key='proj') passes project_key to each service call."""
        brain, svcs = make_brain_service()
        decision_svc, learning_svc, snippet_svc, runbook_svc, adr_svc, _ = svcs

        await brain.search("query", project_key="proj")

        # Each service should have been called with project_key="proj"
        for svc in [decision_svc, learning_svc, snippet_svc, runbook_svc, adr_svc]:
            call_kwargs = svc.semantic_search.call_args.kwargs
            assert call_kwargs.get("project_key") == "proj", (
                f"{svc} was not called with project_key='proj', got: {call_kwargs}"
            )

    async def test_search_passes_none_project_key_by_default(self) -> None:
        """search(query) passes project_key=None to services by default."""
        brain, svcs = make_brain_service()
        decision_svc, _, _, _, _, _ = svcs

        await brain.search("query")

        call_kwargs = decision_svc.semantic_search.call_args.kwargs
        assert call_kwargs.get("project_key") is None


# ---------------------------------------------------------------------------
# TestBrainServiceSearchMergesAndSorts — result merging and ordering
# ---------------------------------------------------------------------------


class TestBrainServiceSearchMergesAndSorts:
    async def test_results_from_all_services_are_merged(self) -> None:
        """search() merges results from all services into a flat list."""
        decision = make_decision()
        learning = make_learning()

        brain, _ = make_brain_service(
            decision_results=[(decision, 0.9)],
            learning_results=[(learning, 0.7)],
        )
        response = await brain.search("query")

        assert response.total == 2
        types_in_results = {r.type for r in response.results}
        assert "decision" in types_in_results
        assert "learning" in types_in_results

    async def test_results_sorted_by_score_desc(self) -> None:
        """search() returns results sorted by score DESC."""
        decision1 = make_decision(title="High score")
        decision2 = make_decision(title="Low score")
        learning1 = make_learning(topic="Medium score")

        brain, _ = make_brain_service(
            decision_results=[(decision1, 0.95), (decision2, 0.3)],
            learning_results=[(learning1, 0.75)],
        )
        response = await brain.search("query")

        scores = [r.score for r in response.results]
        assert scores == sorted(scores, reverse=True)

    async def test_search_respects_limit(self) -> None:
        """search() returns at most `limit` results."""
        decisions = [(make_decision(), float(i) / 10) for i in range(10)]
        brain, _ = make_brain_service(decision_results=decisions)

        response = await brain.search("query", limit=3)

        assert len(response.results) <= 3
        assert response.total <= 3

    async def test_search_default_limit_is_20(self) -> None:
        """search() default limit is 20 — returns at most 20 results."""
        decisions = [(make_decision(), 0.9) for _ in range(30)]
        brain, _ = make_brain_service(decision_results=decisions)

        response = await brain.search("query")

        assert len(response.results) <= 20

    async def test_search_result_items_are_serializable_dicts(self) -> None:
        """search() SearchResult.item is a dict (model_dump output)."""
        decision = make_decision()
        brain, _ = make_brain_service(decision_results=[(decision, 0.9)])

        response = await brain.search("query")

        assert len(response.results) == 1
        result = response.results[0]
        assert isinstance(result, SearchResult)
        assert isinstance(result.item, dict)
        assert result.type == "decision"
        assert result.score == 0.9


# ---------------------------------------------------------------------------
# TestBrainServiceWhatDoIKnowAbout — grouped results
# ---------------------------------------------------------------------------


class TestBrainServiceWhatDoIKnowAbout:
    async def test_what_do_i_know_fans_out_to_all_services(self) -> None:
        """what_do_i_know_about() calls semantic_search on all 5 services."""
        brain, svcs = make_brain_service()
        decision_svc, learning_svc, snippet_svc, runbook_svc, adr_svc, _ = svcs

        await brain.what_do_i_know_about("topic")

        decision_svc.semantic_search.assert_awaited_once()
        learning_svc.semantic_search.assert_awaited_once()
        snippet_svc.semantic_search.assert_awaited_once()
        runbook_svc.semantic_search.assert_awaited_once()
        adr_svc.semantic_search.assert_awaited_once()

    async def test_what_do_i_know_returns_what_do_i_know_response(self) -> None:
        """what_do_i_know_about() returns a WhatDoIKnowResponse."""
        brain, _ = make_brain_service()
        response = await brain.what_do_i_know_about("topic")
        assert isinstance(response, WhatDoIKnowResponse)

    async def test_what_do_i_know_groups_by_type(self) -> None:
        """what_do_i_know_about() groups results under by_type fields."""
        decision = make_decision()
        learning = make_learning()
        snippet = make_snippet()

        brain, _ = make_brain_service(
            decision_results=[(decision, 0.9)],
            learning_results=[(learning, 0.8)],
            snippet_results=[(snippet, 0.7)],
        )
        response = await brain.what_do_i_know_about("topic")

        assert isinstance(response.by_type, KnowledgeByType)
        assert len(response.by_type.decisions) == 1
        assert len(response.by_type.learnings) == 1
        assert len(response.by_type.snippets) == 1
        assert len(response.by_type.runbooks) == 0
        assert len(response.by_type.adrs) == 0

    async def test_what_do_i_know_total_across_all_types(self) -> None:
        """what_do_i_know_about() returns correct total count."""
        decision = make_decision()
        learning = make_learning()

        brain, _ = make_brain_service(
            decision_results=[(decision, 0.9)],
            learning_results=[(learning, 0.8)],
        )
        response = await brain.what_do_i_know_about("topic")

        assert response.total == 2

    async def test_what_do_i_know_has_topic_field(self) -> None:
        """what_do_i_know_about('topic') has response.topic == 'topic'."""
        brain, _ = make_brain_service()
        response = await brain.what_do_i_know_about("TDD")
        assert response.topic == "TDD"

    async def test_what_do_i_know_passes_project_key_to_services(self) -> None:
        """what_do_i_know_about(topic, project_key='proj') passes project_key to services."""
        brain, svcs = make_brain_service()
        decision_svc, _, _, _, _, _ = svcs

        await brain.what_do_i_know_about("topic", project_key="proj")

        call_kwargs = decision_svc.semantic_search.call_args.kwargs
        assert call_kwargs.get("project_key") == "proj"


# ---------------------------------------------------------------------------
# TestBrainServiceEmptyResults — all services return empty
# ---------------------------------------------------------------------------


class TestBrainServiceEmptyResults:
    async def test_search_empty_results_returns_empty_search_response(self) -> None:
        """search() with all services returning [] returns empty SearchResponse."""
        brain, _ = make_brain_service()
        response = await brain.search("query")

        assert isinstance(response, SearchResponse)
        assert response.results == []
        assert response.total == 0

    async def test_what_do_i_know_empty_results_returns_empty_response(self) -> None:
        """what_do_i_know_about() with all services returning [] returns empty WhatDoIKnowResponse."""
        brain, _ = make_brain_service()
        response = await brain.what_do_i_know_about("topic")

        assert response.total == 0
        assert response.by_type.decisions == []
        assert response.by_type.learnings == []
        assert response.by_type.snippets == []
        assert response.by_type.runbooks == []
        assert response.by_type.adrs == []


# ---------------------------------------------------------------------------
# TestBrainServiceServiceException — exception isolation
# ---------------------------------------------------------------------------


class TestBrainServiceServiceException:
    async def test_service_exception_does_not_crash_search(self) -> None:
        """A service raising an exception does NOT crash search(). Other results are returned."""
        decision_svc, learning_svc, snippet_svc, runbook_svc, adr_svc, embedding_svc = (
            make_mock_services()
        )
        # Decision service will fail
        decision_svc.semantic_search.side_effect = RuntimeError("DB connection failed")
        # Learning service returns a result
        learning = make_learning()
        learning_svc.semantic_search.return_value = [(learning, 0.85)]

        brain = BrainService(
            decision_svc=decision_svc,
            learning_svc=learning_svc,
            snippet_svc=snippet_svc,
            runbook_svc=runbook_svc,
            adr_svc=adr_svc,
            embedding_svc=embedding_svc,
        )

        # Should NOT raise — exception is isolated
        response = await brain.search("query")

        assert isinstance(response, SearchResponse)
        # The learning result should still be in results
        assert response.total >= 1
        types_in_results = {r.type for r in response.results}
        assert "learning" in types_in_results

    async def test_service_exception_what_do_i_know_does_not_crash(self) -> None:
        """A service exception in what_do_i_know_about() does not crash the call."""
        decision_svc, learning_svc, snippet_svc, runbook_svc, adr_svc, embedding_svc = (
            make_mock_services()
        )
        decision_svc.semantic_search.side_effect = Exception("Unexpected error")
        learning = make_learning()
        learning_svc.semantic_search.return_value = [(learning, 0.9)]

        brain = BrainService(
            decision_svc=decision_svc,
            learning_svc=learning_svc,
            snippet_svc=snippet_svc,
            runbook_svc=runbook_svc,
            adr_svc=adr_svc,
            embedding_svc=embedding_svc,
        )

        response = await brain.what_do_i_know_about("topic")

        assert isinstance(response, WhatDoIKnowResponse)
        assert len(response.by_type.learnings) == 1


# ---------------------------------------------------------------------------
# TestBrainServiceEmbedCalledOnce — embedding pre-computed once
# ---------------------------------------------------------------------------


class TestBrainServiceEmbedCalledOnce:
    """embed() should be called exactly ONCE per search()/what_do_i_know_about(),
    not 5x (once per service). The pre-computed embedding is forwarded to each
    service's semantic_search() via the `embedding` kwarg."""

    async def test_search_embeds_query_exactly_once(self) -> None:
        """search() calls embedding_svc.embed exactly once, not 5x."""
        embedding_svc = AsyncMock()
        embedding_svc.embed = AsyncMock(return_value=FAKE_EMBEDDING)

        decision_svc = MagicMock()
        decision_svc.semantic_search = AsyncMock(return_value=[])
        learning_svc = MagicMock()
        learning_svc.semantic_search = AsyncMock(return_value=[])
        snippet_svc = MagicMock()
        snippet_svc.semantic_search = AsyncMock(return_value=[])
        runbook_svc = MagicMock()
        runbook_svc.semantic_search = AsyncMock(return_value=[])
        adr_svc = MagicMock()
        adr_svc.semantic_search = AsyncMock(return_value=[])

        brain = BrainService(
            decision_svc=decision_svc,
            learning_svc=learning_svc,
            snippet_svc=snippet_svc,
            runbook_svc=runbook_svc,
            adr_svc=adr_svc,
            embedding_svc=embedding_svc,
        )

        await brain.search("test query")

        # embed() called exactly once (not 5x)
        embedding_svc.embed.assert_awaited_once_with("test query")

        # Each service receives the pre-computed embedding
        for svc in [decision_svc, learning_svc, snippet_svc, runbook_svc, adr_svc]:
            call_kwargs = svc.semantic_search.call_args.kwargs
            assert call_kwargs.get("embedding") == FAKE_EMBEDDING

    async def test_what_do_i_know_embeds_query_exactly_once(self) -> None:
        """what_do_i_know_about() calls embedding_svc.embed exactly once."""
        embedding_svc = AsyncMock()
        embedding_svc.embed = AsyncMock(return_value=FAKE_EMBEDDING)

        decision_svc = MagicMock()
        decision_svc.semantic_search = AsyncMock(return_value=[])
        learning_svc = MagicMock()
        learning_svc.semantic_search = AsyncMock(return_value=[])
        snippet_svc = MagicMock()
        snippet_svc.semantic_search = AsyncMock(return_value=[])
        runbook_svc = MagicMock()
        runbook_svc.semantic_search = AsyncMock(return_value=[])
        adr_svc = MagicMock()
        adr_svc.semantic_search = AsyncMock(return_value=[])

        brain = BrainService(
            decision_svc=decision_svc,
            learning_svc=learning_svc,
            snippet_svc=snippet_svc,
            runbook_svc=runbook_svc,
            adr_svc=adr_svc,
            embedding_svc=embedding_svc,
        )

        await brain.what_do_i_know_about("test topic")

        embedding_svc.embed.assert_awaited_once_with("test topic")

    async def test_search_no_embedding_when_svc_is_none(self) -> None:
        """search() with embedding_svc=None still works (no embed call)."""
        decision_svc = MagicMock()
        decision_svc.semantic_search = AsyncMock(return_value=[])
        learning_svc = MagicMock()
        learning_svc.semantic_search = AsyncMock(return_value=[])
        snippet_svc = MagicMock()
        snippet_svc.semantic_search = AsyncMock(return_value=[])
        runbook_svc = MagicMock()
        runbook_svc.semantic_search = AsyncMock(return_value=[])
        adr_svc = MagicMock()
        adr_svc.semantic_search = AsyncMock(return_value=[])

        brain = BrainService(
            decision_svc=decision_svc,
            learning_svc=learning_svc,
            snippet_svc=snippet_svc,
            runbook_svc=runbook_svc,
            adr_svc=adr_svc,
            embedding_svc=None,
        )

        response = await brain.search("query")

        assert isinstance(response, SearchResponse)
        # embedding=None should be passed to services
        for svc in [decision_svc, learning_svc, snippet_svc, runbook_svc, adr_svc]:
            call_kwargs = svc.semantic_search.call_args.kwargs
            assert call_kwargs.get("embedding") is None

    async def test_hybrid_search_passes_embedding(self) -> None:
        """When hybrid_searcher is set, embedding is passed through."""
        embedding_svc = AsyncMock()
        embedding_svc.embed = AsyncMock(return_value=FAKE_EMBEDDING)

        mock_hybrid = MagicMock()
        mock_hybrid.search = AsyncMock(return_value=([], "rrf_only"))

        decision_svc = MagicMock()
        decision_svc.semantic_search = AsyncMock(return_value=[])
        decision_svc.search = AsyncMock(return_value=[])
        learning_svc = MagicMock()
        learning_svc.semantic_search = AsyncMock(return_value=[])
        snippet_svc = MagicMock()
        snippet_svc.semantic_search = AsyncMock(return_value=[])
        runbook_svc = MagicMock()
        runbook_svc.semantic_search = AsyncMock(return_value=[])
        adr_svc = MagicMock()
        adr_svc.semantic_search = AsyncMock(return_value=[])

        brain = BrainService(
            decision_svc=decision_svc,
            learning_svc=learning_svc,
            snippet_svc=snippet_svc,
            runbook_svc=runbook_svc,
            adr_svc=adr_svc,
            embedding_svc=embedding_svc,
            hybrid_searcher=mock_hybrid,
        )

        await brain.search("test query", types=["decision"])

        # embed() called once
        embedding_svc.embed.assert_awaited_once_with("test query")
        # hybrid searcher receives embedding kwarg
        call_kwargs = mock_hybrid.search.call_args.kwargs
        assert call_kwargs.get("embedding") == FAKE_EMBEDDING


# ---------------------------------------------------------------------------
# TestBrainServiceQueryPassedToServices — query forwarding
# ---------------------------------------------------------------------------


class TestBrainServiceQueryPassedToServices:
    async def test_search_passes_query_to_all_services(self) -> None:
        """search() passes query string to each service's semantic_search()."""
        brain, svcs = make_brain_service()
        decision_svc, learning_svc, snippet_svc, runbook_svc, adr_svc, _ = svcs

        await brain.search("query text")

        for svc in [decision_svc, learning_svc, snippet_svc, runbook_svc, adr_svc]:
            call_args = svc.semantic_search.call_args
            assert (
                call_args.args[0] == "query text" or call_args.kwargs.get("query") == "query text"
            ), f"Service was not called with query='query text': {call_args}"

    async def test_what_do_i_know_passes_topic_to_all_services(self) -> None:
        """what_do_i_know_about() passes topic to each service's semantic_search()."""
        brain, svcs = make_brain_service()
        decision_svc, learning_svc, snippet_svc, runbook_svc, adr_svc, _ = svcs

        await brain.what_do_i_know_about("some topic")

        for svc in [decision_svc, learning_svc, snippet_svc, runbook_svc, adr_svc]:
            call_args = svc.semantic_search.call_args
            assert (
                call_args.args[0] == "some topic" or call_args.kwargs.get("query") == "some topic"
            )


# ---------------------------------------------------------------------------
# TestBrainServiceTagFiltering — tags post-filter in _build_search_results
# ---------------------------------------------------------------------------


class TestBrainServiceTagFiltering:
    """Tests for tags post-filtering in _build_search_results."""

    @pytest.mark.asyncio
    async def test_search_filters_by_tags(self) -> None:
        """search(tags=["dream:scan"]) only returns entities with matching tags."""
        matching = make_decision(tags=["dream:scan", "dream:scan:2026-04-05"])
        non_matching = make_decision(tags=["other"])

        brain, _ = make_brain_service(
            decision_results=[(matching, 0.9), (non_matching, 0.8)],
        )

        response = await brain.search("Dream Scan report", tags=["dream:scan"])

        assert response.total == 1
        assert response.results[0].item["tags"] == ["dream:scan", "dream:scan:2026-04-05"]

    @pytest.mark.asyncio
    async def test_search_tags_none_returns_all(self) -> None:
        """search(tags=None) returns all entities (no filtering)."""
        d1 = make_decision(tags=["dream:scan"])
        d2 = make_decision(tags=["other"])

        brain, _ = make_brain_service(
            decision_results=[(d1, 0.9), (d2, 0.8)],
        )

        response = await brain.search("test", tags=None)

        assert response.total == 2


# ---------------------------------------------------------------------------
# TestBrainServiceScoreThreshold — min_score filtering
# ---------------------------------------------------------------------------


class TestBrainServiceScoreThreshold:
    async def test_results_below_min_score_are_filtered(self) -> None:
        """Results with score < min_score are excluded from search results."""
        decision_high = make_decision(title="High score")
        decision_low = make_decision(title="Low score")

        brain, _ = make_brain_service(
            decision_results=[(decision_high, 0.9), (decision_low, 0.2)],
            min_score=0.5,
        )
        response = await brain.search("query")

        assert response.total == 1
        assert response.results[0].score == 0.9

    async def test_default_min_score_zero_includes_all_results(self) -> None:
        """Default min_score=0.0 includes all results regardless of score."""
        decision = make_decision()
        brain, _ = make_brain_service(
            decision_results=[(decision, 0.0)],
            min_score=0.0,
        )
        response = await brain.search("query")
        assert response.total == 1


# ---------------------------------------------------------------------------
# TestBrainServiceHybridSearcher — hybrid search integration
# ---------------------------------------------------------------------------


class TestBrainServiceHybridSearcher:
    async def test_fan_out_uses_hybrid_searcher_when_set(self) -> None:
        """When hybrid_searcher is provided, _fan_out calls it instead of semantic_search."""
        mock_hybrid = MagicMock()
        mock_hybrid.search = AsyncMock(return_value=([(make_learning(), 0.9)], "reranked"))

        svcs = make_mock_services()
        decision_svc, learning_svc, snippet_svc, runbook_svc, adr_svc, embedding_svc = svcs

        brain = BrainService(
            decision_svc=decision_svc,
            learning_svc=learning_svc,
            snippet_svc=snippet_svc,
            runbook_svc=runbook_svc,
            adr_svc=adr_svc,
            embedding_svc=embedding_svc,
            hybrid_searcher=mock_hybrid,
        )

        await brain._fan_out(["learning"], "test query", None, 10)

        mock_hybrid.search.assert_called_once()
        # semantic_search should NOT have been called
        learning_svc.semantic_search.assert_not_awaited()

    async def test_fan_out_fallback_without_hybrid_searcher(self) -> None:
        """Without hybrid_searcher, _fan_out calls semantic_search (old behavior)."""
        brain, svcs = make_brain_service()
        learning_svc = svcs[1]

        await brain._fan_out(["learning"], "test query", None, 10)

        learning_svc.semantic_search.assert_awaited_once()

    @pytest.mark.parametrize("entrypoint", ["search", "what_do_i_know_about"])
    async def test_include_archived_reaches_only_plan_hybrid_callables(
        self,
        entrypoint: str,
    ) -> None:
        """Public archive policy is forwarded to plan SQL without widening peers."""
        svcs = make_mock_services()
        domain_svcs = list(svcs[:5])
        for svc in domain_svcs:
            svc.search = AsyncMock(return_value=[])

        plan_svc = MagicMock()
        plan_svc.search = AsyncMock(return_value=[])
        plan_svc.semantic_search = AsyncMock(return_value=[])
        brain = BrainService(
            decision_svc=svcs[0],
            learning_svc=svcs[1],
            snippet_svc=svcs[2],
            runbook_svc=svcs[3],
            adr_svc=svcs[4],
            embedding_svc=svcs[5],
            hybrid_searcher=HybridSearcher(),
            plan_search_svc=plan_svc,
        )

        if entrypoint == "search":
            await brain.search(
                "roadmap",
                types=["decision", "plan"],
                include_archived=True,
            )
            called_domain_svcs = domain_svcs[:1]
        else:
            await brain.what_do_i_know_about("roadmap", include_archived=True)
            called_domain_svcs = domain_svcs

        assert plan_svc.search.await_args.kwargs["include_archived"] is True
        assert plan_svc.semantic_search.await_args.kwargs["include_archived"] is True
        for svc in called_domain_svcs:
            assert "include_archived" not in svc.search.await_args.kwargs
            assert "include_archived" not in svc.semantic_search.await_args.kwargs


# ---------------------------------------------------------------------------
# TestBrainServiceDecayReranking — decay integration in _build_search_results
# ---------------------------------------------------------------------------


class TestBrainServiceDecayFiltering:
    """Tests for archived/merged entity filtering in _build_search_results."""

    async def test_build_search_results_filters_archived_by_default(self) -> None:
        """Entities with freshness_status='archived' are excluded when include_archived=False."""
        archived_decision = make_decision(title="Archived", freshness_status="archived")
        fresh_decision = make_decision(title="Fresh", freshness_status="fresh")

        brain, _ = make_brain_service(
            decision_results=[
                (archived_decision, 0.9),
                (fresh_decision, 0.8),
            ],
        )
        response = await brain.search("query", include_archived=False)

        assert response.total == 1
        assert response.results[0].item["title"] == "Fresh"

    async def test_plan_search_filters_archived_parent_even_when_chunk_is_active(self) -> None:
        """Plan lifecycle freshness comes from indexed_plans, not its chunk row."""
        chunk = make_plan_chunk(parent_freshness_status="archived", status="active")
        plan_svc = MagicMock()
        plan_svc.semantic_search = AsyncMock(return_value=[(chunk, 0.9)])
        svcs = make_mock_services()
        brain = BrainService(
            decision_svc=svcs[0],
            learning_svc=svcs[1],
            snippet_svc=svcs[2],
            runbook_svc=svcs[3],
            adr_svc=svcs[4],
            embedding_svc=svcs[5],
            plan_search_svc=plan_svc,
        )

        response = await brain.search("roadmap", types=["plan"])
        grouped = await brain.what_do_i_know_about("roadmap")

        assert response.total == 0
        assert grouped.by_type.plans == []

    async def test_build_search_results_includes_archived_when_flag_set(self) -> None:
        """Entities with freshness_status='archived' are included when include_archived=True."""
        archived_decision = make_decision(title="Archived", freshness_status="archived")
        fresh_decision = make_decision(title="Fresh", freshness_status="fresh")

        brain, _ = make_brain_service(
            decision_results=[
                (archived_decision, 0.9),
                (fresh_decision, 0.8),
            ],
        )
        response = await brain.search("query", include_archived=True)

        assert response.total == 2
        titles = {r.item["title"] for r in response.results}
        assert "Archived" in titles
        assert "Fresh" in titles

    async def test_build_search_results_filters_merged_entities(self) -> None:
        """Entities with merged_into set are excluded by default."""
        merged_id = uuid.uuid4()
        merged_decision = make_decision(title="Merged", merged_into=merged_id)
        normal_decision = make_decision(title="Normal")

        brain, _ = make_brain_service(
            decision_results=[
                (merged_decision, 0.9),
                (normal_decision, 0.8),
            ],
        )
        response = await brain.search("query", include_archived=False)

        assert response.total == 1
        assert response.results[0].item["title"] == "Normal"

    async def test_build_search_results_includes_merged_when_archived_flag(self) -> None:
        """Entities with merged_into set are included when include_archived=True."""
        merged_id = uuid.uuid4()
        merged_decision = make_decision(title="Merged", merged_into=merged_id)

        brain, _ = make_brain_service(
            decision_results=[(merged_decision, 0.9)],
        )
        response = await brain.search("query", include_archived=True)

        assert response.total == 1


class TestBrainServiceDecayReranking:
    """Tests for decay-based re-ranking when DecayCalculator is provided."""

    async def test_build_search_results_applies_decay_reranking(self) -> None:
        """With DecayCalculator, results are sorted by effective_score (score * decay)."""
        # decision1: high raw score but low decay multiplier
        decision1 = make_decision(title="Old high score")
        # decision2: lower raw score but high decay multiplier
        decision2 = make_decision(title="Recent low score")

        mock_decay = MagicMock()
        # First call (decision1): low multiplier -> effective = 0.95 * (0.3 + 0.7 * 0.1) = 0.95 * 0.37 = 0.3515
        # Second call (decision2): high multiplier -> effective = 0.7 * (0.3 + 0.7 * 1.0) = 0.7 * 1.0 = 0.7
        mock_decay.compute_multiplier = MagicMock(side_effect=[0.1, 1.0])
        mock_decay.freshness_status = MagicMock(return_value="stale")

        svcs = make_mock_services(
            decision_results=[(decision1, 0.95), (decision2, 0.7)],
        )
        decision_svc, learning_svc, snippet_svc, runbook_svc, adr_svc, embedding_svc = svcs

        brain = BrainService(
            decision_svc=decision_svc,
            learning_svc=learning_svc,
            snippet_svc=snippet_svc,
            runbook_svc=runbook_svc,
            adr_svc=adr_svc,
            embedding_svc=embedding_svc,
            decay_calculator=mock_decay,
        )

        response = await brain.search("query")

        # decision2 should come first (higher effective_score)
        assert response.results[0].item["title"] == "Recent low score"
        assert response.results[1].item["title"] == "Old high score"
        # Raw scores should be preserved in SearchResult.score
        assert response.results[0].score == 0.7
        assert response.results[1].score == 0.95

    async def test_plan_decay_calculation_uses_parent_counters(self) -> None:
        """Plan chunks are scored from their canonical parent's decay state."""
        parent_created = NOW - timedelta(days=400)
        parent_accessed = NOW - timedelta(hours=1)
        chunk = make_plan_chunk(
            access_count=1,
            last_accessed_at=None,
            parent_created_at=parent_created,
            parent_last_accessed_at=parent_accessed,
            parent_access_count=17,
            parent_freshness_status="stale",
        )
        plan_svc = MagicMock()
        plan_svc.semantic_search = AsyncMock(return_value=[(chunk, 0.9)])
        mock_decay = MagicMock()
        mock_decay.compute_multiplier.return_value = 0.8
        mock_decay.freshness_status.return_value = "fresh"
        svcs = make_mock_services()
        brain = BrainService(
            decision_svc=svcs[0],
            learning_svc=svcs[1],
            snippet_svc=svcs[2],
            runbook_svc=svcs[3],
            adr_svc=svcs[4],
            embedding_svc=svcs[5],
            plan_search_svc=plan_svc,
            decay_calculator=mock_decay,
        )

        await brain.search("roadmap", types=["plan"])

        mock_decay.compute_multiplier.assert_called_once_with(
            entity_type="plan",
            created_at=parent_created,
            last_accessed_at=parent_accessed,
            access_count=17,
            is_validated=False,
        )

    async def test_hydrated_parent_null_access_time_overrides_chunk_counter(self) -> None:
        """A never-used parent must not inherit a chunk's historical access time."""
        parent_created = NOW - timedelta(days=400)
        chunk = make_plan_chunk(
            last_accessed_at=NOW,
            parent_created_at=parent_created,
            parent_last_accessed_at=None,
            parent_access_count=0,
            parent_freshness_status="stale",
        )
        plan_svc = MagicMock()
        plan_svc.semantic_search = AsyncMock(return_value=[(chunk, 0.9)])
        mock_decay = MagicMock()
        mock_decay.compute_multiplier.return_value = 0.4
        mock_decay.freshness_status.return_value = "stale"
        svcs = make_mock_services()
        brain = BrainService(
            decision_svc=svcs[0],
            learning_svc=svcs[1],
            snippet_svc=svcs[2],
            runbook_svc=svcs[3],
            adr_svc=svcs[4],
            embedding_svc=svcs[5],
            plan_search_svc=plan_svc,
            decay_calculator=mock_decay,
        )

        await brain.search("roadmap", types=["plan"])

        assert mock_decay.compute_multiplier.call_args.kwargs["last_accessed_at"] is None

    async def test_decay_metadata_injected_into_item(self) -> None:
        """When DecayCalculator is set, _decay_multiplier and _freshness are added to item."""
        decision = make_decision(title="Test")

        mock_decay = MagicMock()
        mock_decay.compute_multiplier = MagicMock(return_value=0.85)
        mock_decay.freshness_status = MagicMock(return_value="warm")

        svcs = make_mock_services(decision_results=[(decision, 0.9)])
        decision_svc, learning_svc, snippet_svc, runbook_svc, adr_svc, embedding_svc = svcs

        brain = BrainService(
            decision_svc=decision_svc,
            learning_svc=learning_svc,
            snippet_svc=snippet_svc,
            runbook_svc=runbook_svc,
            adr_svc=adr_svc,
            embedding_svc=embedding_svc,
            decay_calculator=mock_decay,
        )

        response = await brain.search("query")

        item = response.results[0].item
        assert item["_decay_multiplier"] == 0.85
        assert item["_freshness"] == "warm"

    async def test_no_decay_calculator_means_no_decay_metadata(self) -> None:
        """Without DecayCalculator, no _decay_multiplier or _freshness in item."""
        decision = make_decision()

        brain, _ = make_brain_service(decision_results=[(decision, 0.9)])
        response = await brain.search("query")

        item = response.results[0].item
        assert "_decay_multiplier" not in item
        assert "_freshness" not in item

    async def test_decay_floor_applied(self) -> None:
        """Decay floor prevents effective_score from dropping below score * decay_floor."""
        decision = make_decision()

        mock_decay = MagicMock()
        # multiplier = 0.0 -> effective = score * (floor + (1-floor)*0) = score * floor
        mock_decay.compute_multiplier = MagicMock(return_value=0.0)
        mock_decay.freshness_status = MagicMock(return_value="cold")

        svcs = make_mock_services(decision_results=[(decision, 1.0)])
        decision_svc, learning_svc, snippet_svc, runbook_svc, adr_svc, embedding_svc = svcs

        brain = BrainService(
            decision_svc=decision_svc,
            learning_svc=learning_svc,
            snippet_svc=snippet_svc,
            runbook_svc=runbook_svc,
            adr_svc=adr_svc,
            embedding_svc=embedding_svc,
            decay_calculator=mock_decay,
            decay_floor=0.3,
        )

        response = await brain.search("query")

        # With multiplier=0.0, effective = 1.0 * (0.3 + 0.7 * 0.0) = 0.3
        # The result should still be returned (score=1.0 > min_score=0.2)
        assert response.total == 1


class TestBrainServiceAccessLogging:
    """Tests for access logging after search results are built."""

    async def test_search_logs_access_events(self) -> None:
        """After search(), access_logger.log_access called for each result."""
        decision = make_decision(title="Test Decision")
        learning = make_learning(topic="Test Learning")

        mock_logger = MagicMock()

        svcs = make_mock_services(
            decision_results=[(decision, 0.9)],
            learning_results=[(learning, 0.8)],
        )
        decision_svc, learning_svc, snippet_svc, runbook_svc, adr_svc, embedding_svc = svcs

        brain = BrainService(
            decision_svc=decision_svc,
            learning_svc=learning_svc,
            snippet_svc=snippet_svc,
            runbook_svc=runbook_svc,
            adr_svc=adr_svc,
            embedding_svc=embedding_svc,
            access_logger=mock_logger,
        )

        await brain.search("query")

        assert mock_logger.log_access.call_count == 2

    async def test_search_no_access_logging_without_logger(self) -> None:
        """Without access_logger, search() does not attempt to log access."""
        decision = make_decision()
        brain, _ = make_brain_service(decision_results=[(decision, 0.9)])

        # Should not raise even without access_logger
        response = await brain.search("query")
        assert response.total == 1

    async def test_what_do_i_know_logs_access_events(self) -> None:
        """After what_do_i_know_about(), access_logger.log_access called for results."""
        decision = make_decision()
        mock_logger = MagicMock()

        svcs = make_mock_services(decision_results=[(decision, 0.9)])
        decision_svc, learning_svc, snippet_svc, runbook_svc, adr_svc, embedding_svc = svcs

        brain = BrainService(
            decision_svc=decision_svc,
            learning_svc=learning_svc,
            snippet_svc=snippet_svc,
            runbook_svc=runbook_svc,
            adr_svc=adr_svc,
            embedding_svc=embedding_svc,
            access_logger=mock_logger,
        )

        await brain.what_do_i_know_about("topic")

        assert mock_logger.log_access.call_count == 1

    async def test_access_logger_receives_entity_type_and_id(self) -> None:
        """access_logger.log_access is called with (entity_type, entity_id, 'search_hit')."""
        decision = make_decision(title="Specific Decision")

        mock_logger = MagicMock()

        svcs = make_mock_services(decision_results=[(decision, 0.9)])
        decision_svc, learning_svc, snippet_svc, runbook_svc, adr_svc, embedding_svc = svcs

        brain = BrainService(
            decision_svc=decision_svc,
            learning_svc=learning_svc,
            snippet_svc=snippet_svc,
            runbook_svc=runbook_svc,
            adr_svc=adr_svc,
            embedding_svc=embedding_svc,
            access_logger=mock_logger,
        )

        await brain.search("query")

        mock_logger.log_access.assert_called_once_with("decision", decision.id, "search_hit")

    async def test_search_logs_each_plan_parent_once_as_uuid(self) -> None:
        """Duplicate plan chunks refresh their canonical parent, not each chunk."""
        parent_a = uuid.uuid4()
        parent_b = uuid.uuid4()
        chunks = [
            make_plan_chunk(plan_id=parent_a, section_order=1),
            make_plan_chunk(plan_id=parent_a, section_order=2),
            make_plan_chunk(plan_id=parent_b, section_order=1),
        ]
        plan_svc = MagicMock()
        plan_svc.semantic_search = AsyncMock(
            return_value=[(chunk, 0.9 - index / 10) for index, chunk in enumerate(chunks)]
        )
        mock_logger = MagicMock()
        svcs = make_mock_services()

        brain = BrainService(
            decision_svc=svcs[0],
            learning_svc=svcs[1],
            snippet_svc=svcs[2],
            runbook_svc=svcs[3],
            adr_svc=svcs[4],
            embedding_svc=svcs[5],
            access_logger=mock_logger,
            plan_search_svc=plan_svc,
        )

        await brain.search("roadmap", types=["plan"])

        assert mock_logger.log_access.call_args_list == [
            call("plan", parent_a, "search_hit"),
            call("plan", parent_b, "search_hit"),
        ]

    async def test_what_do_i_know_logs_each_plan_parent_once_as_uuid(self) -> None:
        """Grouped knowledge results retain and deduplicate plan parent IDs."""
        parent_a = uuid.uuid4()
        parent_b = uuid.uuid4()
        chunks = [
            make_plan_chunk(plan_id=parent_a, section_order=1),
            make_plan_chunk(plan_id=parent_a, section_order=2),
            make_plan_chunk(plan_id=parent_b, section_order=1),
        ]
        plan_svc = MagicMock()
        plan_svc.semantic_search = AsyncMock(
            return_value=[(chunk, 0.9 - index / 10) for index, chunk in enumerate(chunks)]
        )
        mock_logger = MagicMock()
        svcs = make_mock_services()

        brain = BrainService(
            decision_svc=svcs[0],
            learning_svc=svcs[1],
            snippet_svc=svcs[2],
            runbook_svc=svcs[3],
            adr_svc=svcs[4],
            embedding_svc=svcs[5],
            access_logger=mock_logger,
            plan_search_svc=plan_svc,
        )

        response = await brain.what_do_i_know_about("roadmap")

        assert [result.parent_id for result in response.by_type.plans] == [
            parent_a,
            parent_a,
            parent_b,
        ]
        assert mock_logger.log_access.call_args_list == [
            call("plan", parent_a, "search_hit"),
            call("plan", parent_b, "search_hit"),
        ]


class TestBrainServiceWhatDoIKnowAboutDecay:
    """Tests for include_archived in what_do_i_know_about()."""

    async def test_what_do_i_know_filters_archived_by_default(self) -> None:
        """what_do_i_know_about() excludes archived entities by default."""
        archived_decision = make_decision(title="Archived", freshness_status="archived")
        fresh_decision = make_decision(title="Fresh", freshness_status="fresh")

        brain, _ = make_brain_service(
            decision_results=[
                (archived_decision, 0.9),
                (fresh_decision, 0.8),
            ],
        )
        response = await brain.what_do_i_know_about("topic", include_archived=False)

        assert response.total == 1
        assert response.by_type.decisions[0].item["title"] == "Fresh"

    async def test_what_do_i_know_includes_archived_when_flag_set(self) -> None:
        """what_do_i_know_about(include_archived=True) includes archived entities."""
        archived_decision = make_decision(title="Archived", freshness_status="archived")

        brain, _ = make_brain_service(
            decision_results=[(archived_decision, 0.9)],
        )
        response = await brain.what_do_i_know_about("topic", include_archived=True)

        assert response.total == 1


# ---------------------------------------------------------------------------
# Task 8: Graph neighbor enrichment in search()
# ---------------------------------------------------------------------------


def make_brain_service_with_graph(
    *,
    decision_results: list[tuple] | None = None,
    learning_results: list[tuple] | None = None,
    min_score: float = 0.0,
    graph: MagicMock | None = None,
) -> tuple[BrainService, tuple, MagicMock]:
    """Build a BrainService with an optional graph mock."""
    svcs = make_mock_services(
        decision_results=decision_results,
        learning_results=learning_results,
    )
    decision_svc, learning_svc, snippet_svc, runbook_svc, adr_svc, embedding_svc = svcs
    if graph is None:
        graph = MagicMock()
        graph.get_related_ids = AsyncMock(return_value={})
        graph.get_project_tree = AsyncMock(return_value=[])
    brain = BrainService(
        decision_svc=decision_svc,
        learning_svc=learning_svc,
        snippet_svc=snippet_svc,
        runbook_svc=runbook_svc,
        adr_svc=adr_svc,
        embedding_svc=embedding_svc,
        min_score=min_score,
        graph=graph,
    )
    return brain, svcs, graph


class TestBrainServiceGraphEnrichment:
    """Task 8: search() calls graph.get_related_ids after fan-out."""

    async def test_search_calls_graph_related_ids_when_graph_available(self) -> None:
        """search() calls graph.get_related_ids with result IDs when graph is set."""
        decision = make_decision()
        graph = MagicMock()
        graph.get_related_ids = AsyncMock(return_value={str(decision.id): []})
        graph.get_project_tree = AsyncMock(return_value=[])

        brain, _, _ = make_brain_service_with_graph(
            decision_results=[(decision, 0.9)],
            graph=graph,
        )

        await brain.search("query")

        graph.get_related_ids.assert_awaited_once()

    async def test_search_includes_related_when_graph_available(self) -> None:
        """search() enriches SearchResponse with related nodes when graph is set."""
        decision = make_decision()
        related_data = [
            {
                "id": str(uuid.uuid4()),
                "type": "Learning",
                "rel": "MOTIVATED_BY",
                "title": "some insight",
            }
        ]
        graph = MagicMock()
        graph.get_related_ids = AsyncMock(return_value={str(decision.id): related_data})
        graph.get_project_tree = AsyncMock(return_value=[])

        brain, _, _ = make_brain_service_with_graph(
            decision_results=[(decision, 0.9)],
            graph=graph,
        )

        response = await brain.search("query")

        # Related data should be present in the response
        assert response.related is not None
        # At least one relation should be present
        assert len(response.related) >= 1

    async def test_search_works_without_graph(self) -> None:
        """search() works normally when graph=None (no related enrichment)."""
        decision = make_decision()
        brain, _ = make_brain_service(decision_results=[(decision, 0.9)])

        response = await brain.search("query")

        assert isinstance(response, SearchResponse)
        assert response.total == 1
        # related should be empty or None when no graph
        assert not response.related

    async def test_search_graph_enrichment_failure_does_not_crash(self) -> None:
        """If graph.get_related_ids raises, search() still returns results."""
        decision = make_decision()
        graph = MagicMock()
        graph.get_related_ids = AsyncMock(side_effect=Exception("neo4j down"))
        graph.get_project_tree = AsyncMock(return_value=[])

        brain, _, _ = make_brain_service_with_graph(
            decision_results=[(decision, 0.9)],
            graph=graph,
        )

        # Should NOT raise
        response = await brain.search("query")
        assert response.total == 1


# ---------------------------------------------------------------------------
# Task 9: Project tree traversal in search()
# ---------------------------------------------------------------------------


class TestBrainServiceProjectTree:
    """Task 9 (Fix 3): Dead fetch removed — get_project_tree must NEVER be called.

    The original Task 9 stored sub-project keys in _sub_project_keys but never
    used them. That wasted one Neo4j roundtrip per scoped search. The dead fetch
    is now removed entirely.  Full multi-project fan-out is still deferred.
    """

    async def test_search_does_not_call_project_tree_with_project_key(self) -> None:
        """Fix 3: search(project_key=...) no longer calls graph.get_project_tree.

        The old test asserted it WAS called — that was encoding the dead fetch bug.
        The new test asserts it is NOT called (dead code removed).
        """
        graph = MagicMock()
        graph.get_related_ids = AsyncMock(return_value={})
        graph.get_project_tree = AsyncMock(return_value=["brain_v42_sub1", "brain_v42_sub2"])

        brain, _, _ = make_brain_service_with_graph(graph=graph)

        await brain.search("query", project_key="brain_v42")

        # Dead fetch removed — must NOT be called
        graph.get_project_tree.assert_not_awaited()

    async def test_search_project_tree_not_called_without_project_key(self) -> None:
        """search() without project_key does NOT call graph.get_project_tree."""
        graph = MagicMock()
        graph.get_related_ids = AsyncMock(return_value={})
        graph.get_project_tree = AsyncMock(return_value=[])

        brain, _, _ = make_brain_service_with_graph(graph=graph)

        await brain.search("query")

        graph.get_project_tree.assert_not_awaited()

    async def test_search_project_tree_not_called_without_graph(self) -> None:
        """search(project_key=...) without graph does NOT call project_tree."""
        # Brain without graph — no project tree traversal
        brain, _ = make_brain_service()

        # Should work fine without graph
        response = await brain.search("query", project_key="brain_v42")

        assert isinstance(response, SearchResponse)

    async def test_search_with_project_key_still_works_after_fix3(self) -> None:
        """After dead fetch removal, search(project_key=...) still returns results correctly."""
        decision = make_decision()
        graph = MagicMock()
        graph.get_related_ids = AsyncMock(return_value={})
        graph.get_project_tree = AsyncMock(return_value=[])  # Never called, but set up for safety

        brain, _, _ = make_brain_service_with_graph(
            decision_results=[(decision, 0.9)],
            graph=graph,
        )

        response = await brain.search("query", project_key="brain_v42")
        assert isinstance(response, SearchResponse)
        assert response.total == 1


# ---------------------------------------------------------------------------
# TestBrainServiceModelDumpTypeerror — TypeError must propagate
# ---------------------------------------------------------------------------


class TestBrainServiceModelDumpTypeerror:
    async def test_model_dump_typeerror_propagates(self) -> None:
        """When model_dump() raises TypeError, it must NOT be caught.

        The bare except Exception was masking real bugs like TypeError.
        After narrowing the catch to (ValueError, AttributeError), TypeError
        should propagate up.
        """
        # Create a mock entity whose model_dump() raises TypeError
        entity = MagicMock()
        entity.model_dump = MagicMock(side_effect=TypeError("unexpected kwarg"))
        entity.created_at = NOW
        entity.freshness_status = "fresh"
        entity.merged_into = None

        # Wire it into a decision service that returns the bad entity
        brain, _ = make_brain_service(
            decision_results=[(entity, 0.9)],
        )

        with pytest.raises(TypeError, match="unexpected kwarg"):
            await brain.search("query")


# ---------------------------------------------------------------------------
# MAJOR: what_do_i_know_about degraded propagation (REVIEW FINDING)
# ---------------------------------------------------------------------------


class TestWhatDoIKnowAboutDegradedPropagation:
    """MAJOR review finding: degraded marker from _fan_out must be propagated
    to WhatDoIKnowResponse.degraded, and the threshold must be bypassed.

    Previously _wdika_degraded was captured but NOT returned — it was silently
    discarded. Rank-based scores (1.00, 0.86…) appeared as confident scores.
    """

    @pytest.mark.asyncio
    async def test_wdika_degraded_field_present_when_reranker_down(self) -> None:
        """WhatDoIKnowResponse must have degraded set when fan-out returns degraded marker.

        MAJOR: _wdika_degraded was previously discarded — WhatDoIKnowResponse had
        no degraded field at all. This test fails before the fix.
        """
        from brain_v42.services.gpu_embedding_service import EmbeddingUnavailable

        decision = make_decision(title="Degraded decision")

        decision_svc = MagicMock()
        decision_svc.search = AsyncMock(return_value=[decision])
        decision_svc.semantic_search = AsyncMock(return_value=[])

        empty_svc = MagicMock()
        empty_svc.search = AsyncMock(return_value=[])
        empty_svc.semantic_search = AsyncMock(return_value=[])

        # Embedding down → fts_fallback degraded marker
        embedding_svc = MagicMock()
        embedding_svc.embed = AsyncMock(
            side_effect=EmbeddingUnavailable("GPU down", kind="unreachable")
        )

        brain = BrainService(
            decision_svc=decision_svc,
            learning_svc=empty_svc,
            snippet_svc=empty_svc,
            runbook_svc=empty_svc,
            adr_svc=empty_svc,
            embedding_svc=embedding_svc,
            min_score=0.2,
        )

        response = await brain.what_do_i_know_about("topic")

        # MAJOR: degraded must be present — was discarded before the fix
        assert isinstance(response, WhatDoIKnowResponse)
        assert response.degraded is not None, (
            "MAJOR: degraded marker from fan-out was discarded — WhatDoIKnowResponse "
            "must expose degraded so format_knowledge_by_type can show the banner."
        )
        assert response.degraded.get("search_mode") == "fts_fallback"

    @pytest.mark.asyncio
    async def test_wdika_degraded_none_when_healthy(self) -> None:
        """WhatDoIKnowResponse.degraded is None when search runs normally."""
        decision = make_decision()
        brain, _ = make_brain_service(decision_results=[(decision, 0.9)])

        response = await brain.what_do_i_know_about("topic")

        assert response.degraded is None

    @pytest.mark.asyncio
    async def test_wdika_degraded_field_present_on_model(self) -> None:
        """WhatDoIKnowResponse model must accept degraded field (rétro-compatible, default None)."""
        # If the field doesn't exist on the model, this construction will fail
        response = WhatDoIKnowResponse(
            topic="test",
            by_type=KnowledgeByType(),
            total=0,
            types_searched=list(ALL_TYPES),
            degraded={"rerank_mode": "rrf_fallback"},
        )
        assert response.degraded == {"rerank_mode": "rrf_fallback"}

    @pytest.mark.asyncio
    async def test_wdika_degraded_default_is_none(self) -> None:
        """WhatDoIKnowResponse.degraded defaults to None (backwards compatible)."""
        response = WhatDoIKnowResponse(
            topic="test",
            by_type=KnowledgeByType(),
            total=0,
            types_searched=list(ALL_TYPES),
        )
        assert response.degraded is None
