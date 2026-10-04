"""A scoped decision delete and a relay naming the same decisions must not deadlock.

Ticket d85b4f66, reopened by the review of its first fix. `relay` validates the
captures it is given and, afterwards, the whole ledger: two locking statements, so
the ascending id order held inside each did not hold across them. With A in the
ledger, A.superseded_by = B, and the relay naming B, the relay held B and then
wanted A while the delete held A and wanted B.

A module of its own: relays write focus slots, which the shared head must never get.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa

from brain_v42.db.tables import decisions
from brain_v42.models.brain_session import KnowledgeCapturedError
from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo
from brain_v42.repositories.pg_decision import PgDecisionRepo
from tests.integration.db import test_delivery_focus_slots as _slots
from tests.integration.db.test_delivery_focus_slot_bound_end import bound

# Fixtures are rebound by assignment: a plain import would be shadowed by the parameters.
session_factory = _slots.session_factory
slot_project = _slots.slot_project

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def _lock_waiters(factory) -> int:
    async with factory() as session:
        return int(
            await session.scalar(
                sa.text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND wait_event_type = 'Lock'"
                )
            )
        )


async def _until(condition: Callable[[], Awaitable[bool]]) -> None:
    async with asyncio.timeout(10):
        while not await condition():
            await asyncio.sleep(0.02)


async def _decision(factory, project: str, decision_id: UUID, **values) -> None:
    async with factory.begin() as session:
        await session.execute(
            decisions.insert().values(
                id=decision_id,
                project_key=project,
                title="t",
                description="d",
                reasoning="r",
                **values,
            )
        )


async def test_scoped_delete_and_relay_naming_the_same_pair_do_not_deadlock(
    session_factory, slot_project
):
    session_id, key, _slot = await bound(session_factory, slot_project)
    referrer_id, target_id = sorted((uuid4(), uuid4()), key=str)
    await _decision(session_factory, slot_project, referrer_id)
    await _decision(session_factory, slot_project, target_id)
    sessions = PgBrainSessionRepo(session_factory)
    await sessions.capture(session_id, key, [referrer_id])
    async with session_factory.begin() as session:
        await session.execute(
            decisions.update()
            .where(decisions.c.id == referrer_id)
            .values(status="superseded", superseded_by=target_id)
        )

    async with session_factory() as gate:
        async with gate.begin():
            # FOR UPDATE on A parks the delete first, then the relay's lock on A.
            await gate.execute(
                sa.select(decisions.c.id).where(decisions.c.id == referrer_id).with_for_update()
            )
            deleting = asyncio.create_task(
                PgDecisionRepo(session_factory).delete(target_id, project_key=slot_project)
            )
            await _until(lambda: _waiters_at_least(session_factory, 1))
            relaying = asyncio.create_task(
                sessions.relay(
                    session_id,
                    key,
                    summary="s",
                    handover="h",
                    expected_slot_revision=0,
                    new_client_key=f"next-{uuid4().hex[:8]}",
                    initiator="operator",
                    knowledge_ids=[target_id],
                    nothing_to_capture_reason=None,
                )
            )
            await _until(lambda: _waiters_at_least(session_factory, 2))

    outcomes = await asyncio.wait_for(
        asyncio.gather(deleting, relaying, return_exceptions=True), timeout=15
    )

    failures = [o for o in outcomes if isinstance(o, BaseException)]
    assert not any("deadlock" in str(f).lower() for f in failures), failures
    deleted_ok = outcomes[0] is True
    relayed_ok = not isinstance(outcomes[1], BaseException)
    assert deleted_ok != relayed_ok, outcomes
    if not deleted_ok:
        assert isinstance(outcomes[0], KnowledgeCapturedError), outcomes


async def _waiters_at_least(factory, count: int) -> bool:
    return await _lock_waiters(factory) >= count
