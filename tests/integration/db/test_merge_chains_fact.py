"""Count a real merge chain on an isolated, migrated PostgreSQL database."""

from __future__ import annotations

from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from brain_v42.db.tables import learnings
from brain_v42.facts.probes.merge_chains import MergeChainsProbe
from brain_v42.facts.sources import PostgresSourceFactory

pytestmark = pytest.mark.integration


async def test_seeded_learning_chain_is_counted_once_and_other_tables_are_zero(
    private_head_engine: AsyncEngine,
) -> None:
    """An ordinary merge is not a chain; the probe must also work in a read-only snapshot."""
    survivor, middle, child, direct = (uuid4() for _ in range(4))
    async with AsyncSession(private_head_engine) as session, session.begin():
        for row_id in (survivor, middle, child, direct):
            await session.execute(
                sa.insert(learnings).values(
                    id=row_id,
                    topic="Merge-chain fact fixture",
                    insight="Merge-chain fact fixture",
                    project_key="merge-chain-fact-test",
                )
            )
        for row_id, target in ((middle, survivor), (child, middle), (direct, survivor)):
            await session.execute(
                sa.update(learnings).where(learnings.c.id == row_id).values(merged_into=target)
            )

    factory = PostgresSourceFactory(async_sessionmaker(private_head_engine))
    async with factory() as source:
        assert await source.session.scalar(sa.text("SHOW transaction_read_only")) == "on"
        assert await MergeChainsProbe().measure(source) == {
            "learnings": 1,
            "decisions": 0,
            "snippets": 0,
            "runbooks": 0,
            "adrs": 0,
            "features": 0,
        }
