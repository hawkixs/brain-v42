"""The server closes the agent tracers it opens (ticket 09d2b56e).

Measured 2026-10-05: 1,616 tracers open, none closed since the Dream sweep was
suspended, while the clients DID end their connections (1,183 DELETE and 480
idle evictions in 24 h). Two server-side writers close them now:

- ``close_agent_traces(connection_id)`` when the transport of that connection
  terminates (DELETE, idle eviction, shutdown);
- ``close_inactive_agent_traces`` as the net for a connection that never
  terminates cleanly (a crash, a killed process), on the 4 h observation rule.

Both write ``closed_inactive`` and nothing else: the ledger stays, so a closed
tracer remains a donor for derived capture. Neither can reach an operator
session.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brain_v42.db.tables import brain_sessions, project_contexts
from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo

pytestmark = pytest.mark.integration

T0 = datetime(2026, 10, 5, 3, 0, tzinfo=UTC)


@dataclass(frozen=True)
class _Identity:
    project_key: str
    connection_id: str
    started_by_actor: str = "integ-actor"
    nature: str = "agent"
    intent: str | None = None


@pytest_asyncio.fixture
async def project(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[str]:
    project_key = f"integ-traceclose-{uuid4().hex[:10]}"
    async with session_factory.begin() as session:
        await session.execute(
            project_contexts.insert().values(
                project_key=project_key,
                name="Trace close integration",
                description="Isolated trace-close fixture",
                current_focus="focus",
            )
        )
    try:
        yield project_key
    finally:
        async with session_factory.begin() as session:
            await session.execute(
                brain_sessions.delete().where(brain_sessions.c.project_key == project_key)
            )
            await session.execute(
                project_contexts.delete().where(project_contexts.c.project_key == project_key)
            )


async def _row(session_factory: async_sessionmaker[AsyncSession], session_id):
    async with session_factory() as session:
        return (
            (
                await session.execute(
                    sa.select(brain_sessions).where(brain_sessions.c.id == session_id)
                )
            )
            .mappings()
            .one()
        )


async def _operator(session_factory, project_key: str, *, last_heartbeat_at: datetime):
    async with session_factory.begin() as session:
        return (
            await session.execute(
                brain_sessions.insert()
                .values(
                    project_key=project_key,
                    client_key=f"operator-{uuid4().hex[:8]}",
                    started_focus="focus",
                    started_focus_revision=1,
                    started_at=last_heartbeat_at,
                    last_heartbeat_at=last_heartbeat_at,
                    last_observed_at=last_heartbeat_at,
                )
                .returning(brain_sessions.c.id)
            )
        ).scalar_one()


async def test_a_terminated_connection_closes_its_open_tracer(
    session_factory: async_sessionmaker[AsyncSession], project: str
) -> None:
    repo = PgBrainSessionRepo(session_factory)
    connection = uuid4().hex
    tracer = await repo.auto_open(_Identity(project, connection), now=T0)

    closed = await repo.close_agent_traces(connection, now=T0 + timedelta(minutes=3))

    assert closed == [tracer]
    row = await _row(session_factory, tracer)
    assert row["status"] == "closed_inactive"
    assert row["abandonment_reason"] is None
    assert row["ended_at"] == T0 + timedelta(minutes=3)


async def test_closing_a_connection_touches_no_other_connection_and_no_operator(
    session_factory: async_sessionmaker[AsyncSession], project: str
) -> None:
    repo = PgBrainSessionRepo(session_factory)
    mine, theirs = uuid4().hex, uuid4().hex
    await repo.auto_open(_Identity(project, mine), now=T0)
    neighbour = await repo.auto_open(_Identity(project, theirs), now=T0)
    operator = await _operator(session_factory, project, last_heartbeat_at=T0)

    await repo.close_agent_traces(mine, now=T0 + timedelta(minutes=1))

    assert (await _row(session_factory, neighbour))["status"] == "open"
    assert (await _row(session_factory, operator))["status"] == "open"


async def test_closing_an_already_closed_connection_changes_nothing(
    session_factory: async_sessionmaker[AsyncSession], project: str
) -> None:
    repo = PgBrainSessionRepo(session_factory)
    connection = uuid4().hex
    tracer = await repo.auto_open(_Identity(project, connection), now=T0)
    await repo.close_agent_traces(connection, now=T0 + timedelta(minutes=1))

    assert await repo.close_agent_traces(connection, now=T0 + timedelta(minutes=9)) == []
    assert (await _row(session_factory, tracer))["ended_at"] == T0 + timedelta(minutes=1)


async def test_the_net_closes_only_tracers_unobserved_past_the_threshold(
    session_factory: async_sessionmaker[AsyncSession], project: str
) -> None:
    repo = PgBrainSessionRepo(session_factory)
    stale = await repo.auto_open(_Identity(project, uuid4().hex), now=T0)
    fresh = await repo.auto_open(_Identity(project, uuid4().hex), now=T0 + timedelta(hours=3))
    forgotten_operator = await _operator(
        session_factory, project, last_heartbeat_at=T0 - timedelta(days=30)
    )

    closed = await repo.close_inactive_agent_traces(
        inactive_after=timedelta(hours=4), now=T0 + timedelta(hours=5)
    )

    assert stale in closed
    assert fresh not in closed
    assert (await _row(session_factory, stale))["status"] == "closed_inactive"
    assert (await _row(session_factory, fresh))["status"] == "open"
    # The net is the tracer's, never the human's: a forgotten operator session
    # stays for the explicit commands and the 7-day Dream rule.
    assert (await _row(session_factory, forgotten_operator))["status"] == "open"


async def test_the_net_closes_a_week_old_tracer_without_abandoning_it(
    session_factory: async_sessionmaker[AsyncSession], project: str
) -> None:
    """Past seven days a tracer is still a tracer: closed, ledger kept, no reason."""
    repo = PgBrainSessionRepo(session_factory)
    old = await repo.auto_open(_Identity(project, uuid4().hex), now=T0 - timedelta(days=9))

    await repo.close_inactive_agent_traces(inactive_after=timedelta(hours=4), now=T0)

    row = await _row(session_factory, old)
    assert row["status"] == "closed_inactive"
    assert row["abandonment_reason"] is None
