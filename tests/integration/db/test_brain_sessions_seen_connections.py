"""Exact absorption over every connection an operator session was seen on (03291fdc).

Measured 2026-10-04 (learning 3d401c5a): a coordinating operator session open
next to a lot session on the same project covers every creation instant, so the
window stage refuses every artifact and the lot's work stays on agent tracers.
The connection stage is exact but only knew the CURRENT connection, and the idle
eviction changes it many times a day.

The server now records each connection an operator lifecycle call arrives on.
The exact stage takes the tracers of all of them, bounded by the target's own
interval, so the successor of an end (or a relay) on the same connection never
takes what its predecessor produced, and the reverse.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brain_v42.db.tables import brain_sessions
from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo
from brain_v42.repositories.pg_learning import PgLearningRepo
from tests.integration.db.test_brain_sessions_derived_absorption import (
    _absorb_from_the_current_connection,
    _derive_one_artifact,
    _derived_capture,
    _Identity,
    _ledger_owner,
    _transport,
    absorption_project,  # noqa: F401 — fixture re-exported for this module
)

pytestmark = pytest.mark.integration


async def _close_tracer(session_factory: async_sessionmaker[AsyncSession], tracer) -> None:
    """The connection ended: its tracer is closed_inactive, its ledger kept (09d2b56e)."""
    async with session_factory.begin() as session:
        await session.execute(
            brain_sessions.update()
            .where(brain_sessions.c.id == tracer)
            .values(status="closed_inactive", ended_at=sa.func.clock_timestamp())
        )


async def test_a_lot_session_takes_its_artifacts_despite_a_coordinating_session(
    session_factory: async_sessionmaker[AsyncSession],
    absorption_project: str,  # noqa: F811
) -> None:
    repo = PgBrainSessionRepo(session_factory)
    learning_repo = PgLearningRepo(session_factory)

    with _derived_capture(True):
        coordinator = await repo.start(absorption_project, "coordinator")
        with _transport(uuid4().hex) as lot_connection:
            lot = await repo.start(absorption_project, "lot")
            lot_id = UUID(str(lot.session.id))
            # The lot's own lifecycle call on this connection: what records it.
            await _absorb_from_the_current_connection(repo, lot_id, "lot")
            tracer = await repo.auto_open(_Identity(absorption_project, lot_connection))
            artifact = await _derive_one_artifact(learning_repo, absorption_project)
        await _close_tracer(session_factory, tracer)

        # Idle eviction: the lot comes back on a fresh connection to end.
        with _transport(uuid4().hex):
            moved = await _absorb_from_the_current_connection(repo, lot_id, "lot")

    assert coordinator.session.id != lot.session.id
    assert moved == 1, "the coordinating session still blocks the lot's capture"
    assert await _ledger_owner(session_factory, artifact) == lot_id


async def test_the_coordinator_keeps_what_its_own_connection_produced(
    session_factory: async_sessionmaker[AsyncSession],
    absorption_project: str,  # noqa: F811
) -> None:
    repo = PgBrainSessionRepo(session_factory)
    learning_repo = PgLearningRepo(session_factory)

    with _derived_capture(True):
        with _transport(uuid4().hex) as coordinator_connection:
            coordinator = await repo.start(absorption_project, "coordinator")
            coordinator_id = UUID(str(coordinator.session.id))
            await _absorb_from_the_current_connection(repo, coordinator_id, "coordinator")
            await repo.auto_open(_Identity(absorption_project, coordinator_connection))
            theirs = await _derive_one_artifact(learning_repo, absorption_project)
        with _transport(uuid4().hex):
            lot = await repo.start(absorption_project, "lot")
            lot_id = UUID(str(lot.session.id))
            assert await _absorb_from_the_current_connection(repo, lot_id, "lot") == 0
        with _transport(uuid4().hex):
            await _absorb_from_the_current_connection(repo, coordinator_id, "coordinator")

    assert await _ledger_owner(session_factory, theirs) == coordinator_id


async def test_an_end_then_a_start_on_one_connection_split_the_artifacts_at_the_boundary(
    session_factory: async_sessionmaker[AsyncSession],
    absorption_project: str,  # noqa: F811
) -> None:
    """The successor never takes its predecessor's work, nor the reverse."""
    repo = PgBrainSessionRepo(session_factory)
    learning_repo = PgLearningRepo(session_factory)

    with _derived_capture(True):
        await repo.start(absorption_project, "coordinator")
        with _transport(uuid4().hex) as connection:
            await repo.auto_open(_Identity(absorption_project, connection))
            first = await repo.start(absorption_project, "first")
            first_id = UUID(str(first.session.id))
            await _absorb_from_the_current_connection(repo, first_id, "first")
            before = await _derive_one_artifact(learning_repo, absorption_project)
            await _absorb_from_the_current_connection(repo, first_id, "first")
            await repo.end(
                first_id,
                "first",
                summary="first ends",
                next_focus="focus",
                expected_focus_revision=first.session.started_focus_revision,
                nothing_to_capture_reason=None,
            )
            between = await _derive_one_artifact(learning_repo, absorption_project)
            second = await repo.start(absorption_project, "second")
            second_id = UUID(str(second.session.id))
            after = await _derive_one_artifact(learning_repo, absorption_project)
            await _absorb_from_the_current_connection(repo, second_id, "second")

    assert await _ledger_owner(session_factory, before) == first_id
    assert await _ledger_owner(session_factory, after) == second_id
    # Created while no session of this client was open: neither may claim it.
    assert await _ledger_owner(session_factory, between) not in {first_id, second_id}


async def test_a_session_records_each_connection_once(
    session_factory: async_sessionmaker[AsyncSession],
    absorption_project: str,  # noqa: F811
) -> None:
    repo = PgBrainSessionRepo(session_factory)

    with _derived_capture(True):
        started = await repo.start(absorption_project, "lot")
        session_id = UUID(str(started.session.id))
        with _transport(uuid4().hex) as connection:
            await _absorb_from_the_current_connection(repo, session_id, "lot")
            await _absorb_from_the_current_connection(repo, session_id, "lot")

    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    sa.text(
                        "SELECT connection_id FROM brain_session_connections "
                        "WHERE session_id = :session_id"
                    ),
                    {"session_id": session_id},
                )
            )
            .scalars()
            .all()
        )
    assert rows == [connection]
