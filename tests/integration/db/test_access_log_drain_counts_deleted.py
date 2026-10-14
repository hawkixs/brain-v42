"""Exercise the drain on PostgreSQL: only DELETE RETURNING defines counted rows."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from brain_v42.db.tables import access_log, access_log_daily
from brain_v42.repositories.pg_access_log import PgAccessLogRepo

pytestmark = pytest.mark.integration


async def test_counters_and_journal_match_removed_rows_within_the_boundary(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep later ids while journaling every drained entity, actor and UTC day."""
    entities = [("learning", uuid4()), ("decision", uuid4())]
    actors = ["red-lab", "dream-codex-reorg"]
    days = [datetime(2026, 8, 30, 22, 15, tzinfo=UTC), datetime(2026, 8, 31, 23, 40, tzinfo=UTC)]
    events = [
        {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "access_type": "search",
            "actor": actor,
            "accessed_at": day + timedelta(minutes=offset),
        }
        for entity_index, (entity_type, entity_id) in enumerate(entities)
        for actor in actors
        for day_index, day in enumerate(days)
        for offset in range(1 + entity_index + day_index)
    ]
    factory = async_sessionmaker(engine, expire_on_commit=False)
    repo = PgAccessLogRepo(session_factory=factory)
    async with factory() as session, session.begin():
        # A non-UTC connection must still bucket the events by UTC calendar day.
        await session.execute(sa.text("SET LOCAL TIME ZONE 'Pacific/Auckland'"))
        ids = list(
            (await session.scalars(sa.insert(access_log).returning(access_log.c.id), events)).all()
        )
        boundary = max(ids)
        execute = session.execute
        late_id: int | None = None

        async def insert_after_boundary(statement: Any, *args: Any, **kwargs: Any) -> Any:
            nonlocal late_id
            result = await execute(statement, *args, **kwargs)
            if "max(access_log.id)" in str(statement):
                late = await execute(
                    sa.insert(access_log).values(events[0]).returning(access_log.c.id)
                )
                late_id = late.scalar_one()
            return result

        monkeypatch.setattr(session, "execute", insert_after_boundary)

        counters = await repo.aggregate_in_session(session)

        remaining = list((await session.scalars(sa.select(access_log.c.id))).all())
        assert late_id is not None and late_id > boundary
        assert remaining == [late_id]
        assert (
            await session.scalar(
                sa.select(sa.func.count())
                .select_from(access_log)
                .where(access_log.c.id <= boundary)
            )
            == 0
        )
        removed = len(set(ids) - set(remaining))
        assert sum(entry["count"] for entry in counters.values()) == removed == len(events)
        for entity in entities:
            owned = [e for e in events if (e["entity_type"], e["entity_id"]) == entity]
            human = [e for e in owned if e["actor"] == "red-lab"]
            assert counters[entity] == {
                "count": len(owned),
                "count_human": len(human),
                "max_accessed": max(e["accessed_at"] for e in owned),
                "max_accessed_human": max(e["accessed_at"] for e in human),
            }
        journal = (await session.execute(sa.select(access_log_daily))).mappings().all()
        expected = Counter(
            (e["entity_type"], e["entity_id"], e["actor"], e["accessed_at"].date()) for e in events
        )
        assert {
            (r["entity_type"], r["entity_id"], r["actor"], r["day"]): r["count"] for r in journal
        } == dict(expected)
        assert sum(row["count"] for row in journal) == removed
        for row in journal:
            assert row["last_accessed_at"] == max(
                e["accessed_at"]
                for e in events
                if (e["entity_type"], e["entity_id"], e["actor"], e["accessed_at"].date())
                == (row["entity_type"], row["entity_id"], row["actor"], row["day"])
            )
        # Roll back even the surviving event: no queue residue for later tests.
        await session.rollback()


async def test_a_lower_id_committed_after_aggregation_is_left_for_the_next_flush(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reserved low id must never be deleted without counters and a journal row."""
    entity_id = uuid4()
    event = {
        "entity_type": "learning",
        "entity_id": entity_id,
        "access_type": "search",
        "actor": "red-lab",
        "accessed_at": datetime(2026, 8, 30, 22, 15, tzinfo=UTC),
    }
    factory = async_sessionmaker(engine, expire_on_commit=False)
    repo = PgAccessLogRepo(session_factory=factory)
    try:
        async with engine.connect() as writer:
            await writer.begin()
            reserved_id = await writer.scalar(
                sa.insert(access_log).values(event).returning(access_log.c.id)
            )
            async with engine.begin() as seed:
                boundary = await seed.scalar(
                    sa.insert(access_log).values(event).returning(access_log.c.id)
                )
            assert reserved_id < boundary
            async with factory() as session:
                await session.execute(sa.text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
                execute = session.execute

                async def commit_after_aggregation(
                    statement: Any, *args: Any, **kwargs: Any
                ) -> Any:
                    result = await execute(statement, *args, **kwargs)
                    if "GROUP BY" in str(statement):
                        # The row was invisible to this statement's snapshot.
                        # A separate DELETE would now see it and silently lose it.
                        await writer.commit()
                    return result

                monkeypatch.setattr(session, "execute", commit_after_aggregation)
                counters = await repo.aggregate_in_session(session)
                await session.commit()

            async with engine.connect() as check:
                remaining = list(
                    (
                        await check.scalars(
                            sa.select(access_log.c.id).where(access_log.c.entity_id == entity_id)
                        )
                    ).all()
                )
                journal_count = await check.scalar(
                    sa.select(sa.func.sum(access_log_daily.c.count)).where(
                        access_log_daily.c.entity_id == entity_id
                    )
                )
            counted = counters[("learning", entity_id)]["count"]
            assert counted == journal_count == 2 - len(remaining)
            assert remaining == [reserved_id]

        async with factory() as session:
            second = await repo.aggregate_in_session(session)
            await session.commit()
        assert second[("learning", entity_id)]["count"] == 1
        async with engine.connect() as check:
            assert (
                await check.scalar(
                    sa.select(sa.func.sum(access_log_daily.c.count)).where(
                        access_log_daily.c.entity_id == entity_id
                    )
                )
                == 2
            )
    finally:
        async with engine.begin() as cleanup:
            await cleanup.execute(
                sa.delete(access_log_daily).where(access_log_daily.c.entity_id == entity_id)
            )
            await cleanup.execute(sa.delete(access_log).where(access_log.c.entity_id == entity_id))
