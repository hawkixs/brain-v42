"""PostgreSQL proof that the project listing is ordered by update and not paged.

Ticket 372324a6: `brain_list_projects` rendered 20 of 60 projects, ordered by
creation, so the project updated that very day was missing from the list.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brain_v42.db.tables import project_contexts
from brain_v42.repositories.pg_project_context import PgProjectContextRepo

pytestmark = pytest.mark.integration


async def test_list_all_returns_every_project_most_recently_updated_first(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tag = uuid4().hex[:8]
    # Created in ascending key order but updated in the opposite order, and far
    # enough in the future to sit above whatever the shared database holds.
    keys = [f"integ-list-{tag}-{i:02d}" for i in range(22)]
    base = datetime(2099, 1, 1, tzinfo=UTC)
    async with session_factory.begin() as session:
        for i, key in enumerate(keys):
            await session.execute(
                project_contexts.insert().values(
                    project_key=key,
                    name=key,
                    description="listing order proof",
                    updated_at=base - timedelta(days=i),
                )
            )

    try:
        listed = await PgProjectContextRepo(session_factory).list_all()

        assert [c.project_key for c in listed[:22]] == keys
        assert len(listed) >= 22
    finally:
        async with session_factory.begin() as session:
            await session.execute(
                sa.delete(project_contexts).where(project_contexts.c.project_key.in_(keys))
            )
