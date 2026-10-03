"""The slot briefing view over PostgreSQL: the previous session after a relay, gaps (a)/(c)."""

from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa

from brain_v42.db.focus_slots import close_slot
from brain_v42.db.tables import decisions, focus_slots, project_contexts
from brain_v42.models.focus_slot import SlotAnchor
from brain_v42.repositories.pg_focus_slot import PgFocusSlotRepo
from brain_v42.repositories.pg_project_context import PgProjectContextRepo
from tests.integration.db import test_delivery_focus_slots as _slots
from tests.integration.db.test_delivery_focus_slot_binding import sessions
from tests.integration.db.test_delivery_focus_slot_bound_end import bound
from tests.integration.db.test_delivery_focus_slot_relay import relay
from tests.integration.db.test_delivery_focus_slots import service, ticket

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


async def closed_slot(factory, project: str, reason: str | None) -> UUID:
    """Open a ticket-anchored slot and close it. `reason=None` goes through the service's
    explicit close (what `brain_slot_close` calls); otherwise `close_slot` is called with that
    reason, the very call the receipt hook makes. A real integration receipt is not built here:
    its workflow is bound to the shared `executor` project, which would leak other modules'
    slots into the list under test."""
    opened = await service(factory).open(
        project,
        f"distill {uuid4().hex[:6]}",
        "b",
        [SlotAnchor(kind="ticket", ticket_id=await ticket(factory, project))],
    )
    if reason is None:
        await service(factory).close(opened.slot.id, 0, "done")
    else:
        async with factory.begin() as session:
            await close_slot(session, slot_id=opened.slot.id, reason=reason, note=None)
    return opened.slot.id


async def to_distill(factory, project: str) -> list[UUID]:
    other = await sessions(factory).start(project, f"op-{uuid4().hex[:8]}")
    view = await PgFocusSlotRepo(factory).briefing_view(project, other.session.id)
    return [slot.id for slot in view.to_distill]


async def test_a_receipt_closed_slot_is_to_distill_and_an_explicit_close_is_not(
    session_factory, slot_project
):
    receipted = await closed_slot(session_factory, slot_project, f"receipt:{uuid4()}")
    await closed_slot(session_factory, slot_project, None)
    assert await to_distill(session_factory, slot_project) == [receipted]


async def test_writing_the_base_clears_the_slots_closed_before_it(session_factory, slot_project):
    await closed_slot(session_factory, slot_project, f"receipt:{uuid4()}")
    assert (
        await PgProjectContextRepo(session_factory).update_focus(slot_project, "distilled focus")
        is not None
    )
    after = await closed_slot(session_factory, slot_project, f"receipt:{uuid4()}")
    assert await to_distill(session_factory, slot_project) == [after]


async def test_a_base_never_written_leaves_every_receipt_closed_slot_to_distill(
    session_factory, slot_project
):
    async with session_factory() as session:
        stamp = await session.scalar(
            sa.select(project_contexts.c.focus_updated_at).where(
                project_contexts.c.project_key == slot_project
            )
        )
    assert stamp is None
    receipted = await closed_slot(session_factory, slot_project, f"receipt:{uuid4()}")
    assert await to_distill(session_factory, slot_project) == [receipted]
