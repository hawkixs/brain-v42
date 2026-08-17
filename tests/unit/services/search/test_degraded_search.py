"""RED-phase tests for degraded search resilience fixes.

Three bugs exposed here (written before the fixes so they fail first):

1. BLACKOUT RERANKER (Fix 1): HybridReranker fallback returns RRF scores
   (~0.033 max), which are all < min_score=0.2 default, so brain_search
   silently returns 0 results when the reranker is down.

2. EMBEDDING DOWN = HARD CRASH (Fix 2): EmbeddingUnavailable in _fan_out
   propagates uncaught and crashes brain_search instead of falling back to FTS.

3. DEAD FETCH (Fix 3): graph.get_project_tree() is called but its result is
   never used — 1 wasted Neo4j roundtrip per scoped search.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from brain_v42.models.brain import SearchResponse
from brain_v42.models.decision import Decision
from brain_v42.services.brain_service import BrainService
from brain_v42.services.gpu_embedding_service import EmbeddingUnavailable
from brain_v42.services.search.hybrid import HybridSearcher, RankedCandidate
from brain_v42.services.search.reranker import HybridReranker

NOW = datetime.now(UTC)
FAKE_EMBEDDING = [0.1] * 1536


def _make_decision(**kwargs) -> Decision:
    defaults = {
        "id": uuid.uuid4(),
        "title": "Test Decision",
        "description": "desc",
        "reasoning": "reason",
        "project_key": "brain-v42",
        "created_at": NOW,
        "updated_at": NOW,
    }
    defaults.update(kwargs)
    return Decision(**defaults)


def _make_candidate(id_str: str, score: float = 0.0) -> RankedCandidate:
    return RankedCandidate(
        id=uuid.UUID(id_str),
        entity=_make_decision(),
        entity_type="decision",
        score=score,
        text="some text about a decision",
    )


# ---------------------------------------------------------------------------
# Fix 1: BLACKOUT RERANKER — rrf_fallback mode
# ---------------------------------------------------------------------------


class TestRerankerBlackout:
    """HybridReranker fallback must signal rrf_fallback mode, not return bare candidates."""

    @pytest.mark.asyncio
    async def test_fallback_returns_rrf_fallback_mode(self) -> None:
        """When client raises, rerank() must signal rrf_fallback, not return bare candidates."""
        client = AsyncMock()
        client.rerank.side_effect = Exception("503 gpu_busy")
        reranker = HybridReranker(client=client)

        c1 = _make_candidate("00000000-0000-0000-0000-000000000001", score=1.0 / 61)
        c2 = _make_candidate("00000000-0000-0000-0000-000000000002", score=1.0 / 62)

        mode, result = await reranker.rerank_with_mode("test query", [c1, c2])

        # Mode must be rrf_fallback when service is down
        assert mode == "rrf_fallback"
        # Candidates must survive (non-empty)
        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_fallback_scores_raised_above_min_score(self) -> None:
        """In rrf_fallback mode, scores must NOT be the tiny RRF values (max ~0.033).

        They must be rescaled so min_score=0.2 does not filter everything out.
        """
        client = AsyncMock()
        client.rerank.side_effect = Exception("unreachable")
        reranker = HybridReranker(client=client)

        # Simulate 3 candidates with raw RRF scores (all < 0.2)
        c1 = _make_candidate("00000000-0000-0000-0000-000000000001", score=2.0 / 61)  # ~0.033
        c2 = _make_candidate("00000000-0000-0000-0000-000000000002", score=1.0 / 61)  # ~0.016
        c3 = _make_candidate("00000000-0000-0000-0000-000000000003", score=1.0 / 62)  # ~0.016

        mode, result = await reranker.rerank_with_mode("test query", [c1, c2, c3])

        assert mode == "rrf_fallback"
        # All scores must be >= 0.2 so min_score=0.2 doesn't filter them
        # (rank-based rescaling: score = (n - rank) / n)
        for c in result:
            assert c.score >= 0.2, f"score {c.score} is below min_score=0.2 threshold"

    @pytest.mark.asyncio
    async def test_healthy_reranker_returns_reranked_mode(self) -> None:
        """When client succeeds, rerank_with_mode() returns 'reranked' mode."""
        client = AsyncMock()
        client.rerank.return_value = [2.0, -3.0]
        reranker = HybridReranker(client=client)

        c1 = _make_candidate("00000000-0000-0000-0000-000000000001")
        c2 = _make_candidate("00000000-0000-0000-0000-000000000002")

        mode, result = await reranker.rerank_with_mode("test query", [c1, c2])

        assert mode == "reranked"
        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_empty_candidates_returns_reranked_mode(self) -> None:
        """Empty candidate list → reranked mode (no client call, no fallback)."""
        client = AsyncMock()
        reranker = HybridReranker(client=client)

        mode, result = await reranker.rerank_with_mode("q", [])

        assert mode == "reranked"
        assert result == []

    @pytest.mark.asyncio
    async def test_hybrid_searcher_returns_rerank_mode(self) -> None:
        """HybridSearcher.search() must return (results, rerank_mode) 2-tuple."""
        entity = MagicMock(id=uuid.uuid4())
        fts_fn = AsyncMock(return_value=[entity])
        vec_fn = AsyncMock(return_value=[])

        searcher = HybridSearcher(reranker=None)
        result = await searcher.search(
            query="q",
            fts_search_fn=fts_fn,
            vector_search_fn=vec_fn,
            text_extractor=lambda e: "t",
            limit=10,
        )

        # Must be a 2-tuple: (list[tuple[entity, float]], rerank_mode)
        assert isinstance(result, tuple)
        results, mode = result
        assert isinstance(results, list)
        assert mode in ("reranked", "rrf_fallback", "rrf_only")

    @pytest.mark.asyncio
    async def test_hybrid_searcher_no_reranker_returns_rrf_only_mode(self) -> None:
        """Without reranker, HybridSearcher returns 'rrf_only' mode."""
        entity = MagicMock(id=uuid.uuid4())
        fts_fn = AsyncMock(return_value=[entity])
        vec_fn = AsyncMock(return_value=[])

        searcher = HybridSearcher(reranker=None)
        results, mode = await searcher.search(
            query="q",
            fts_search_fn=fts_fn,
            vector_search_fn=vec_fn,
            text_extractor=lambda e: "t",
            limit=10,
        )

        assert mode == "rrf_only"

    @pytest.mark.asyncio
    async def test_hybrid_searcher_healthy_reranker_returns_reranked_mode(self) -> None:
        """When reranker succeeds, HybridSearcher returns 'reranked' mode."""
        entity_a = MagicMock(id=uuid.uuid4())
        fts_fn = AsyncMock(return_value=[entity_a])
        vec_fn = AsyncMock(return_value=[])

        async def mock_rerank_with_mode(query, candidates):
            return "reranked", candidates

        reranker = MagicMock()
        reranker.rerank_with_mode = AsyncMock(side_effect=mock_rerank_with_mode)

        searcher = HybridSearcher(reranker=reranker)
        results, mode = await searcher.search(
            query="q",
            fts_search_fn=fts_fn,
            vector_search_fn=vec_fn,
            text_extractor=lambda e: "t",
            limit=10,
        )

        assert mode == "reranked"


# ---------------------------------------------------------------------------
# Fix 1b: BrainService regression — reranker down + min_score=0.2
# ---------------------------------------------------------------------------


class TestBrainServiceRerankerBlackout:
    """Regression test: reranker down with min_score=0.2 must not return 0 results."""

    @pytest.mark.asyncio
    async def test_reranker_down_min_score_0_2_returns_results(self) -> None:
        """BrainService with real hybrid searcher, reranker that raises, min_score=0.2.

        This is THE regression test from the spec. The bug: HybridReranker fallback
        returned RRF scores (~0.033) which were all filtered by min_score=0.2.
        After fix: results survive, rerank_mode='rrf_fallback' is set.
        """
        decision = _make_decision(title="PostgreSQL is reliable")

        # Real decision service mock
        decision_svc = MagicMock()
        decision_svc.search = AsyncMock(return_value=[decision])
        decision_svc.semantic_search = AsyncMock(return_value=[(decision, 0.9)])

        # Other services return nothing
        empty_svc = MagicMock()
        empty_svc.search = AsyncMock(return_value=[])
        empty_svc.semantic_search = AsyncMock(return_value=[])

        # Embedding service works fine
        embedding_svc = MagicMock()
        embedding_svc.embed = AsyncMock(return_value=FAKE_EMBEDDING)

        # Reranker client that ALWAYS raises (GPU down)
        reranker_client = AsyncMock()
        reranker_client.rerank = AsyncMock(side_effect=Exception("503 gpu_busy"))

        from brain_v42.services.search.reranker import HybridReranker

        reranker = HybridReranker(client=reranker_client)
        hybrid_searcher = HybridSearcher(reranker=reranker)

        brain = BrainService(
            decision_svc=decision_svc,
            learning_svc=empty_svc,
            snippet_svc=empty_svc,
            runbook_svc=empty_svc,
            adr_svc=empty_svc,
            embedding_svc=embedding_svc,
            min_score=0.2,  # THE critical threshold from the bug
            hybrid_searcher=hybrid_searcher,
        )

        response = await brain.search("PostgreSQL reliability", types=["decision"])

        # Must NOT be empty — this was the bug
        assert response.total > 0, (
            "BUG: reranker blackout + min_score=0.2 filtered ALL results. "
            "Expected rrf_fallback to rescue them."
        )
        # Must carry the degraded marker
        assert response.degraded is not None
        assert response.degraded.get("rerank_mode") == "rrf_fallback"

    @pytest.mark.asyncio
    async def test_healthy_reranker_applies_min_score_normally(self) -> None:
        """When reranker is healthy, min_score filtering works on sigmoid scores."""
        # Sigmoid of -11 = ~0.000016 (well below 0.2)
        decision_low = _make_decision(title="Low relevance")
        decision_high = _make_decision(title="High relevance")

        decision_svc = MagicMock()
        decision_svc.search = AsyncMock(return_value=[decision_low, decision_high])
        decision_svc.semantic_search = AsyncMock(
            return_value=[(decision_low, 0.9), (decision_high, 0.9)]
        )

        empty_svc = MagicMock()
        empty_svc.search = AsyncMock(return_value=[])
        empty_svc.semantic_search = AsyncMock(return_value=[])

        embedding_svc = MagicMock()
        embedding_svc.embed = AsyncMock(return_value=FAKE_EMBEDDING)

        # Healthy reranker: returns -11 (filtered) and 5.0 (passes)
        reranker_client = AsyncMock()
        reranker_client.rerank = AsyncMock(return_value=[-11.0, 5.0])

        from brain_v42.services.search.reranker import HybridReranker

        reranker = HybridReranker(client=reranker_client)
        hybrid_searcher = HybridSearcher(reranker=reranker)

        brain = BrainService(
            decision_svc=decision_svc,
            learning_svc=empty_svc,
            snippet_svc=empty_svc,
            runbook_svc=empty_svc,
            adr_svc=empty_svc,
            embedding_svc=embedding_svc,
            min_score=0.2,
            hybrid_searcher=hybrid_searcher,
        )

        response = await brain.search("test", types=["decision"])

        # Only the high-score result passes sigmoid > 0.2
        assert response.total == 1
        assert response.results[0].item["title"] == "High relevance"
        # No degraded marker when reranker is healthy
        assert response.degraded is None or response.degraded.get("rerank_mode") == "reranked"


# ---------------------------------------------------------------------------
# Fix 2: EMBEDDING DOWN — FTS fallback
# ---------------------------------------------------------------------------


class TestEmbeddingDownFTSFallback:
    """EmbeddingUnavailable must trigger FTS-only fallback, not crash brain_search."""

    @pytest.mark.asyncio
    async def test_embedding_unavailable_does_not_crash(self) -> None:
        """When embedding raises EmbeddingUnavailable, brain_search returns FTS results."""
        decision = _make_decision(title="FTS-only result")

        decision_svc = MagicMock()
        decision_svc.search = AsyncMock(return_value=[decision])
        decision_svc.semantic_search = AsyncMock(return_value=[])

        empty_svc = MagicMock()
        empty_svc.search = AsyncMock(return_value=[])
        empty_svc.semantic_search = AsyncMock(return_value=[])

        # Embedding ALWAYS raises EmbeddingUnavailable
        embedding_svc = MagicMock()
        embedding_svc.embed = AsyncMock(
            side_effect=EmbeddingUnavailable("GPU unreachable", kind="unreachable")
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

        # Must NOT raise — must return FTS results
        response = await brain.search("reliable database", types=["decision"])

        assert isinstance(response, SearchResponse)
        assert response.total > 0, "FTS fallback must return results when embedding is down"

    @pytest.mark.asyncio
    async def test_embedding_unavailable_sets_fts_fallback_marker(self) -> None:
        """EmbeddingUnavailable fallback sets search_mode='fts_fallback' in degraded."""
        decision = _make_decision(title="FTS decision")

        decision_svc = MagicMock()
        decision_svc.search = AsyncMock(return_value=[decision])
        decision_svc.semantic_search = AsyncMock(return_value=[])

        empty_svc = MagicMock()
        empty_svc.search = AsyncMock(return_value=[])
        empty_svc.semantic_search = AsyncMock(return_value=[])

        embedding_svc = MagicMock()
        embedding_svc.embed = AsyncMock(
            side_effect=EmbeddingUnavailable("503 gpu_busy", kind="gpu_busy")
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

        response = await brain.search("test", types=["decision"])

        assert response.degraded is not None
        assert response.degraded.get("search_mode") == "fts_fallback"

    @pytest.mark.asyncio
    async def test_fts_fallback_does_not_call_semantic_search(self) -> None:
        """When embedding is down, semantic_search must NOT be called (it would re-raise)."""
        decision = _make_decision()

        decision_svc = MagicMock()
        decision_svc.search = AsyncMock(return_value=[decision])
        decision_svc.semantic_search = AsyncMock(
            side_effect=EmbeddingUnavailable("should not be called")
        )

        empty_svc = MagicMock()
        empty_svc.search = AsyncMock(return_value=[])
        empty_svc.semantic_search = AsyncMock(return_value=[])

        embedding_svc = MagicMock()
        embedding_svc.embed = AsyncMock(side_effect=EmbeddingUnavailable("GPU down"))

        brain = BrainService(
            decision_svc=decision_svc,
            learning_svc=empty_svc,
            snippet_svc=empty_svc,
            runbook_svc=empty_svc,
            adr_svc=empty_svc,
            embedding_svc=embedding_svc,
        )

        response = await brain.search("test", types=["decision"])

        # semantic_search must NOT have been called (it would re-raise EmbeddingUnavailable)
        decision_svc.semantic_search.assert_not_awaited()
        assert isinstance(response, SearchResponse)

    @pytest.mark.asyncio
    async def test_fts_fallback_results_have_rank_based_scores(self) -> None:
        """FTS fallback assigns rank-based scores (not RRF, not 0.0)."""
        d1 = _make_decision(title="First FTS result")
        d2 = _make_decision(title="Second FTS result")
        d3 = _make_decision(title="Third FTS result")

        decision_svc = MagicMock()
        decision_svc.search = AsyncMock(return_value=[d1, d2, d3])
        decision_svc.semantic_search = AsyncMock(return_value=[])

        empty_svc = MagicMock()
        empty_svc.search = AsyncMock(return_value=[])
        empty_svc.semantic_search = AsyncMock(return_value=[])

        embedding_svc = MagicMock()
        embedding_svc.embed = AsyncMock(side_effect=EmbeddingUnavailable("down"))

        brain = BrainService(
            decision_svc=decision_svc,
            learning_svc=empty_svc,
            snippet_svc=empty_svc,
            runbook_svc=empty_svc,
            adr_svc=empty_svc,
            embedding_svc=embedding_svc,
            min_score=0.0,  # Accept all
        )

        response = await brain.search("test", types=["decision"])

        # Scores must be strictly decreasing (rank-based) and all > 0
        scores = [r.score for r in response.results]
        assert all(s > 0 for s in scores)
        assert scores == sorted(scores, reverse=True)


# ---------------------------------------------------------------------------
# Fix 3: DEAD FETCH — graph.get_project_tree not called
# ---------------------------------------------------------------------------


class TestDeadFetchRemoved:
    """After Fix 3, graph.get_project_tree must NOT be called during search()."""

    @pytest.mark.asyncio
    async def test_search_with_project_key_does_not_call_get_project_tree(self) -> None:
        """After Task 9 dead code removal, get_project_tree is never called."""
        decision = _make_decision()

        decision_svc = MagicMock()
        decision_svc.semantic_search = AsyncMock(return_value=[(decision, 0.9)])

        empty_svc = MagicMock()
        empty_svc.semantic_search = AsyncMock(return_value=[])

        embedding_svc = MagicMock()
        embedding_svc.embed = AsyncMock(return_value=FAKE_EMBEDDING)

        graph = MagicMock()
        graph.get_related_ids = AsyncMock(return_value={})
        graph.get_project_tree = AsyncMock(return_value=["sub1", "sub2"])

        brain = BrainService(
            decision_svc=decision_svc,
            learning_svc=empty_svc,
            snippet_svc=empty_svc,
            runbook_svc=empty_svc,
            adr_svc=empty_svc,
            embedding_svc=embedding_svc,
            graph=graph,
            min_score=0.0,
        )

        await brain.search("test", project_key="brain-v42")

        # Must NOT have been called — Task 9 dead code removed
        graph.get_project_tree.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_search_still_enriches_related_after_dead_fetch_removal(self) -> None:
        """Task 8 graph enrichment (get_related_ids) must still work after fix."""
        decision = _make_decision()
        related_data = [
            {"id": str(uuid.uuid4()), "type": "Learning", "rel": "MOTIVATED_BY", "title": "insight"}
        ]

        decision_svc = MagicMock()
        decision_svc.semantic_search = AsyncMock(return_value=[(decision, 0.9)])

        empty_svc = MagicMock()
        empty_svc.semantic_search = AsyncMock(return_value=[])

        embedding_svc = MagicMock()
        embedding_svc.embed = AsyncMock(return_value=FAKE_EMBEDDING)

        graph = MagicMock()
        graph.get_related_ids = AsyncMock(return_value={str(decision.id): related_data})
        graph.get_project_tree = AsyncMock(return_value=[])

        brain = BrainService(
            decision_svc=decision_svc,
            learning_svc=empty_svc,
            snippet_svc=empty_svc,
            runbook_svc=empty_svc,
            adr_svc=empty_svc,
            embedding_svc=embedding_svc,
            graph=graph,
            min_score=0.0,
        )

        response = await brain.search("test", project_key="brain-v42")

        # Task 8 enrichment still works
        graph.get_related_ids.assert_awaited_once()
        assert len(response.related) >= 1
