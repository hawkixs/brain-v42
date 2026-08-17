"""Hybrid reranker adapter — bridges RerankerClient to HybridSearcher.

Wraps the shared RerankerClient (HTTP, async) to match the interface
expected by HybridSearcher: rerank_with_mode(query, list[RankedCandidate])
-> tuple[str, list[RankedCandidate]] where str is the rerank mode.

Replaces the previous ONNX-based Reranker — one service, zero local model deps.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from brain_v42.services.reranker_client import RerankerClient
    from brain_v42.services.search.hybrid import RankedCandidate

logger = structlog.get_logger(__name__)

# Rerank modes surfaced to callers.
RERANK_MODE_RERANKED = "reranked"
RERANK_MODE_RRF_FALLBACK = "rrf_fallback"


class HybridReranker:
    """Async adapter: RerankerClient → HybridSearcher-compatible reranker.

    Converts RankedCandidate texts to strings for the HTTP API,
    then maps scores back onto the candidates and re-sorts.

    Falls back gracefully when the reranker service is unavailable
    (rrf_fallback mode): candidates are rescaled to rank-based scores
    in [0, 1] so that min_score filtering does not silently discard all
    results (incident d3cf29e9 2026-03-30).
    """

    def __init__(self, client: RerankerClient) -> None:
        self._client: Any = client

    async def rerank_with_mode(
        self,
        query: str,
        candidates: list[RankedCandidate],
    ) -> tuple[str, list[RankedCandidate]]:
        """Score candidates via the HTTP reranker and re-sort.

        Args:
            query: The search query.
            candidates: Pre-fused candidates from RRF (sorted by RRF score desc).

        Returns:
            A 2-tuple (mode, candidates) where mode is one of:
            - "reranked"     — cross-encoder scores applied, sigmoid-normalised
            - "rrf_fallback" — reranker unavailable; rank-based scores applied

        Design:
            In rrf_fallback mode we do NOT pass the tiny RRF scores through
            (max ~0.033 for k=60, rank 0) because they are systematically below
            the default min_score=0.2 and would silently eliminate all results.
            Instead we rescale by rank: score = (n - rank) / n so the top result
            gets score=1.0, second gets (n-1)/n, etc.  The order is preserved
            (best-RRF-first) and all scores are in (0, 1].
        """
        if not candidates:
            return RERANK_MODE_RERANKED, []

        texts = [c.text for c in candidates]
        try:
            scores = await self._client.rerank(query, texts)
        except Exception:
            logger.warning(
                "hybrid_reranker.rrf_fallback",
                reason="reranker_unavailable",
                n_candidates=len(candidates),
            )
            # Rescale by rank so scores are in (0, 1] — compatible with min_score.
            n = len(candidates)
            for rank, candidate in enumerate(candidates):
                candidate.score = (n - rank) / n
            return RERANK_MODE_RRF_FALLBACK, candidates

        for candidate, raw_score in zip(candidates, scores, strict=True):
            # Cross-encoder returns raw logits (e.g. -11 to +8).
            # Normalize to [0, 1] via sigmoid so min_score filtering works.
            candidate.score = 1.0 / (1.0 + math.exp(-float(raw_score)))

        return RERANK_MODE_RERANKED, sorted(candidates, key=lambda c: c.score, reverse=True)

    async def is_available(self) -> bool:
        """Check if the underlying reranker service is healthy."""
        return await self._client.is_available()  # type: ignore[no-any-return]
