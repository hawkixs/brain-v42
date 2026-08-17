"""L'agrégation sépare les lectures humaines des lectures du dream."""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa

from brain_v42.repositories.pg_access_log import PgAccessLogRepo

pytestmark = pytest.mark.integration


async def _insert(session, entity_id, actor: str) -> None:
    await session.execute(
        sa.text(
            "INSERT INTO access_log (entity_type, entity_id, access_type, actor) "
            "VALUES ('learning', :id, 'get_by_id', :actor)"
        ),
        {"id": entity_id, "actor": actor},
    )


class TestAggregateByActor:
    async def test_splits_human_from_system(self, db_session, session_factory) -> None:
        entity_id = uuid.uuid4()
        await _insert(db_session, entity_id, "red-lab")
        await _insert(db_session, entity_id, "dream-codex-reorg")
        await _insert(db_session, entity_id, "dream-codex-synth")
        await db_session.commit()

        repo = PgAccessLogRepo(session_factory)
        async with session_factory() as session:
            aggregated = await repo.aggregate_in_session(session)
            await session.commit()

        stats = aggregated[("learning", entity_id)]
        assert stats["count"] == 3
        assert stats["count_human"] == 1

    async def test_unknown_actor_is_not_human(self, db_session, session_factory) -> None:
        entity_id = uuid.uuid4()
        await _insert(db_session, entity_id, "unknown")
        await db_session.commit()

        repo = PgAccessLogRepo(session_factory)
        async with session_factory() as session:
            aggregated = await repo.aggregate_in_session(session)
            await session.commit()

        stats = aggregated[("learning", entity_id)]
        assert stats["count"] == 1
        assert stats["count_human"] == 0
