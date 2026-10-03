"""The slot briefing view over PostgreSQL: the previous session after a relay, gaps (a)/(c)."""

from uuid import uuid4

import pytest
import sqlalchemy as sa

from brain_v42.db.tables import decisions, focus_slots
from brain_v42.repositories.pg_focus_slot import PgFocusSlotRepo
from tests.integration.db import test_delivery_focus_slots as _slots
from tests.integration.db.test_delivery_focus_slot_binding import sessions
from tests.integration.db.test_delivery_focus_slot_bound_end import bound
from tests.integration.db.test_delivery_focus_slot_relay import relay

session_factory = _slots.session_factory
slot_project = _slots.slot_project

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def test_the_successor_sees_its_slot_and_its_predecessor(session_factory, slot_project):
    session_id, key, slot_id = await bound(session_factory, slot_project)
    async with session_factory.begin() as session:
        decision = await session.scalar(
            decisions.insert()
            .values(
                title="relay keeps the base",
                description="d",
                reasoning="r",
                project_key=slot_project,
            )
            .returning(decisions.c.id)
        )
    await sessions(session_factory).checkpoint(
        session_id, key, seq=1, progress="half done", next_step="relay"
    )
    result = await relay(
        session_factory, session_id, key, knowledge_ids=[decision], summary="what happened"
    )
    view = await PgFocusSlotRepo(session_factory).briefing_view(slot_project, result.session.id)
    assert view.bound_slot is not None and view.bound_slot.id == slot_id
    assert view.previous is not None and view.previous.session_id == session_id
    assert view.previous.summary == "what happened"
    assert view.previous.decisions == [(decision, "relay keeps the base")]
    assert view.previous.last_checkpoint is not None
    assert view.previous.last_checkpoint.progress == "half done"
    assert [slot.id for slot in view.open_slots] == [slot_id]


async def test_an_unbound_session_sees_open_slots_and_no_bound_slot(session_factory, slot_project):
    _session_id, _key, slot_id = await bound(session_factory, slot_project)
    other = await sessions(session_factory).start(slot_project, f"op-{uuid4().hex[:8]}")
    view = await PgFocusSlotRepo(session_factory).briefing_view(slot_project, other.session.id)
    assert view.bound_slot is None and view.previous is None
    assert [slot.id for slot in view.open_slots] == [slot_id]
    assert view.open_slots[0].bound_session_id is not None


async def test_last_ended_session_activity_prevents_a_slot_from_looking_stale(
    session_factory, slot_project
):
    session_id, key, slot_id = await bound(session_factory, slot_project)
    await sessions(session_factory).end(session_id, key, "done", "body", 0)
    async with session_factory.begin() as session:
        await session.execute(
            focus_slots.update()
            .where(focus_slots.c.id == slot_id)
            .values(body_updated_at=sa.func.now() - sa.text("interval '8 days'"))
        )
    other = await sessions(session_factory).start(slot_project, f"op-{uuid4().hex[:8]}")
    view = await PgFocusSlotRepo(session_factory).briefing_view(slot_project, other.session.id)
    assert view.open_slots[0].bound_session_id is None
    assert view.open_slots[0].is_stale is False
