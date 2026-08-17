"""Tests for HybridReranker — async adapter around RerankerClient.

Updated post-Fix1: HybridReranker now exposes rerank_with_mode() which
returns a (mode, candidates) 2-tuple instead of bare candidates. The old
rerank() method is removed to avoid ambiguity — callers must use the new API.
"""

from __future__ import annotations

import math
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from brain_v42.services.search.hybrid import RankedCandidate
from brain_v42.services.search.reranker import (
    RERANK_MODE_RERANKED,
    RERANK_MODE_RRF_FALLBACK,
    HybridReranker,
)


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def _make_candidate(id_suffix: int, text: str) -> RankedCandidate:
    return RankedCandidate(
        id=uuid.UUID(f"00000000-0000-0000-0000-{id_suffix:012d}"),
        entity=MagicMock(),
        entity_type="learning",
        score=0.0,
        text=text,
    )


class TestHybridReranker:
    @pytest.mark.asyncio
    async def test_rerank_empty_candidates(self):
        """Empty candidate list returns empty without HTTP call."""
        client = AsyncMock()
        reranker = HybridReranker(client=client)

        mode, result = await reranker.rerank_with_mode("query", [])

        assert result == []
        assert mode == RERANK_MODE_RERANKED
        client.rerank.assert_not_called()

    @pytest.mark.asyncio
    async def test_rerank_calls_client_with_texts(self):
        """Extracts text from candidates and sends to RerankerClient."""
        client = AsyncMock()
        client.rerank.return_value = [2.0, -3.0]
        reranker = HybridReranker(client=client)

        c1 = _make_candidate(1, "hello world")
        c2 = _make_candidate(2, "foo bar")
        await reranker.rerank_with_mode("query", [c1, c2])

        client.rerank.assert_called_once_with("query", ["hello world", "foo bar"])

    @pytest.mark.asyncio
    async def test_rerank_reorders_by_score(self):
        """Candidates are reordered by cross-encoder scores (raw logits)."""
        client = AsyncMock()
        # Raw cross-encoder logits: -5 (low), 7 (high), 1 (mid)
        client.rerank.return_value = [-5.0, 7.0, 1.0]
        reranker = HybridReranker(client=client)

        c1 = _make_candidate(1, "low relevance")
        c2 = _make_candidate(2, "high relevance")
        c3 = _make_candidate(3, "mid relevance")

        mode, result = await reranker.rerank_with_mode("query", [c1, c2, c3])

        assert mode == RERANK_MODE_RERANKED
        assert result[0].id == c2.id  # logit 7 -> sigmoid ~0.999
        assert result[1].id == c3.id  # logit 1 -> sigmoid ~0.731
        assert result[2].id == c1.id  # logit -5 -> sigmoid ~0.007

    @pytest.mark.asyncio
    async def test_rerank_normalizes_scores_with_sigmoid(self):
        """Raw cross-encoder logits are normalized to [0, 1] via sigmoid."""
        client = AsyncMock()
        client.rerank.return_value = [2.0]
        reranker = HybridReranker(client=client)

        c = _make_candidate(1, "text")
        mode, result = await reranker.rerank_with_mode("query", [c])

        assert mode == RERANK_MODE_RERANKED
        assert result[0].score == pytest.approx(_sigmoid(2.0), abs=1e-6)

    @pytest.mark.asyncio
    async def test_rerank_fallback_on_error_returns_rrf_fallback_mode(self):
        """Returns rrf_fallback mode with rank-rescaled scores when client raises."""
        client = AsyncMock()
        client.rerank.side_effect = Exception("connection refused")
        reranker = HybridReranker(client=client)

        c1 = _make_candidate(1, "a")
        c2 = _make_candidate(2, "b")
        mode, result = await reranker.rerank_with_mode("query", [c1, c2])

        assert mode == RERANK_MODE_RRF_FALLBACK
        # Returned in original order (rank preserved)
        assert result[0].id == c1.id
        assert result[1].id == c2.id
        # Scores must be rank-based (not 0.0 which was the original RRF score)
        # n=2: rank 0 → 2/2=1.0, rank 1 → 1/2=0.5
        assert result[0].score == pytest.approx(1.0)
        assert result[1].score == pytest.approx(0.5)

    @pytest.mark.asyncio
    async def test_rerank_fallback_scores_above_default_min_score(self):
        """Rank-based fallback scores must be >= 0.2 (default min_score).

        This was the root cause of incident d3cf29e9 2026-03-30: RRF scores
        (~0.033 max) were returned unchanged, causing min_score=0.2 to filter
        everything out silently.
        """
        client = AsyncMock()
        client.rerank.side_effect = Exception("503 gpu_busy")
        reranker = HybridReranker(client=client)

        # 5 candidates with simulated RRF scores all < 0.2
        candidates = [_make_candidate(i, f"text {i}") for i in range(1, 6)]
        for i, c in enumerate(candidates):
            c.score = 1.0 / (60 + i + 1)  # max ~0.016

        mode, result = await reranker.rerank_with_mode("query", candidates)

        assert mode == RERANK_MODE_RRF_FALLBACK
        for c in result:
            assert c.score >= 0.2, (
                f"score {c.score} is below min_score=0.2 — would be silently filtered"
            )

    @pytest.mark.asyncio
    async def test_is_available_delegates_to_client(self):
        """is_available() delegates to client.is_available()."""
        client = AsyncMock()
        client.is_available.return_value = True
        reranker = HybridReranker(client=client)

        assert await reranker.is_available() is True
        client.is_available.assert_called_once()

    @pytest.mark.asyncio
    async def test_is_available_returns_false(self):
        """is_available() returns False when client is unavailable."""
        client = AsyncMock()
        client.is_available.return_value = False
        reranker = HybridReranker(client=client)

        assert await reranker.is_available() is False
