"""Integration tests for ClusterGuard link-only mode against a real database.

The unit tests in tests/unit/test_cluster_guard.py mock the session, so they
prove the branching logic but not that the absent INSERT is really absent.
These tests run the resolver against real PostgreSQL+pgvector and count rows
in ``features`` afterwards.

Neither GPU embedding nor the reranker is needed: on an empty project
``_find_candidates`` returns no rows, so resolution goes straight to the
create-or-skip decision without calling either service.

Each test uses a unique project_key for isolation.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brain_v42.db.tables import features
from brain_v42.services.cluster_guard import ClusterGuard
from brain_v42.services.status_engine import StatusEngine

pytestmark = pytest.mark.integration


def _unique_key() -> str:
    """Return a unique project_key per test call."""
    return f"integ-linkonly-{uuid.uuid4().hex[:8]}"


def _build_guard(session_factory: async_sessionmaker[AsyncSession]) -> ClusterGuard:
    """ClusterGuard wired to the test engine with a real StatusEngine.

    embedding_svc and reranker are stubs: the paths exercised here never
    reach them. If a future change makes them reachable, the AsyncMock will
    return a MagicMock and the test will fail loudly rather than silently
    exercise a different path.
    """
    return ClusterGuard(
        session_factory=session_factory,
        embedding_svc=AsyncMock(),
        reranker=AsyncMock(),
        status_engine=StatusEngine(),
    )


async def _count_features(
    session_factory: async_sessionmaker[AsyncSession], project_key: str
) -> int:
    async with session_factory() as session:
        result = await session.execute(
            sa.select(sa.func.count())
            .select_from(features)
            .where(features.c.project_key == project_key)
        )
        return int(result.scalar_one())


@pytest.mark.asyncio
async def test_knowledge_signal_creates_no_row_in_features(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A decision on an empty project is skipped and inserts nothing.

    This is the defect the link-only mode fixes: brain_log_decision used to
    promote itself into the roadmap as a pseudo-feature named after its title.
    """
    project_key = _unique_key()
    guard = _build_guard(session_factory)

    feature, action = await guard.resolve(
        text="Ajouter un chemin explicite fail-closed",
        embedding=[0.1] * 1536,
        project_key=project_key,
        signal_type="decision",
    )

    assert action == "skipped"
    assert feature is None
    assert await _count_features(session_factory, project_key) == 0


@pytest.mark.asyncio
async def test_work_signal_still_creates_a_row_in_features(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Non-regression: a plan on an empty project still creates its feature.

    Guards the other half of the contract — link-only must not have turned
    into "never create anything".
    """
    project_key = _unique_key()
    guard = _build_guard(session_factory)

    feature, action = await guard.resolve(
        text="Plans Chunking & Search Integration",
        embedding=[0.1] * 1536,
        project_key=project_key,
        signal_type="plan",
    )

    assert action == "created"
    assert feature is not None
    assert await _count_features(session_factory, project_key) == 1
