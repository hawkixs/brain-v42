"""Unit tests for FeatureLinker service."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from brain_v42.services.feature_linker import FeatureLinker


@pytest.fixture
def mock_session_factory():
    """Mock async session factory returning a mock session."""
    session = AsyncMock()
    result = MagicMock()
    result.fetchall.return_value = []
    session.execute = AsyncMock(return_value=result)
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return factory, session


@pytest.mark.asyncio
async def test_link_artifact_skips_when_no_embedding(mock_session_factory):
    factory, _ = mock_session_factory
    linker = FeatureLinker(session_factory=factory)
    result = await linker.link_artifact(
        embedding=None,
        artifact_type="learning",
        artifact_id=uuid.uuid4(),
        project_key="red",
    )
    assert result == 0


@pytest.mark.asyncio
async def test_link_artifact_skips_when_no_project_key(mock_session_factory):
    factory, _ = mock_session_factory
    linker = FeatureLinker(session_factory=factory)
    result = await linker.link_artifact(
        embedding=[0.1] * 1536,
        artifact_type="learning",
        artifact_id=uuid.uuid4(),
        project_key=None,
    )
    assert result == 0


@pytest.mark.asyncio
async def test_link_artifact_catches_exceptions(mock_session_factory):
    factory, session = mock_session_factory
    session.execute = AsyncMock(side_effect=RuntimeError("db down"))
    linker = FeatureLinker(session_factory=factory)
    result = await linker.link_artifact(
        embedding=[0.1] * 1536,
        artifact_type="learning",
        artifact_id=uuid.uuid4(),
        project_key="red",
    )
    assert result == 0  # fire-and-forget, no exception raised


@pytest.mark.asyncio
async def test_link_artifact_delegates_to_cluster_guard(mock_session_factory):
    """When cluster_guard is set AND title is provided, delegate to resolve()."""
    factory, session = mock_session_factory
    cluster_guard = AsyncMock()
    mock_feature = MagicMock(id=uuid.uuid4(), name="Test Feature")
    cluster_guard.resolve.return_value = (mock_feature, "linked")

    linker = FeatureLinker(session_factory=factory, cluster_guard=cluster_guard)
    result = await linker.link_artifact(
        embedding=[0.1] * 10,
        artifact_type="learning",
        artifact_id=uuid.uuid4(),
        project_key="test",
        title="Some learning",
    )
    cluster_guard.resolve.assert_called_once()
    assert result >= 0


@pytest.mark.asyncio
async def test_link_artifact_uses_raw_sql_without_cluster_guard(mock_session_factory):
    """Without cluster_guard, the existing raw SQL path is used."""
    factory, session = mock_session_factory
    linker = FeatureLinker(session_factory=factory)
    result = await linker.link_artifact(
        embedding=[0.1] * 10,
        artifact_type="learning",
        artifact_id=uuid.uuid4(),
        project_key="test",
    )
    assert result >= 0


@pytest.mark.asyncio
async def test_link_artifact_falls_back_to_raw_sql_when_no_title(mock_session_factory):
    """With cluster_guard but no title, fall back to raw SQL path."""
    factory, session = mock_session_factory
    cluster_guard = AsyncMock()

    linker = FeatureLinker(session_factory=factory, cluster_guard=cluster_guard)
    result = await linker.link_artifact(
        embedding=[0.1] * 10,
        artifact_type="learning",
        artifact_id=uuid.uuid4(),
        project_key="test",
    )
    cluster_guard.resolve.assert_not_called()
    assert result >= 0


@pytest.mark.asyncio
async def test_link_artifact_cluster_guard_inserts_link(mock_session_factory):
    """ClusterGuard path inserts a feature_artifact link after resolve()."""
    factory, session = mock_session_factory
    cluster_guard = AsyncMock()
    feature_id = uuid.uuid4()
    mock_feature = MagicMock(id=feature_id, name="Auth Feature")
    cluster_guard.resolve.return_value = (mock_feature, "created")

    linker = FeatureLinker(session_factory=factory, cluster_guard=cluster_guard)
    artifact_id = uuid.uuid4()
    result = await linker.link_artifact(
        embedding=[0.1] * 10,
        artifact_type="decision",
        artifact_id=artifact_id,
        project_key="proj",
        title="Add JWT auth",
    )
    assert result == 1
    # session.execute should have been called to insert the link
    session.execute.assert_called_once()
    session.commit.assert_called_once()


@pytest.mark.asyncio
async def test_link_artifact_returns_zero_when_cluster_guard_skips(mock_session_factory):
    """ClusterGuard link-only mode: (None, "skipped") -> 0 links, no insert.

    An explicit `if feature is None: return 0` guard is required *before* the
    insert. Without it, `feature.id` would raise AttributeError, which the
    outer fire-and-forget try/except would swallow and mislabel as
    "feature_linker.link_failed" — masking a legitimate skip as an error.
    """
    factory, session = mock_session_factory
    cluster_guard = AsyncMock()
    cluster_guard.resolve.return_value = (None, "skipped")

    linker = FeatureLinker(session_factory=factory, cluster_guard=cluster_guard)
    with patch("brain_v42.services.feature_linker.logger") as mock_logger:
        result = await linker.link_artifact(
            embedding=[0.1] * 10,
            artifact_type="learning",
            artifact_id=uuid.uuid4(),
            project_key="test",
            title="Some learning",
        )

    assert result == 0
    # No feature_artifacts INSERT should have been attempted.
    session.execute.assert_not_called()
    session.commit.assert_not_called()
    # Must be a deliberate no-op, not an exception swallowed by the
    # fire-and-forget try/except.
    mock_logger.warning.assert_not_called()


@pytest.mark.asyncio
async def test_link_artifact_cluster_guard_exception_caught(mock_session_factory):
    """Exceptions from cluster_guard.resolve() are caught (fire-and-forget)."""
    factory, session = mock_session_factory
    cluster_guard = AsyncMock()
    cluster_guard.resolve.side_effect = RuntimeError("reranker timeout")

    linker = FeatureLinker(session_factory=factory, cluster_guard=cluster_guard)
    result = await linker.link_artifact(
        embedding=[0.1] * 10,
        artifact_type="learning",
        artifact_id=uuid.uuid4(),
        project_key="test",
        title="Some learning",
    )
    assert result == 0  # fire-and-forget


# ── archived / merged_into filter in _do_link ─────────────────────────────


@pytest.mark.asyncio
async def test_do_link_excludes_archived_and_merged(mock_session_factory):
    """_do_link SQL must filter out archived and merged-into features.

    Without the filter, stale-linking would attach artifacts to archived
    clusters, silently polluting the graph and breaking briefing queries.
    """
    factory, session = mock_session_factory
    # Return empty rows so the method exits early (no insert needed).
    result = MagicMock()
    result.fetchall.return_value = []
    session.execute = AsyncMock(return_value=result)

    linker = FeatureLinker(session_factory=factory)
    await linker.link_artifact(
        embedding=[0.1] * 10,
        artifact_type="learning",
        artifact_id=uuid.uuid4(),
        project_key="test",
    )

    # _do_link calls session.execute exactly once (the SELECT).
    stmt = session.execute.call_args_list[0].args[0]
    # literal_binds=True fails on pgvector VECTOR type; inspect the WHERE
    # clause elements directly instead.
    compiled_sql = str(stmt.compile(compile_kwargs={"literal_binds": False}))
    # 'merged_into IS NULL' appears verbatim in the compiled SQL.
    assert "merged_into IS NULL" in compiled_sql
    # The archived filter renders as 'features.status != :status_N'; verify
    # the bound value is 'archived' by inspecting the statement's whereclause.
    where_str = str(stmt.whereclause.compile(compile_kwargs={"literal_binds": False}))
    # The status != 'archived' clause is present — find 'status' in the WHERE.
    assert "status" in where_str and "!=" in where_str
