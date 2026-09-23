"""Hybrid search: FTS + vector + RRF fusion + optional reranking.

HybridSearcher.search() now returns a 2-tuple (results, rerank_mode) so
callers can surface degradation to the LLM.  Modes:
  - "reranked"     — cross-encoder applied
  - "rrf_fallback" — reranker unavailable; rank-based scores
  - "rrf_only"     — no reranker configured; RRF scores as-is
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
from collections.abc import Callable, Coroutine, Iterator
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import structlog

logger = structlog.get_logger(__name__)

# Exposed mode constants so callers can compare without hard-coding strings.
RERANK_MODE_RERANKED = "reranked"
RERANK_MODE_RRF_FALLBACK = "rrf_fallback"
RERANK_MODE_RRF_ONLY = "rrf_only"

# W33 "a rank is not a score" — these name the PROVENANCE of a score, not
# just its value. A rank ordinal rescaled into (0, 1] (rrf_fallback) is
# numerically indistinguishable from a calibrated cross-encoder sigmoid
# unless something tags it. The decision of which value applies is made in
# exactly one place — brain_service._fan_out's rerank_mode -> score_kind
# mapping — and consumed with NO plausible default at SearchResult
# construction: a type missing from that mapping raises KeyError rather than
# silently rendering a rank ordinal as a calibrated score. RankedCandidate
# itself carries no score_kind field: an earlier version duplicated the
# provenance there (set by reranker.py, never read by anything), which gave
# the impression of two independent guarantees when only the _fan_out
# mapping was actually enforced.
SCORE_KIND_CROSS_ENCODER = "cross_encoder"
SCORE_KIND_RANK = "rank"
SCORE_KIND_FTS_RANK = "fts_rank"


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


# ── Rerank round: one reranker call per multi-shard search ─────────────────
#
# brain_search runs one HybridSearcher.search() per knowledge type. Each used
# to rerank on its own, and BatchingRerankerClient's 20 ms window was the only
# thing merging them. Measured 2026-09-23: slower shards arrive 23-67 ms after
# the first, form a second batch, and hit the embedding shim's SINGLE rerank
# slot while the first batch is still computing -> 503 -> rrf_fallback for
# those shards only. That was 36 of 70 fallbacks, and the partial degradation
# put rank ordinals (1.0, (n-1)/n ...) beside cross-encoder sigmoids in one
# merged list, where they outranked everything.
#
# A round replaces the time window by a count. The fan-out declares how many
# shards it launched; each shard either submits its fused candidates or
# withdraws (empty, failed, cancelled). When every shard has settled, ONE
# rerank_with_mode() call scores the concatenation and every shard receives
# the SAME mode. A straggler timeout bounds the wait if a shard never
# settles; a shard arriving after the flush reranks alone.

_CURRENT_ROUND: contextvars.ContextVar[_RerankRound | None] = contextvars.ContextVar(
    "brain_v42_rerank_round", default=None
)

DEFAULT_STRAGGLER_TIMEOUT_SECONDS = 2.0


class _RerankRound:
    """Shared rerank state for the shards of one fan-out (asyncio-only)."""

    def __init__(self, expected: int, straggler_timeout: float) -> None:
        self._expected = expected
        self._straggler_timeout = straggler_timeout
        self._settled = 0
        self._closed = False
        self._query: str | None = None
        self._reranker: Any = None
        self._pending: list[
            tuple[list[RankedCandidate], asyncio.Future[tuple[str, list[RankedCandidate]]]]
        ] = []
        self._timer: asyncio.TimerHandle | None = None
        self._flush_task: asyncio.Task[None] | None = None

    def ticket(self) -> _ShardTicket:
        """One per shard; it settles the shard exactly once."""
        return _ShardTicket(self)

    def _withdraw(self) -> None:
        if self._closed:
            return
        self._settled += 1
        self._maybe_flush()

    async def _submit(
        self, reranker: Any, query: str, candidates: list[RankedCandidate]
    ) -> tuple[str, list[RankedCandidate]]:
        joinable = not self._closed and (
            self._reranker is None or (reranker is self._reranker and query == self._query)
        )
        if not joinable:
            mode, reranked = await reranker.rerank_with_mode(query, candidates)
            return str(mode), reranked
        loop = asyncio.get_running_loop()
        future: asyncio.Future[tuple[str, list[RankedCandidate]]] = loop.create_future()
        self._query = query
        self._reranker = reranker
        self._pending.append((candidates, future))
        self._settled += 1
        if self._timer is None:
            self._timer = loop.call_later(self._straggler_timeout, self._start_flush)
        self._maybe_flush()
        return await future

    def _maybe_flush(self) -> None:
        if self._settled >= self._expected:
            self._start_flush()

    def _start_flush(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._timer is not None:
            self._timer.cancel()
        if self._settled < self._expected:
            logger.warning(
                "hybrid_searcher.rerank_round_straggler_timeout",
                expected=self._expected,
                settled=self._settled,
            )
        if self._pending:
            self._flush_task = asyncio.get_running_loop().create_task(
                self._flush(), name="hybrid_searcher.rerank_round"
            )

    async def _flush(self) -> None:
        pending = self._pending
        combined = [c for candidates, _ in pending for c in candidates]
        try:
            mode, _ = await self._reranker.rerank_with_mode(self._query, combined)
        except BaseException as exc:
            for _, future in pending:
                if not future.done():
                    future.set_exception(exc)
            if isinstance(exc, asyncio.CancelledError):
                raise
            return
        for candidates, future in pending:
            if mode == RERANK_MODE_RRF_FALLBACK:
                # Rank-rescale PER SHARD, in its own RRF order: the reranker
                # rescaled the concatenation, which would give the second
                # shard's best candidate a score below the first shard's worst.
                n = len(candidates)
                for rank, candidate in enumerate(candidates):
                    candidate.score = (n - rank) / n
                result = list(candidates)
            else:
                result = sorted(candidates, key=lambda c: c.score, reverse=True)
            if not future.done():
                future.set_result((str(mode), result))


class _ShardTicket:
    """A shard's single settlement in a round: submit OR withdraw, once."""

    def __init__(self, round_: _RerankRound) -> None:
        self._round = round_
        self._settled = False

    async def submit(
        self, reranker: Any, query: str, candidates: list[RankedCandidate]
    ) -> tuple[str, list[RankedCandidate]]:
        """Wait for the round and return this shard's (mode, reranked candidates)."""
        if self._settled:
            mode, reranked = await reranker.rerank_with_mode(query, candidates)
            return str(mode), reranked
        self._settled = True
        return await self._round._submit(reranker, query, candidates)

    def withdraw(self) -> None:
        """Settle without candidates (empty, failed, cancelled); no-op once settled."""
        if self._settled:
            return
        self._settled = True
        self._round._withdraw()


@contextlib.contextmanager
def rerank_round(
    expected: int, straggler_timeout: float = DEFAULT_STRAGGLER_TIMEOUT_SECONDS
) -> Iterator[None]:
    """Make the next ``expected`` HybridSearcher.search() calls share one rerank.

    Tasks must be CREATED inside the block: asyncio copies the context at task
    creation, which is how each shard finds the round.
    """
    token = _CURRENT_ROUND.set(_RerankRound(expected, straggler_timeout))
    try:
        yield
    finally:
        _CURRENT_ROUND.reset(token)


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
        round_ = _CURRENT_ROUND.get()
        ticket = round_.ticket() if round_ is not None else None
        try:
            return await self._search_shard(
                query,
                fts_search_fn,
                vector_search_fn,
                text_extractor,
                limit,
                project_key,
                embedding,
                project_keys,
                entity_type,
                ticket,
            )
        finally:
            # Every exit that did not submit -- empty shard, no reranker, a
            # raised search fn, a cancellation -- must settle the round, or
            # the other shards would wait for the straggler timeout. A ticket
            # settles once: withdrawing after a submit is a no-op.
            if ticket is not None:
                ticket.withdraw()

    async def _search_shard(
        self,
        query: str,
        fts_search_fn: Callable[..., Coroutine[Any, Any, Any]],
        vector_search_fn: Callable[..., Coroutine[Any, Any, Any]],
        text_extractor: Callable[[Any], str],
        limit: int,
        project_key: str | None,
        embedding: list[float] | None,
        project_keys: list[str] | None,
        entity_type: str,
        ticket: _ShardTicket | None,
    ) -> tuple[list[tuple[Any, float]], str]:
        """Body of search(); reranks through the round when a ticket is given."""
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
            if fused and ticket is not None:
                rerank_mode, fused = await ticket.submit(self._reranker, query, fused)
            elif fused:
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
