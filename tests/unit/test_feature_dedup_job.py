"""Unit tests for FeatureDedupJob — probable-duplicate detection (read-only).

The job only finds candidate pairs for signalling; it never merges (ruling
9e21964f, extension of d4648d84).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from structlog.testing import capture_logs

from brain_v42.services.feature_dedup_job import FeatureDedupJob
from brain_v42.services.rerank_wire import CohereRerankWire, ShimRerankWire
from brain_v42.services.reranker_client import RerankerClient

# ── helpers ────────────────────────────────────────────────────────────


def _make_feature_row(
    *,
    feature_id: uuid.UUID | None = None,
    name: str = "Test Feature",
    description: str = "A test feature",
    embedding: list[float] | None = None,
    created_at: float = 1000.0,
    similarity: float | None = None,
    status: str = "research",
    merged_into: uuid.UUID | None = None,
    pinned: bool = False,
) -> MagicMock:
    """Create a mock DB row resembling a features table row.

    `pinned` MUST be set explicitly, never left to the MagicMock's default
    attribute: that one is truthy, and the guard added to `find_candidates` would
    then refuse every merge. The suite would go green by no longer deduplicating
    anything — a false green nothing would catch.
    """
    row = MagicMock()
    row.id = feature_id or uuid.uuid4()
    row.name = name
    row.description = description
    row.embedding = embedding or [0.1] * 1536
    row.created_at = created_at
    row.status = status
    row.merged_into = merged_into
    row.pinned = pinned
    if similarity is not None:
        row.similarity = similarity
    return row


@pytest.fixture
async def mock_deps() -> AsyncIterator[dict[str, Any]]:
    """Use the real shim wire so the tests exercise calibrated signalling."""
    session = AsyncMock()
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)

    scores = [0.85]
    reranker = RerankerClient(wire=ShimRerankWire())
    reranker._client = httpx.AsyncClient(
        base_url="http://shim",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"scores": scores})),
    )
    reranker.rerank = AsyncMock(wraps=reranker.rerank)

    try:
        yield {
            "session_factory": factory,
            "session": session,
            "reranker": reranker,
            "scores": scores,
        }
    finally:
        await reranker.close()


def _build_job(deps: dict) -> FeatureDedupJob:
    return FeatureDedupJob(
        session_factory=deps["session_factory"],
        reranker=deps["reranker"],
    )


# ── find_candidates tests ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_voyage_skips_before_session_and_transport() -> None:
    transport = MagicMock(return_value=httpx.Response(200, json={"results": []}))
    reranker = RerankerClient(wire=CohereRerankWire("voyageai/rerank-3-lite"))
    reranker._client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    factory = MagicMock(side_effect=AssertionError("Uncalibrated reranker opened a DB session"))
    job = FeatureDedupJob(factory, reranker)
    try:
        with capture_logs() as records:
            assert await job.find_candidates("brain-v42") == []
    finally:
        await reranker.close()
    factory.assert_not_called()
    factory.return_value.__aenter__.assert_not_called()
    transport.assert_not_called()
    assert records == [
        {
            "event": "feature_dedup.reranker_uncalibrated",
            "identity": "cohere:voyageai/rerank-3-lite",
            "log_level": "warning",
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_calibration", [False, True])
async def test_uncalibrated_mock_skips_without_reranking(missing_calibration: bool) -> None:
    reranker = MagicMock()
    reranker.rerank = AsyncMock()
    if missing_calibration:
        del reranker.calibration
    factory = MagicMock(side_effect=AssertionError("Uncalibrated reranker opened a DB session"))
    job = FeatureDedupJob(factory, reranker)
    with capture_logs() as records:
        assert await job.find_candidates("brain-v42") == []
    factory.assert_not_called()
    reranker.rerank.assert_not_awaited()
    assert len(records) == 1
    assert records[0]["event"] == "feature_dedup.reranker_uncalibrated"
    assert records[0]["log_level"] == "warning"


@pytest.mark.asyncio
@pytest.mark.parametrize(("score", "signals"), [(0.81, True), (0.80, True), (0.79, False)])
async def test_shim_signal_threshold_is_unchanged(
    mock_deps: dict[str, Any], score: float, signals: bool
) -> None:
    older = _make_feature_row(name="Feature A", created_at=100.0)
    newer = _make_feature_row(name="Feature B", created_at=200.0, similarity=0.75)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"scores": [score]})

    reranker = RerankerClient(wire=ShimRerankWire())
    reranker._client = httpx.AsyncClient(
        base_url="http://shim", transport=httpx.MockTransport(handler)
    )
    job = FeatureDedupJob(mock_deps["session_factory"], reranker)
    job._get_all_features = AsyncMock(return_value=[older, newer])
    job._find_neighbors = AsyncMock(side_effect=[[newer], []])
    try:
        candidates = await job.find_candidates("brain-v42")
    finally:
        await reranker.close()
    assert candidates == ([(older, newer, score)] if signals else [])
    assert len(requests) == 1
    assert requests[0].url.path == "/rerank"


@pytest.mark.asyncio
async def test_find_candidates_empty_when_no_features(mock_deps):
    """No features -> empty candidate list."""
    # Session execute returns no features
    result_set = MagicMock()
    result_set.fetchall.return_value = []
    mock_deps["session"].execute = AsyncMock(return_value=result_set)

    job = _build_job(mock_deps)
    candidates = await job.find_candidates("brain_v42")

    assert candidates == []


@pytest.mark.asyncio
async def test_find_candidates_empty_when_single_feature(mock_deps):
    """Single feature -> no pairs possible -> empty list."""
    feature = _make_feature_row(name="Only Feature")
    result_set = MagicMock()
    result_set.fetchall.return_value = [feature]

    # Neighbor query returns no neighbors
    neighbor_result = MagicMock()
    neighbor_result.fetchall.return_value = []

    mock_deps["session"].execute = AsyncMock(side_effect=[result_set, neighbor_result])

    job = _build_job(mock_deps)
    candidates = await job.find_candidates("brain_v42")

    assert candidates == []


@pytest.mark.asyncio
async def test_find_candidates_returns_high_score_pairs(mock_deps):
    """Two similar features with reranker score >= 0.80 -> candidate pair."""
    older_id = uuid.uuid4()
    newer_id = uuid.uuid4()

    older = _make_feature_row(feature_id=older_id, name="Memory Decay", created_at=100.0)
    newer = _make_feature_row(
        feature_id=newer_id,
        name="Decay System",
        created_at=200.0,
        similarity=0.75,
    )

    # First execute: get all features
    all_features_result = MagicMock()
    all_features_result.fetchall.return_value = [older, newer]

    # Second execute: neighbors of older (finds newer)
    neighbor_result_1 = MagicMock()
    neighbor_result_1.fetchall.return_value = [newer]

    # Third execute: neighbors of newer (finds older, but pair already seen)
    older_as_neighbor = _make_feature_row(
        feature_id=older_id, name="Memory Decay", created_at=100.0, similarity=0.75
    )
    neighbor_result_2 = MagicMock()
    neighbor_result_2.fetchall.return_value = [older_as_neighbor]

    mock_deps["session"].execute = AsyncMock(
        side_effect=[all_features_result, neighbor_result_1, neighbor_result_2]
    )

    # Reranker gives high score (only called once for deduplicated pair)
    mock_deps["scores"][:] = [0.90]

    job = _build_job(mock_deps)
    candidates = await job.find_candidates("brain_v42")

    assert len(candidates) == 1
    target, source, score = candidates[0]
    # Oldest absorbs newest
    assert target.id == older_id
    assert source.id == newer_id
    assert score == 0.90


@pytest.mark.asyncio
async def test_find_candidates_skips_low_reranker_score(mock_deps):
    """Reranker score < 0.80 -> not a candidate."""
    older = _make_feature_row(name="Feature A", created_at=100.0)
    newer = _make_feature_row(name="Feature B", created_at=200.0, similarity=0.55)

    all_features_result = MagicMock()
    all_features_result.fetchall.return_value = [older, newer]

    neighbor_result_1 = MagicMock()
    neighbor_result_1.fetchall.return_value = [newer]

    neighbor_result_2 = MagicMock()
    neighbor_result_2.fetchall.return_value = [
        _make_feature_row(feature_id=older.id, name="Feature A", created_at=100.0, similarity=0.55)
    ]

    mock_deps["session"].execute = AsyncMock(
        side_effect=[all_features_result, neighbor_result_1, neighbor_result_2]
    )

    # Reranker returns low score
    mock_deps["scores"][:] = [0.45]

    job = _build_job(mock_deps)
    candidates = await job.find_candidates("brain_v42")

    assert candidates == []


@pytest.mark.asyncio
async def test_find_candidates_uses_cosine_prefilter(mock_deps):
    """Only features with cosine >= 0.50 are sent to reranker."""
    older = _make_feature_row(name="Feature A", created_at=100.0)

    all_features_result = MagicMock()
    all_features_result.fetchall.return_value = [older]

    # No neighbors above threshold
    neighbor_result = MagicMock()
    neighbor_result.fetchall.return_value = []

    mock_deps["session"].execute = AsyncMock(side_effect=[all_features_result, neighbor_result])

    job = _build_job(mock_deps)
    candidates = await job.find_candidates("brain_v42")

    assert candidates == []
    # Reranker should NOT be called since no cosine pre-filter matches
    mock_deps["reranker"].rerank.assert_not_called()


@pytest.mark.asyncio
async def test_feature_queries_exclude_archived_and_already_merged_rows(mock_deps):
    """Both sides of automatic dedup must be live merge roots.

    Archived rows retain embeddings in production.  Without these predicates an
    archived target can be paired with its own survivor and the merge path can
    create a ``merged_into`` self-loop.
    """
    empty = MagicMock()
    empty.fetchall.return_value = []
    mock_deps["session"].execute = AsyncMock(return_value=empty)

    job = _build_job(mock_deps)
    feature = _make_feature_row()
    await job._get_all_features(mock_deps["session"], "brain_v42")
    await job._find_neighbors(mock_deps["session"], feature, "brain_v42")

    statements = [str(call.args[0]).lower() for call in mock_deps["session"].execute.call_args_list]
    assert len(statements) == 2
    for statement in statements:
        assert "features.status !=" in statement
        assert "features.merged_into is null" in statement
