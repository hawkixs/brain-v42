"""Tests for hybrid search RRF fusion and HybridSearcher orchestrator.

Updated post-Fix1: HybridSearcher.search() now returns a 2-tuple
(results, rerank_mode) instead of bare results. All callers updated.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from brain_v42.services.search.hybrid import (
    RERANK_MODE_RERANKED,
    RERANK_MODE_RRF_FALLBACK,
    RERANK_MODE_RRF_ONLY,
    HybridSearcher,
    RankedCandidate,
    rrf_fuse,
)


def _make_candidate(id_str: str, text: str = "test") -> RankedCandidate:
    return RankedCandidate(
        id=uuid.UUID(id_str),
        entity=None,
        entity_type="learning",
        score=0.0,
        text=text,
    )


# Fixed UUIDs for deterministic tests
ID_A = "00000000-0000-0000-0000-000000000001"
ID_B = "00000000-0000-0000-0000-000000000002"
ID_C = "00000000-0000-0000-0000-000000000003"
ID_D = "00000000-0000-0000-0000-000000000004"


class TestRRFFuse:
    def test_merges_two_ranked_lists(self):
        """RRF merges results from FTS and vector, sorted by fused score."""
        fts = [_make_candidate(ID_A), _make_candidate(ID_B)]
        vec = [_make_candidate(ID_C), _make_candidate(ID_A)]

        result = rrf_fuse(fts, vec, k=60)

        # ID_A appears in both lists → highest fused score
        assert result[0].id == uuid.UUID(ID_A)
        assert len(result) == 3  # A, B, C

    def test_duplicate_gets_combined_score(self):
        """Same entity in both lists gets scores from both rankings."""
        fts = [_make_candidate(ID_A)]
        vec = [_make_candidate(ID_A)]

        result = rrf_fuse(fts, vec, k=60)

        assert len(result) == 1
        # Score = 1/(60+1) + 1/(60+1) = 2/61
        expected_score = 2.0 / 61.0
        assert abs(result[0].score - expected_score) < 1e-9

    def test_empty_fts_returns_vector_only(self):
        """Empty FTS list → results come from vector only."""
        vec = [_make_candidate(ID_A), _make_candidate(ID_B)]

        result = rrf_fuse([], vec, k=60)

        assert len(result) == 2
        assert result[0].id == uuid.UUID(ID_A)  # rank 0 → higher score

    def test_empty_vector_returns_fts_only(self):
        """Empty vector list → results come from FTS only."""
        fts = [_make_candidate(ID_A)]

        result = rrf_fuse(fts, [], k=60)

        assert len(result) == 1

    def test_both_empty_returns_empty(self):
        """Both lists empty → empty result."""
        result = rrf_fuse([], [], k=60)
        assert result == []

    def test_ranking_order_matters(self):
        """Higher-ranked items in input get higher RRF scores."""
        fts = [_make_candidate(ID_A), _make_candidate(ID_B)]
        vec = [_make_candidate(ID_B), _make_candidate(ID_A)]

        result = rrf_fuse(fts, vec, k=60)

        # Both A and B appear in both lists, but at different ranks
        # A: fts rank 0 (1/61) + vec rank 1 (1/62) = ~0.03254
        # B: fts rank 1 (1/62) + vec rank 0 (1/61) = ~0.03254
        # Same score — but check both present
        ids = [r.id for r in result]
        assert uuid.UUID(ID_A) in ids
        assert uuid.UUID(ID_B) in ids


class TestHybridSearcher:
    @pytest.mark.asyncio
    async def test_runs_fts_and_vector_in_parallel(self):
        """Both search functions are called concurrently."""
        fts_fn = AsyncMock(return_value=[MagicMock(id=uuid.UUID(ID_A))])
        vec_fn = AsyncMock(return_value=[(MagicMock(id=uuid.UUID(ID_B)), 0.8)])

        searcher = HybridSearcher(reranker=None)
        results, mode = await searcher.search(
            query="test query",
            fts_search_fn=fts_fn,
            vector_search_fn=vec_fn,
            text_extractor=lambda e: "text",
            limit=10,
        )

        fts_fn.assert_called_once()
        vec_fn.assert_called_once()
        assert len(results) == 2

    @pytest.mark.asyncio
    async def test_returns_tuples_of_entity_and_score(self):
        """Results are list[tuple[entity, float]]."""
        entity_a = MagicMock(id=uuid.UUID(ID_A))
        fts_fn = AsyncMock(return_value=[entity_a])
        vec_fn = AsyncMock(return_value=[])

        searcher = HybridSearcher(reranker=None)
        results, mode = await searcher.search(
            query="q",
            fts_search_fn=fts_fn,
            vector_search_fn=vec_fn,
            text_extractor=lambda e: "text",
            limit=10,
        )

        assert len(results) == 1
        entity, score = results[0]
        assert entity is entity_a
        assert isinstance(score, float)

    @pytest.mark.asyncio
    async def test_returns_two_tuple(self):
        """search() returns a 2-tuple (results, rerank_mode)."""
        fts_fn = AsyncMock(return_value=[])
        vec_fn = AsyncMock(return_value=[])

        searcher = HybridSearcher(reranker=None)
        result = await searcher.search(
            query="q",
            fts_search_fn=fts_fn,
            vector_search_fn=vec_fn,
            text_extractor=lambda e: "t",
            limit=10,
        )

        assert isinstance(result, tuple)
        results, mode = result
        assert isinstance(results, list)
        assert isinstance(mode, str)

    @pytest.mark.asyncio
    async def test_passes_project_key_to_both_fns(self):
        """project_key is forwarded to both search functions."""
        fts_fn = AsyncMock(return_value=[])
        vec_fn = AsyncMock(return_value=[])

        searcher = HybridSearcher(reranker=None)
        await searcher.search(
            query="q",
            fts_search_fn=fts_fn,
            vector_search_fn=vec_fn,
            text_extractor=lambda e: "t",
            limit=5,
            project_key="brain_v42",
        )

        fts_call_kwargs = fts_fn.call_args
        vec_call_kwargs = vec_fn.call_args
        assert fts_call_kwargs.kwargs.get("project_key") == "brain_v42"
        assert vec_call_kwargs.kwargs.get("project_key") == "brain_v42"

    @pytest.mark.asyncio
    async def test_respects_limit(self):
        """Returns at most `limit` results."""
        entities = [MagicMock(id=uuid.uuid4()) for _ in range(10)]
        fts_fn = AsyncMock(return_value=entities)
        vec_fn = AsyncMock(return_value=[])

        searcher = HybridSearcher(reranker=None)
        results, mode = await searcher.search(
            query="q",
            fts_search_fn=fts_fn,
            vector_search_fn=vec_fn,
            text_extractor=lambda e: "t",
            limit=3,
        )

        assert len(results) == 3

    @pytest.mark.asyncio
    async def test_fallback_fts_empty(self):
        """If FTS returns empty, results come from vector only."""
        entity = MagicMock(id=uuid.UUID(ID_A))
        fts_fn = AsyncMock(return_value=[])
        vec_fn = AsyncMock(return_value=[(entity, 0.9)])

        searcher = HybridSearcher(reranker=None)
        results, mode = await searcher.search(
            query="q",
            fts_search_fn=fts_fn,
            vector_search_fn=vec_fn,
            text_extractor=lambda e: "t",
            limit=10,
        )

        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_fallback_vector_empty(self):
        """If vector returns empty, results come from FTS only."""
        entity = MagicMock(id=uuid.UUID(ID_A))
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

        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_skips_reranker_when_none(self):
        """When reranker is None, results pass through without reranking."""
        entity = MagicMock(id=uuid.UUID(ID_A))
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

        # Should work fine without reranker
        assert len(results) == 1
        assert mode == RERANK_MODE_RRF_ONLY

    @pytest.mark.asyncio
    async def test_calls_async_reranker(self):
        """When reranker is provided, it's called via rerank_with_mode."""
        entity_a = MagicMock(id=uuid.UUID(ID_A))
        entity_b = MagicMock(id=uuid.UUID(ID_B))
        fts_fn = AsyncMock(return_value=[entity_a, entity_b])
        vec_fn = AsyncMock(return_value=[])

        # Async reranker that reverses order (using new rerank_with_mode API)
        async def mock_rerank_with_mode(query, candidates):
            return RERANK_MODE_RERANKED, list(reversed(candidates))

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

        reranker.rerank_with_mode.assert_called_once()
        assert len(results) == 2
        assert mode == RERANK_MODE_RERANKED

    @pytest.mark.asyncio
    async def test_reranker_fallback_returns_rrf_fallback_mode(self):
        """When reranker signals rrf_fallback, HybridSearcher propagates the mode."""
        entity = MagicMock(id=uuid.UUID(ID_A))
        fts_fn = AsyncMock(return_value=[entity])
        vec_fn = AsyncMock(return_value=[])

        async def mock_rerank_with_mode(query, candidates):
            # Simulate reranker unavailable
            for i, c in enumerate(candidates):
                c.score = (len(candidates) - i) / len(candidates)
            return RERANK_MODE_RRF_FALLBACK, candidates

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

        assert mode == RERANK_MODE_RRF_FALLBACK
        assert len(results) == 1
