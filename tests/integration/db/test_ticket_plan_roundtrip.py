import asyncio

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from brain_v42.models.ticket import TicketCreate, TicketKind
from brain_v42.repositories.pg_ticket import PgTicketRepo
from tests.conftest import require_test_db_url

pytestmark = pytest.mark.asyncio


async def test_column_and_message_land_together_and_cas_refuses_a_stale_expected():
    engine = create_async_engine(require_test_db_url())
    try:
        repo = PgTicketRepo(async_sessionmaker(engine, expire_on_commit=False))
        ticket = await repo.create(
            TicketCreate(
                kind=TicketKind.REQUEST,
                title="integ-plan",
                body="b",
                from_project="brain-v42",
                to_project="brain-v42",
            )
        )
        first = await repo.set_target_release(
            ticket.id,
            author_project="brain-v42",
            expected=None,
            new="0.6.3",
            message="planned for 0.6.3",
        )
        assert first is not None
        stale = await repo.set_target_release(
            ticket.id,
            author_project="brain-v42",
            expected=None,
            new="0.6.4",
            message="planned for 0.6.4",
        )
        assert stale is None
        assert (await repo.get_by_id(ticket.id)).target_release == "0.6.3"
        bodies = [m.body for m in await repo.get_messages(ticket.id)]
        assert bodies == ["planned for 0.6.3"]
    finally:
        async with engine.begin() as connection:
            await connection.execute(sa.text("DELETE FROM tickets WHERE title = 'integ-plan'"))
        await engine.dispose()


async def test_two_concurrent_plans_one_wins_one_message():
    engine = create_async_engine(require_test_db_url())
    try:
        repo = PgTicketRepo(async_sessionmaker(engine, expire_on_commit=False))
        ticket = await repo.create(
            TicketCreate(
                kind=TicketKind.REQUEST,
                title="integ-plan",
                body="b",
                from_project="brain-v42",
                to_project="brain-v42",
            )
        )
        results = await asyncio.gather(
            *(
                repo.set_target_release(
                    ticket.id,
                    author_project="brain-v42",
                    expected=None,
                    new=value,
                    message=f"planned for {value}",
                )
                for value in ("0.6.3", "0.6.4")
            )
        )
        assert sum(result is not None for result in results) == 1
        assert len(await repo.get_messages(ticket.id)) == 1
    finally:
        async with engine.begin() as connection:
            await connection.execute(sa.text("DELETE FROM tickets WHERE title = 'integ-plan'"))
        await engine.dispose()
