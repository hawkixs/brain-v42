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

# W33 "a rank is not a score" — score_kind names the PROVENANCE of
# RankedCandidate.score, not just its value. A rank ordinal rescaled into
# (0, 1] (rrf_fallback) is numerically indistinguishable from a calibrated
# cross-encoder sigmoid unless something tags it. Exactly one of the two real
# producers below (reranker.py) must overwrite the sentinel default.
SCORE_KIND_CROSS_ENCODER = "cross_encoder"
SCORE_KIND_RANK = "rank"
SCORE_KIND_FTS_RANK = "fts_rank"

# Sentinel default for RankedCandidate.score_kind — deliberately NOT one of
# the three real ScoreKind values above. A producer that forgets to
# overwrite it must fail loudly the moment the value reaches
# SearchResult.score_kind (a required Literal field): pydantic rejects this
# sentinel at construction instead of silently rendering a rank ordinal as a
# plausible-looking score.
SCORE_KIND_UNSET = "score_kind_unset"


@dataclass
class RankedCandidate:
    """Internal candidate for RRF fusion and reranking."""

    id: UUID
    entity: Any
    entity_type: str
    score: float
    text: str
    # See SCORE_KIND_UNSET docstring above — overwritten by reranker.py's two
    # producers (cross_encoder / rank). Never read as a plausible default.
    score_kind: str = SCORE_KIND_UNSET


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
        entity_type: str = "",
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
            entity_type: The KnowledgeType this shard searches (e.g. "decision").
                Threaded onto every RankedCandidate so a degraded reranker can
                name which shard fell back — W33 incident (2026-09-07): the
                log carried n_candidates but not WHICH shard produced them.

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
                entity_type=entity_type,
                score=0.0,
                text=text_extractor(e),
            )
            for e in fts_raw
        ]
        vec_candidates = [
            RankedCandidate(
                id=e.id,
                entity=e,
                entity_type=entity_type,
                score=s,
                text=text_extractor(e),
            )
            for e, s in vec_raw
        ]

        fused = rrf_fuse(fts_candidates, vec_candidates, k=60)[:20]

        if self._reranker:
            if fused:
                rerank_mode, fused = await self._reranker.rerank_with_mode(query, fused)
            else:
                # An empty shard must not report RRF_ONLY: that mode means "no
                # reranker configured" (a property of this instance), not
                # "this particular shard had zero candidates". Reporting
                # RRF_ONLY for a merely-empty shard flipped an otherwise
                # healthy multi-type query into "degraded" whenever that
                # shard happened to be observed first (lot F4;
                # brain_service._fan_out picks observed_rerank_modes[0]).
                #
                # rerank_with_mode() already returns RERANK_MODE_RERANKED for
                # `[]` (nothing needed reranking, vacuously true) — we
                # short-circuit to that SAME value here instead of
                # delegating, so an empty shard costs zero reranker
                # round-trips. Delegating unconditionally (the original F4
                # fix) round-trips every empty shard through
                # InstrumentedReranker, which records a reranker_call for
                # it — inflating total_calls and diluting avg_latency_ms for
                # calls that never touched the reranker (measured: 6 empty
                # shards -> 6 record_reranker_call(0.0, 0) on the delegating
                # version, 0 on this one).
                rerank_mode = RERANK_MODE_RERANKED
        else:
            rerank_mode = RERANK_MODE_RRF_ONLY

        return [(c.entity, c.score) for c in fused[:limit]], rerank_mode
