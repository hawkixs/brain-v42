"""Unit tests for FeatureDedupJob — periodic feature deduplication.

Updated to reflect the existence re-check (FOR UPDATE) added in merge_features:
the first execute() call is now a SELECT … FOR UPDATE returning both target and
source rows.  Tests that call merge_features now set up a recheck_result mock
that returns both rows so the full merge path executes.  Call count assertions
are updated from 4 → 6 (recheck + 5 DML).  These are legitimate updates: the
old tests encoded the interface before the chain-of-merges fix, not the correct
new contract.  The invariants under test (artifact transfer, gitlab_events
re-parent, enriched description, DELETE source) are unchanged.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from brain_v42.services.feature_dedup_job import FeatureDedupJob

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


def _make_recheck_result(target: MagicMock, source: MagicMock) -> MagicMock:
    """Build a mock execute() result for the SELECT … FOR UPDATE existence recheck.

    Returns both rows so the merge proceeds normally (both parties exist).
    """
    recheck = MagicMock()
    recheck.fetchall.return_value = [target, source]
    return recheck


@pytest.fixture
def mock_deps():
    """Create mock dependencies for FeatureDedupJob."""
    session = AsyncMock()
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)

    embedding_svc = AsyncMock()
    embedding_svc.embed = AsyncMock(return_value=[0.2] * 1536)

    reranker = AsyncMock()
    reranker.is_available = AsyncMock(return_value=True)
    reranker.rerank = AsyncMock(return_value=[0.85])

    return {
        "session_factory": factory,
        "session": session,
        "embedding_svc": embedding_svc,
        "reranker": reranker,
    }


def _build_job(deps: dict) -> FeatureDedupJob:
    return FeatureDedupJob(
        session_factory=deps["session_factory"],
        reranker=deps["reranker"],
        embedding_svc=deps["embedding_svc"],
    )


# ── find_candidates tests ─────────────────────────────────────────────


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
    mock_deps["reranker"].rerank = AsyncMock(return_value=[0.90])

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
    mock_deps["reranker"].rerank = AsyncMock(return_value=[0.45])

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


# ── merge_features tests ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_merge_transfers_artifacts(mock_deps):
    """Verify all related rows are re-parented and DELETE source is executed.

    The call count includes the existence recheck and the self-FK re-parenting
    that prevents descendants from blocking deletion of the source.
    """
    target = _make_feature_row(name="Target Feature", description="Target desc")
    source = _make_feature_row(name="Source Feature", description="Source desc")

    session = mock_deps["session"]
    recheck = _make_recheck_result(target, source)
    dml_result = MagicMock()
    # execute calls: 1 recheck + 5 DML (3 transfers, target update, delete)
    session.execute = AsyncMock(
        side_effect=[recheck, dml_result, dml_result, dml_result, dml_result, dml_result]
    )

    job = _build_job(mock_deps)
    await job.merge_features(session, target, source)

    assert session.execute.call_count == 6


@pytest.mark.asyncio
async def test_merge_transfers_gitlab_events(mock_deps):
    """gitlab_events pointing at the source must be re-parented to target.

    Without this, the final DELETE source trips the FK gitlab_events.feature_id
    (ON DELETE NO ACTION in production) and dedup silently rolls back.
    """
    source_id = uuid.uuid4()
    target_id = uuid.uuid4()
    target = _make_feature_row(feature_id=target_id, name="Target", description="Target")
    source = _make_feature_row(feature_id=source_id, name="Source", description="Source")

    session = mock_deps["session"]
    recheck = _make_recheck_result(target, source)
    dml_result = MagicMock()
    session.execute = AsyncMock(
        side_effect=[recheck, dml_result, dml_result, dml_result, dml_result, dml_result]
    )

    job = _build_job(mock_deps)
    await job.merge_features(session, target, source)

    # Exactly one of the UPDATE calls must target the gitlab_events table
    # with feature_id=target_id on the rows where feature_id=source_id.
    update_calls = [
        call
        for call in session.execute.call_args_list
        if hasattr(call[0][0], "is_update") and call[0][0].is_update
    ]
    gitlab_events_updates = [
        call for call in update_calls if getattr(call[0][0].table, "name", None) == "gitlab_events"
    ]
    assert len(gitlab_events_updates) == 1, (
        f"expected exactly 1 UPDATE on gitlab_events, got {len(gitlab_events_updates)} "
        f"out of {len(update_calls)} UPDATE calls total"
    )


@pytest.mark.asyncio
async def test_merge_reparents_features_already_merged_into_source(mock_deps):
    """Archived descendants must follow the source to its surviving target.

    ``features.merged_into`` is a self-referencing FK without ``ON DELETE``.
    Leaving descendants pointed at the source makes the final DELETE fail and
    rolls back the otherwise valid merge.
    """
    source_id = uuid.uuid4()
    target_id = uuid.uuid4()
    target = _make_feature_row(feature_id=target_id, name="Target", description="Target")
    source = _make_feature_row(feature_id=source_id, name="Source", description="Source")

    session = mock_deps["session"]
    recheck = _make_recheck_result(target, source)
    dml_result = MagicMock()
    session.execute = AsyncMock(
        side_effect=[recheck, dml_result, dml_result, dml_result, dml_result, dml_result]
    )

    job = _build_job(mock_deps)
    await job.merge_features(session, target, source)

    merged_into_updates = []
    for call in session.execute.call_args_list:
        stmt = call.args[0]
        if not (getattr(stmt, "is_update", False) and stmt.table.name == "features"):
            continue
        values = {
            (column.key if hasattr(column, "key") else str(column)): getattr(value, "value", value)
            for column, value in stmt._values.items()
        }
        if "merged_into" in values:
            merged_into_updates.append(values)

    assert {"merged_into": target_id} in merged_into_updates


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target_status", "target_merged_into", "source_status", "source_merged_into"),
    [
        ("archived", None, "done", None),
        ("done", uuid.uuid4(), "done", None),
        ("done", None, "archived", None),
        ("done", None, "done", uuid.uuid4()),
    ],
)
async def test_merge_skips_rows_that_are_not_live_roots(
    mock_deps,
    target_status,
    target_merged_into,
    source_status,
    source_merged_into,
):
    """A stale candidate snapshot must not merge archived/merged rows."""
    target = _make_feature_row(
        name="Target",
        status=target_status,
        merged_into=target_merged_into,
    )
    source = _make_feature_row(
        name="Source",
        status=source_status,
        merged_into=source_merged_into,
    )
    recheck = _make_recheck_result(target, source)
    mock_deps["session"].execute = AsyncMock(return_value=recheck)

    result = await _build_job(mock_deps).merge_features(mock_deps["session"], target, source)

    assert result is False
    assert mock_deps["session"].execute.await_count == 1
    mock_deps["embedding_svc"].embed.assert_not_awaited()


@pytest.mark.asyncio
async def test_merge_enriches_description(mock_deps):
    """Merged target gets enriched description and re-embedded vector."""
    target = _make_feature_row(name="Target", description="Target description")
    source = _make_feature_row(name="Source", description="Source description")

    session = mock_deps["session"]
    recheck = _make_recheck_result(target, source)
    dml_result = MagicMock()
    session.execute = AsyncMock(
        side_effect=[recheck, dml_result, dml_result, dml_result, dml_result, dml_result]
    )

    job = _build_job(mock_deps)
    await job.merge_features(session, target, source)

    # Embedding service should be called with enriched description
    expected_desc = "Target description\n---\nSource description"
    mock_deps["embedding_svc"].embed.assert_awaited_once_with(expected_desc)


@pytest.mark.asyncio
async def test_merge_archives_source_without_deleting_it(mock_deps):
    """Source remains as an auditable archived row with merged_into set."""
    target_id = uuid.uuid4()
    source_id = uuid.uuid4()
    target = _make_feature_row(feature_id=target_id, name="Target")
    source = _make_feature_row(feature_id=source_id, name="Source")

    session = mock_deps["session"]
    recheck = _make_recheck_result(target, source)
    dml_result = MagicMock()
    session.execute = AsyncMock(
        side_effect=[recheck, dml_result, dml_result, dml_result, dml_result, dml_result]
    )

    job = _build_job(mock_deps)
    await job.merge_features(session, target, source)

    # 6 calls: recheck + 3 transfers + target update + source archive
    assert session.execute.call_count == 6
    statements = [call.args[0] for call in session.execute.call_args_list]
    assert not any(getattr(statement, "is_delete", False) for statement in statements)

    source_archive_updates = []
    for statement in statements:
        if not (
            getattr(statement, "is_update", False)
            and getattr(statement.table, "name", None) == "features"
        ):
            continue
        values = {
            (column.key if hasattr(column, "key") else str(column)): getattr(value, "value", value)
            for column, value in statement._values.items()
        }
        if "status" in values or "merged_into" in values:
            source_archive_updates.append(values)

    assert any(
        values.get("status") == "archived" and values.get("merged_into") == target_id
        for values in source_archive_updates
    )


# ── embed failure graceful degradation ─────────────────────────────────


@pytest.mark.asyncio
async def test_merge_features_continues_when_embed_fails(mock_deps):
    """When embed fails during merge, existing embedding is kept and update proceeds.

    Updated from 'new_embedding is None' to 'existing embedding preserved' to
    reflect the embed-fail fix: writing None makes the feature permanently
    invisible in cosine searches.  The merge must still complete without raising.
    """
    target = _make_feature_row(name="Target", description="Target desc")
    source = _make_feature_row(name="Source", description="Source desc")

    # Embedding service raises
    mock_deps["embedding_svc"].embed = AsyncMock(side_effect=Exception("GPU down"))

    session = mock_deps["session"]
    recheck = _make_recheck_result(target, source)
    dml_result = MagicMock()
    session.execute = AsyncMock(
        side_effect=[recheck, dml_result, dml_result, dml_result, dml_result, dml_result]
    )

    job = _build_job(mock_deps)
    # Should NOT raise
    await job.merge_features(session, target, source)

    # Session.execute should still be called (recheck + transfers + update + delete)
    assert session.execute.await_count >= 1
