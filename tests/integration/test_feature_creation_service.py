"""PostgreSQL concurrency proof for explicit roadmap feature creation."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brain_v42.db.tables import features, project_contexts
from brain_v42.models.feature import Feature, FeatureCreate
from brain_v42.services.feature_creation_service import (
    FeatureAlreadyExistsError,
    FeatureCreationService,
)

pytestmark = pytest.mark.integration


class _BarrierEmbeddingService:
    def __init__(self) -> None:
        self._calls = 0
        self._both_started = asyncio.Event()

    async def embed(self, _text: str) -> list[float]:
        self._calls += 1
        if self._calls == 2:
            self._both_started.set()
        await self._both_started.wait()
        return [0.25] * 1536


async def test_concurrent_exact_creates_commit_once_and_conflict_once(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    project_key = f"integ-feature-create-{uuid4().hex[:8]}"
    async with session_factory.begin() as session:
        await session.execute(
            project_contexts.insert().values(
                project_key=project_key,
                name="Feature creation integration",
                description="Concurrent fail-closed proof",
            )
        )

    service = FeatureCreationService(session_factory, _BarrierEmbeddingService())
    first = FeatureCreate(
        project_key=project_key,
        name="Concurrent feature",
        description="First caller",
    )
    second = FeatureCreate(
        project_key=project_key,
        name="concurrent FEATURE",
        description="Second caller",
    )

    try:
        results = await asyncio.gather(
            service.create(first),
            service.create(second),
            return_exceptions=True,
        )

        assert sum(isinstance(result, Feature) for result in results) == 1
        assert sum(isinstance(result, FeatureAlreadyExistsError) for result in results) == 1
        async with session_factory() as session:
            count = await session.scalar(
                sa.select(sa.func.count())
                .select_from(features)
                .where(features.c.project_key == project_key)
            )
        assert count == 1
    finally:
        async with session_factory.begin() as session:
            await session.execute(sa.delete(features).where(features.c.project_key == project_key))
            await session.execute(
                sa.delete(project_contexts).where(project_contexts.c.project_key == project_key)
            )
