"""Exercise curation and indexing races on isolated projects in real PostgreSQL."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brain_v42.db.tables import feature_artifacts, features
from brain_v42.services.cluster_guard import ClusterGuard
from brain_v42.services.status_engine import StatusEngine

pytestmark = pytest.mark.integration
_EMBEDDING = [0.1] * 1536


@pytest_asyncio.fixture
async def race_project(session_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[str]:
    """Delete only this test's project; the shared database is never emptied."""
    project_key = f"integ-cg-race-{uuid.uuid4().hex[:16]}"
    try:
        yield project_key
    finally:
        async with session_factory() as session:
            await session.execute(sa.delete(features).where(features.c.project_key == project_key))
            await session.commit()


def _guard(session_factory: async_sessionmaker[AsyncSession]) -> ClusterGuard:
    return ClusterGuard(session_factory, AsyncMock(), StatusEngine())


async def _feature(
    session_factory: async_sessionmaker[AsyncSession], project_key: str
) -> sa.Row[Any]:
    async with session_factory() as session:
        result = await session.execute(
            sa.insert(features)
            .values(
                project_key=project_key,
                name="Race target",
                description="Plan indexing race target",
                status="planned",
                embedding=_EMBEDDING,
            )
            .returning(*features.c)
        )
        row = result.one()
        await session.commit()
        return row


@pytest.mark.asyncio
async def test_concurrent_resolves_create_only_one_feature(
    session_factory: async_sessionmaker[AsyncSession],
    race_project: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hold the first empty snapshot so an unlocked second resolver duplicates it."""
    guard = _guard(session_factory)
    find_candidates = guard._find_candidates
    first_read = asyncio.Event()
    release_first = asyncio.Event()
    reads = 0

    async def hold_first_snapshot(
        session: AsyncSession, embedding: list[float], project_key: str
    ) -> list:
        nonlocal reads
        candidates = await find_candidates(session, embedding, project_key)
        reads += 1
        if reads == 1:
            first_read.set()
            await release_first.wait()
        return candidates

    monkeypatch.setattr(guard, "_find_candidates", hold_first_snapshot)
    first = asyncio.create_task(guard.resolve("Race plan", _EMBEDDING, race_project, "plan"))
    tasks = [first]
    try:
        await asyncio.wait_for(first_read.wait(), timeout=5)
        second = asyncio.create_task(guard.resolve("Race design", _EMBEDDING, race_project, "plan"))
        tasks.append(second)
        # Without the advisory lock the second call commits while the first
        # still holds its empty snapshot. With it, the second waits until release.
        await asyncio.wait({second}, timeout=1)
    finally:
        release_first.set()
        outcomes = await asyncio.wait_for(asyncio.gather(*tasks), timeout=10)

    assert sorted(action for _, action in outcomes) == ["created", "linked"]
    assert outcomes[0][0].id == outcomes[1][0].id
    async with session_factory() as session:
        result = await session.execute(
            sa.select(sa.func.count())
            .select_from(features)
            .where(features.c.project_key == race_project)
        )
        assert result.scalar_one() == 1


@pytest.mark.asyncio
async def test_link_rejects_a_merged_target(
    session_factory: async_sessionmaker[AsyncSession],
    race_project: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Curation between resolution and insertion redirects to the survivor."""
    survivor = await _feature(session_factory, race_project)
    loser = await _feature(session_factory, race_project)
    guard = _guard(session_factory)
    resolve = guard.resolve
    calls = 0

    async def resolve_then_merge(*args: Any, **kwargs: Any) -> tuple:
        nonlocal calls
        calls += 1
        if calls == 1:
            async with session_factory() as curator:
                await curator.execute(
                    sa.update(features)
                    .where(features.c.id == loser.id)
                    .values(merged_into=survivor.id, status="archived")
                )
                await curator.commit()
            return loser, "linked"
        return await resolve(*args, **kwargs)

    monkeypatch.setattr(guard, "resolve", resolve_then_merge)
    artifact_id = uuid.uuid4()
    feature, action, linked = await guard.resolve_and_link(
        "Knowledge", _EMBEDDING, race_project, "decision", "decision", artifact_id, lambda _: 1.0
    )
    assert linked is True
    assert action == "linked"
    assert feature.id == survivor.id
    assert calls == 2
    async with session_factory() as session:
        result = await session.execute(
            sa.select(sa.func.count())
            .select_from(feature_artifacts)
            .where(feature_artifacts.c.feature_id == loser.id)
        )
        assert result.scalar_one() == 0
        links = await session.execute(
            sa.select(feature_artifacts.c.feature_id).where(
                feature_artifacts.c.artifact_id == artifact_id
            )
        )
        assert links.scalars().all() == [survivor.id]


@pytest.mark.asyncio
async def test_link_to_a_live_target_is_idempotent_and_reports_success(
    session_factory: async_sessionmaker[AsyncSession], race_project: str
) -> None:
    """An already-present artifact is success, not a reason to resolve again."""
    target = await _feature(session_factory, race_project)
    plan_id = uuid.uuid4()
    guard = _guard(session_factory)
    for _ in range(2):
        feature, action, linked = await guard.resolve_and_link(
            "Race target", _EMBEDDING, race_project, "plan", "plan", plan_id, lambda _: 0.85
        )
        assert linked is True
        assert feature.id == target.id
        assert action == "linked"
    async with session_factory() as session:
        result = await session.execute(
            sa.select(sa.func.count())
            .select_from(feature_artifacts)
            .where(
                feature_artifacts.c.feature_id == target.id,
                feature_artifacts.c.artifact_id == plan_id,
            )
        )
        assert result.scalar_one() == 1


@pytest.mark.asyncio
async def test_status_update_does_not_revive_a_candidate_archived_after_read(
    session_factory: async_sessionmaker[AsyncSession], race_project: str
) -> None:
    """The decision uses a stale row, while its UPDATE must check the latest row."""
    guard = _guard(session_factory)
    await _feature(session_factory, race_project)
    async with session_factory() as reader:
        candidates = await guard._find_candidates(reader, _EMBEDDING, race_project)
        candidate = candidates[0]
        async with session_factory() as curator:
            await curator.execute(
                sa.update(features).where(features.c.id == candidate.id).values(status="archived")
            )
            await curator.commit()

        await guard._maybe_update_status(reader, candidate, "learning")
        await reader.commit()

    async with session_factory() as session:
        result = await session.execute(
            sa.select(features.c.status).where(features.c.id == candidate.id)
        )
        assert result.scalar_one() == "archived"


@pytest.mark.asyncio
async def test_resolve_keeps_link_when_status_moves_concurrently(
    session_factory: async_sessionmaker[AsyncSession],
    race_project: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An operator promotion after the candidate read is not a lost feature."""
    target = await _feature(session_factory, race_project)
    guard = _guard(session_factory)
    find_candidates = guard._find_candidates

    async def read_then_promote(session, embedding, project_key):
        candidates = await find_candidates(session, embedding, project_key)
        async with session_factory() as operator:
            await operator.execute(
                sa.update(features).where(features.c.id == target.id).values(status="building")
            )
            await operator.commit()
        return candidates

    monkeypatch.setattr(guard, "_find_candidates", read_then_promote)
    feature, action = await guard.resolve("Knowledge", _EMBEDDING, race_project, "learning")
    assert action == "linked"
    assert feature.id == target.id
    async with session_factory() as session:
        result = await session.execute(
            sa.select(features.c.status).where(features.c.id == target.id)
        )
        assert result.scalar_one() == "building"
