"""One rerank call per multi-type search (rerank round).

Measured 2026-09-23 on production: brain_search fans out one HybridSearcher
shard per knowledge type, and the 20 ms coalescing window of
BatchingRerankerClient did not hold -- slower shards arrived 23-67 ms later,
formed a second batch, and hit the embedding shim's single rerank slot while
the first batch was computing (503 -> rrf_fallback). 36 of 70 fallbacks were
a search colliding with ITSELF, and the degraded shards then carried rank
scores (1.0, (n-1)/n ...) next to cross-encoder sigmoids in the same merged
list, floating to the top regardless of relevance.

A rerank round replaces the time window by a count: every shard of the
fan-out either submits its candidates or withdraws, and ONE reranker call
serves them all, with one mode for all.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from brain_v42.services.brain_service import BrainService
from brain_v42.services.search.hybrid import (
    RERANK_MODE_RERANKED,
    RERANK_MODE_RRF_FALLBACK,
    HybridSearcher,
    rerank_round,
)
from brain_v42.services.search.reranker import HybridReranker


class _RecordingClient:
    """Duck-typed RerankerClient: records calls, scores by a per-text table."""

    def __init__(self, scores: dict[str, float] | None = None, fail: bool = False) -> None:
        self.calls: list[list[str]] = []
        self._scores = scores or {}
        self._fail = fail

    async def rerank(self, query: str, candidates: list[str]) -> list[float]:
        self.calls.append(list(candidates))
        if self._fail:
            raise RuntimeError("503 service_busy")
        return [self._scores.get(text, 0.0) for text in candidates]

    async def is_available(self) -> bool:
        return not self._fail


def _entity(text: str) -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), text=text)


def _shard(texts: list[str], delay: float = 0.0):
    """fts/vector search fns returning `texts` (vector side), after `delay`."""
    entities = [_entity(t) for t in texts]

    async def fts(**_kwargs):
        await asyncio.sleep(delay)
        return []

    async def vec(**_kwargs):
        await asyncio.sleep(delay)
        return [(e, 0.5) for e in entities]

    return fts, vec


async def _search(searcher: HybridSearcher, texts: list[str], delay: float, entity_type: str):
    fts, vec = _shard(texts, delay)
    return await searcher.search(
        query="q",
        fts_search_fn=fts,
        vector_search_fn=vec,
        text_extractor=lambda e: e.text,
        limit=10,
        entity_type=entity_type,
    )


@pytest.mark.asyncio
async def test_staggered_shards_share_one_rerank_call() -> None:
    client = _RecordingClient(scores={"a1": 3.0, "a2": -3.0, "b1": -2.0, "b2": 2.0})
    searcher = HybridSearcher(reranker=HybridReranker(client=client))  # type: ignore[arg-type]

    with rerank_round(expected=2):
        (res_a, mode_a), (res_b, mode_b) = await asyncio.gather(
            _search(searcher, ["a1", "a2"], 0.0, "learning"),
            # 80 ms later: outside the old 20 ms coalescing window
            _search(searcher, ["b1", "b2"], 0.08, "plan"),
        )

    assert len(client.calls) == 1
    assert sorted(client.calls[0]) == ["a1", "a2", "b1", "b2"]
    assert mode_a == mode_b == RERANK_MODE_RERANKED
    assert [e.text for e, _ in res_a] == ["a1", "a2"]
    assert [e.text for e, _ in res_b] == ["b2", "b1"]
    assert res_a[0][1] > 0.9 and res_b[0][1] > 0.8


@pytest.mark.asyncio
async def test_a_failed_rerank_degrades_every_shard_with_per_shard_ranks() -> None:
    client = _RecordingClient(fail=True)
    searcher = HybridSearcher(reranker=HybridReranker(client=client))  # type: ignore[arg-type]

    with rerank_round(expected=2):
        (res_a, mode_a), (res_b, mode_b) = await asyncio.gather(
            _search(searcher, ["a1", "a2"], 0.0, "learning"),
            _search(searcher, ["b1", "b2", "b3"], 0.05, "plan"),
        )

    assert len(client.calls) == 1
    assert mode_a == mode_b == RERANK_MODE_RRF_FALLBACK
    assert [s for _, s in res_a] == [1.0, 0.5]
    assert [round(s, 4) for _, s in res_b] == [1.0, 0.6667, 0.3333]


@pytest.mark.asyncio
async def test_empty_and_failing_shards_withdraw_without_blocking() -> None:
    client = _RecordingClient(scores={"a1": 1.0})
    searcher = HybridSearcher(reranker=HybridReranker(client=client))  # type: ignore[arg-type]

    async def boom(**_kwargs):
        await asyncio.sleep(0.03)
        raise RuntimeError("shard failed")

    async def failing_shard():
        return await searcher.search(
            query="q",
            fts_search_fn=boom,
            vector_search_fn=boom,
            text_extractor=lambda e: e.text,
            limit=10,
            entity_type="adr",
        )

    with rerank_round(expected=3):
        results = await asyncio.wait_for(
            asyncio.gather(
                _search(searcher, ["a1"], 0.0, "learning"),
                _search(searcher, [], 0.02, "snippet"),
                failing_shard(),
                return_exceptions=True,
            ),
            timeout=1.0,
        )

    (res_a, mode_a), (res_empty, mode_empty), failure = results
    assert isinstance(failure, RuntimeError)
    assert len(client.calls) == 1
    assert mode_a == RERANK_MODE_RERANKED
    assert res_empty == [] and mode_empty == RERANK_MODE_RERANKED


@pytest.mark.asyncio
async def test_a_missing_shard_cannot_hold_the_round_forever() -> None:
    client = _RecordingClient(scores={"a1": 1.0})
    searcher = HybridSearcher(reranker=HybridReranker(client=client))  # type: ignore[arg-type]

    with rerank_round(expected=2, straggler_timeout=0.05):
        res_a, mode_a = await asyncio.wait_for(
            _search(searcher, ["a1"], 0.0, "learning"), timeout=1.0
        )

    assert len(client.calls) == 1
    assert mode_a == RERANK_MODE_RERANKED
    assert [e.text for e, _ in res_a] == ["a1"]


@pytest.mark.asyncio
async def test_without_a_round_each_shard_reranks_alone() -> None:
    client = _RecordingClient(scores={"a1": 1.0, "b1": 1.0})
    searcher = HybridSearcher(reranker=HybridReranker(client=client))  # type: ignore[arg-type]

    await asyncio.gather(
        _search(searcher, ["a1"], 0.0, "learning"),
        _search(searcher, ["b1"], 0.0, "plan"),
    )

    assert len(client.calls) == 2


def _svc(texts: list[str], delay: float) -> MagicMock:
    fts, vec = _shard(texts, delay)
    svc = MagicMock()
    svc.search = AsyncMock(side_effect=fts)
    svc.semantic_search = AsyncMock(side_effect=vec)
    return svc


@pytest.mark.asyncio
async def test_brain_search_fan_out_makes_one_rerank_call() -> None:
    client = _RecordingClient(scores={"d": 2.0, "l": 1.0, "s": 0.5})
    embedding_svc = MagicMock()
    embedding_svc.embed_query = AsyncMock(return_value=[0.0] * 4)
    brain = BrainService(
        decision_svc=_svc(["d"], 0.0),
        learning_svc=_svc(["l"], 0.04),
        snippet_svc=_svc(["s"], 0.09),
        runbook_svc=_svc([], 0.0),
        adr_svc=_svc([], 0.0),
        embedding_svc=embedding_svc,
        hybrid_searcher=HybridSearcher(reranker=HybridReranker(client=client)),  # type: ignore[arg-type]
    )

    # the extractors read the real entity fields; give each fake entity all of them
    import brain_v42.services.brain_service as bs

    original = dict(bs._TEXT_EXTRACTORS)
    bs._TEXT_EXTRACTORS.update({k: (lambda e: e.text) for k in original})
    try:
        results, degraded, mode, _kinds = await brain._fan_out(
            ["decision", "learning", "snippet", "runbook", "adr"], "q", None, 10
        )
    finally:
        bs._TEXT_EXTRACTORS.clear()
        bs._TEXT_EXTRACTORS.update(original)

    assert len(client.calls) == 1
    assert sorted(client.calls[0]) == ["d", "l", "s"]
    assert degraded is None
    assert mode == RERANK_MODE_RERANKED
