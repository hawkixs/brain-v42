"""Hybrid search: FTS + vector + RRF fusion + optional reranking.

HybridSearcher.search() now returns a 2-tuple (results, rerank_mode) so
callers can surface degradation to the LLM.  Modes:
  - "reranked"     — cross-encoder applied
  - "rrf_fallback" — reranker unavailable; rank-based scores
  - "rrf_only"     — no reranker configured; RRF scores as-is
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import structlog

logger = structlog.get_logger(__name__)

# Exposed mode constants so callers can compare without hard-coding strings.
RERANK_MODE_RERANKED = "reranked"
RERANK_MODE_RRF_FALLBACK = "rrf_fallback"
RERANK_MODE_RRF_ONLY = "rrf_only"


@dataclass
class RankedCandidate:
    """Internal candidate for RRF fusion and reranking."""

    id: UUID
    entity: Any
    entity_type: str
    score: float
    text: str


def rrf_fuse(
    fts_results: list[RankedCandidate],
    vec_results: list[RankedCandidate],
    k: int = 60,
) -> list[RankedCandidate]:
    """Reciprocal Rank Fusion. Pure function, no IO.

    Combines rankings from FTS and vector search. Items appearing in both
    lists get scores from both rankings summed.

    Args:
        fts_results: Candidates ranked by FTS ts_rank (best first).
        vec_results: Candidates ranked by vector cosine similarity (best first).
        k: RRF smoothing constant (standard: 60).

    Returns:
        Merged candidates sorted by fused score descending.
    """
    scores: dict[UUID, float] = {}
    candidates: dict[UUID, RankedCandidate] = {}

    for rank, c in enumerate(fts_results):
        scores[c.id] = scores.get(c.id, 0.0) + 1.0 / (k + rank + 1)
        candidates[c.id] = c

    for rank, c in enumerate(vec_results):
        scores[c.id] = scores.get(c.id, 0.0) + 1.0 / (k + rank + 1)
        candidates.setdefault(c.id, c)

    for cid, candidate in candidates.items():
        candidate.score = scores[cid]

    return sorted(candidates.values(), key=lambda c: c.score, reverse=True)


class HybridSearcher:
    """Orchestrates FTS + vector + RRF + optional reranking.

    Wraps existing service search methods. Does NOT replace services —
    it composes them.
    """

    def __init__(self, reranker: Any | None = None) -> None:
        self._reranker = reranker

    async def search(
        self,
        query: str,
        fts_search_fn: Callable[..., Coroutine[Any, Any, Any]],
        vector_search_fn: Callable[..., Coroutine[Any, Any, Any]],
        text_extractor: Callable[[Any], str],
        limit: int = 10,
        project_key: str | None = None,
        embedding: list[float] | None = None,
        project_keys: list[str] | None = None,
    ) -> tuple[list[tuple[Any, float]], str]:
        """Run hybrid: FTS + vector in parallel -> RRF -> optional rerank.

        Only `project_key` and `project_keys` are forwarded to both search
        functions (the common filters). Entity-specific filters should be
        applied as post-filters by the caller.

        Args:
            query: Search query string.
            fts_search_fn: Async function returning list[Entity] (FTS ranked).
            vector_search_fn: Async function returning list[tuple[Entity, float]].
            text_extractor: Extracts text from entity for reranker input.
            limit: Max results to return.
            project_key: Optional project scope filter.
            embedding: Optional pre-computed embedding vector forwarded to vector_search_fn.
            project_keys: Optional list of project keys for group-based filtering.

        Returns:
            2-tuple (list[tuple[entity, score]], rerank_mode) sorted by score desc.
            rerank_mode is one of: "reranked", "rrf_fallback", "rrf_only".
        """
        common_kwargs: dict[str, Any] = {}
        if project_key is not None:
            common_kwargs["project_key"] = project_key
        if project_keys is not None:
            common_kwargs["project_keys"] = project_keys

        fts_raw, vec_raw = await asyncio.gather(
            fts_search_fn(query=query, limit=50, **common_kwargs),
            vector_search_fn(query=query, limit=50, embedding=embedding, **common_kwargs),
        )

        # Convert to RankedCandidates
        fts_candidates = [
            RankedCandidate(
                id=e.id,
                entity=e,
                entity_type="",
                score=0.0,
                text=text_extractor(e),
            )
            for e in fts_raw
        ]
        vec_candidates = [
            RankedCandidate(
                id=e.id,
                entity=e,
                entity_type="",
                score=s,
                text=text_extractor(e),
            )
            for e, s in vec_raw
        ]

        fused = rrf_fuse(fts_candidates, vec_candidates, k=60)[:20]

        if self._reranker and fused:
            rerank_mode, fused = await self._reranker.rerank_with_mode(query, fused)
        else:
            rerank_mode = RERANK_MODE_RRF_ONLY

        return [(c.entity, c.score) for c in fused[:limit]], rerank_mode
