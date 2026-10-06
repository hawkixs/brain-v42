"""A pinned feature is never absorbed by the deduplication.

`pinned` is the mark of an explicit operator commitment. The dedup job, for its
part, never read it: `grep -c pinned feature_dedup_job.py` returned 0 while
`_get_all_features` does a `select(features)` that loads the column.

The "oldest absorbs newest" rule (`find_candidates`, comparison on `created_at`)
is therefore applied to commitments like everything else. An explicitly created
pinned feature was eaten on 2026-08-14 at 19:17,
`reranker_score=0.8312975168228149` — and the reranker only compares NAMES, never
the descriptions that carry the scope.

WHAT THE GUARD DOES, AND WHAT IT DOES NOT. It refuses the merge when the SOURCE
is pinned — the source is the one that disappears. It does not swap the roles to
have the pinned one absorb: that would decide the survivor on pinning rather than
on age, and would merge anyway two scopes nothing proves identical. A pinned
target absorbing an unpinned feature stays allowed, that is the nominal case.

A TEST TRAP NOT TO REPRODUCE: the feature rows are `MagicMock`s, so `row.pinned`
is truthy by default if the field is not set. A guard written without thinking of
it would make ALL merges impossible, and the existing suite would go green by no
longer merging anything. The tests below therefore set `pinned` explicitly on both
sides.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from brain_v42.services.feature_dedup_job import FeatureDedupJob
from brain_v42.services.rerank_wire import ShimRerankWire
from brain_v42.services.reranker_client import RerankerClient


@pytest.fixture
async def shim_reranker() -> AsyncIterator[RerankerClient]:
    """Keep pinned-source checks on the real calibrated shim signalling path."""
    reranker = RerankerClient(wire=ShimRerankWire())
    reranker._client = httpx.AsyncClient(
        base_url="http://shim",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"scores": [0.95]})),
    )
    try:
        yield reranker
    finally:
        await reranker.close()


def _row(*, pinned: bool, created_at: float, name: str, similarity: float | None = None):
    row = MagicMock()
    row.id = uuid.uuid4()
    row.name = name
    row.description = f"description de {name}"
    row.embedding = [0.1] * 1536
    row.created_at = created_at
    row.status = "research"
    row.merged_into = None
    row.pinned = pinned
    if similarity is not None:
        row.similarity = similarity
    return row


def _job(all_features, neighbors_by_id, reranker: RerankerClient) -> FeatureDedupJob:
    session = AsyncMock()
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)

    job = FeatureDedupJob(
        session_factory=factory,
        reranker=reranker,
    )
    job._get_all_features = AsyncMock(return_value=all_features)  # type: ignore[method-assign]
    job._find_neighbors = AsyncMock(  # type: ignore[method-assign]
        side_effect=lambda _s, feature, _p: neighbors_by_id.get(feature.id, [])
    )
    return job


@pytest.mark.asyncio
async def test_a_pinned_feature_is_never_proposed_for_absorption(
    shim_reranker: RerankerClient,
) -> None:
    """The case measured on 2026-08-14: the recent one is pinned, the older eats it."""
    ancienne = _row(pinned=False, created_at=1000.0, name="Roadmap curation")
    epinglee = _row(pinned=True, created_at=2000.0, name="Roadmap curation v2", similarity=0.93)

    job = _job([ancienne, epinglee], {ancienne.id: [epinglee]}, shim_reranker)
    candidates = await job.find_candidates("brain-v42")

    assert candidates == [], "une feature épinglée a été proposée à l'absorption"


@pytest.mark.asyncio
async def test_an_unpinned_feature_is_still_absorbed_normally(
    shim_reranker: RerankerClient,
) -> None:
    """The guard must not switch off the deduplication — otherwise it is undetectable."""
    ancienne = _row(pinned=False, created_at=1000.0, name="Roadmap curation")
    recente = _row(pinned=False, created_at=2000.0, name="Roadmap curation v2", similarity=0.93)

    job = _job([ancienne, recente], {ancienne.id: [recente]}, shim_reranker)
    candidates = await job.find_candidates("brain-v42")

    assert len(candidates) == 1
    target, source, _score = candidates[0]
    assert target.id == ancienne.id
    assert source.id == recente.id


@pytest.mark.asyncio
async def test_a_pinned_target_may_still_absorb_an_unpinned_source(
    shim_reranker: RerankerClient,
) -> None:
    """Nominal case: the commitment survives and eats the duplicate, the intended effect."""
    epinglee_ancienne = _row(pinned=True, created_at=1000.0, name="Roadmap curation")
    recente = _row(pinned=False, created_at=2000.0, name="Roadmap curation v2", similarity=0.93)

    job = _job([epinglee_ancienne, recente], {epinglee_ancienne.id: [recente]}, shim_reranker)
    candidates = await job.find_candidates("brain-v42")

    assert len(candidates) == 1
    assert candidates[0][0].id == epinglee_ancienne.id


@pytest.mark.asyncio
async def test_two_pinned_features_are_left_alone(shim_reranker: RerankerClient) -> None:
    """Two commitments: the dedup has no business choosing which one dies."""
    a = _row(pinned=True, created_at=1000.0, name="Roadmap curation")
    b = _row(pinned=True, created_at=2000.0, name="Roadmap curation v2", similarity=0.93)

    job = _job([a, b], {a.id: [b]}, shim_reranker)

    assert await job.find_candidates("brain-v42") == []


@pytest.mark.asyncio
async def test_the_guard_reads_pinned_and_does_not_rely_on_truthiness(
    shim_reranker: RerankerClient,
) -> None:
    """A source whose `pinned` is None or 0 must stay mergeable.

    This test exists because the column is nullable in the database: a naive
    `if row.pinned` would treat `None` as unpinned by luck, but an `is not False`
    would treat it as pinned and would switch off the dedup on every legacy row.
    """
    ancienne = _row(pinned=False, created_at=1000.0, name="Roadmap curation")
    recente = _row(pinned=False, created_at=2000.0, name="Roadmap curation v2", similarity=0.93)
    recente.pinned = None

    job = _job([ancienne, recente], {ancienne.id: [recente]}, shim_reranker)

    assert len(await job.find_candidates("brain-v42")) == 1
