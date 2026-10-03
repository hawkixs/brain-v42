"""The bound end and resume (spec §3): the slot, never the base; ruling 1 pinned."""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa

from brain_v42.db.focus_slots import close_slot
from brain_v42.db.tables import focus_slot_history, focus_slots
from brain_v42.models.brain_session import BrainSessionFocusOutcome
from brain_v42.models.focus_slot import FocusSlotError
from tests.integration.db import test_delivery_focus_slots as _slots
from tests.integration.db.test_delivery_focus_slot_binding import open_slot, sessions, started
from tests.integration.db.test_delivery_focus_slots import base_state

# Fixtures are rebound by assignment: a plain import would be shadowed by the parameters.
session_factory = _slots.session_factory
slot_project = _slots.slot_project

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def bound(factory, project: str) -> tuple[UUID, str, UUID]:
    slot_id = await open_slot(factory, project)
    session_id, key = await started(factory, project)
    await sessions(factory).bind(session_id, key, slot_id)
    return session_id, key, slot_id


async def slot_row(factory, slot_id: UUID):
    async with factory() as session:
        return (
            (await session.execute(sa.select(focus_slots).where(focus_slots.c.id == slot_id)))
            .mappings()
            .one()
        )


async def history(factory, slot_id: UUID) -> list[tuple[int, str, UUID | None]]:
    async with factory() as session:
        rows = await session.execute(
            sa.select(
                focus_slot_history.c.revision,
                focus_slot_history.c.source,
                focus_slot_history.c.session_id,
            )
            .where(focus_slot_history.c.slot_id == slot_id)
            .order_by(focus_slot_history.c.revision)
        )
        return [tuple(row) for row in rows]


async def _receipt_close(factory, slot_id: UUID) -> None:
    async with factory.begin() as session:
        await close_slot(session, slot_id=slot_id, reason=f"receipt:{uuid4()}", note=None)


async def test_a_bound_end_writes_the_slot_and_leaves_the_base_untouched(
    session_factory, slot_project
):
    before = await base_state(session_factory, slot_project)
    session_id, key, slot_id = await bound(session_factory, slot_project)
    ended = await sessions(session_factory).end(session_id, key, "done", "next slot body", 0)
    assert ended.focus_outcome is BrainSessionFocusOutcome.APPLIED
    assert (ended.current_focus, ended.current_focus_revision) == ("next slot body", 1)
    assert (ended.focus_at_end, ended.focus_revision_at_end) == ("next slot body", 1)
    assert ended.focus_diff == "+5/-0 chars"
    assert (await slot_row(session_factory, slot_id))["body"] == "next slot body"
    assert await history(session_factory, slot_id) == [
        (0, "slot_open", None),
        (1, "session_end", session_id),
    ]
    assert await base_state(session_factory, slot_project) == before  # S1


async def test_a_stale_bound_end_closes_with_conflict_and_the_slot_untouched(
    session_factory, slot_project
):
    session_id, key, slot_id = await bound(session_factory, slot_project)
    ended = await sessions(session_factory).end(session_id, key, "done", "late body", 7)
    assert ended.focus_outcome is BrainSessionFocusOutcome.CONFLICT
    assert (ended.current_focus, ended.current_focus_revision) == ("slot body", 0)
    assert (await slot_row(session_factory, slot_id))["revision"] == 0


async def test_a_bound_end_over_4000_characters_is_refused_before_any_write(
    session_factory, slot_project
):
    session_id, key, slot_id = await bound(session_factory, slot_project)
    with pytest.raises(FocusSlotError, match="^slot_body_too_long: "):
        await sessions(session_factory).end(session_id, key, "done", "é" * 4001, 0)
    resumed = await sessions(session_factory).resume(session_id, key)
    assert resumed.session.status == "open"
    assert (await slot_row(session_factory, slot_id))["revision"] == 0
    accepted = await sessions(session_factory).end(session_id, key, "done", "é" * 4000, 0)
    assert accepted.focus_outcome is BrainSessionFocusOutcome.APPLIED


async def test_an_equal_bound_end_replays_against_the_slot(session_factory, slot_project):
    session_id, key, slot_id = await bound(session_factory, slot_project)
    svc = sessions(session_factory)
    await svc.end(session_id, key, "done", "replayed body", 0)
    await _receipt_close(session_factory, slot_id)  # the slot moves on: revision 2
    again = await svc.end(session_id, key, "done", "replayed body", 0)
    assert again.replayed is True
    assert (again.current_focus, again.current_focus_revision) == ("replayed body", 2)
    assert (again.focus_at_end, again.focus_revision_at_end) == ("replayed body", 1)


async def test_resume_of_a_bound_session_returns_the_slot(session_factory, slot_project):
    session_id, key, _slot_id = await bound(session_factory, slot_project)
    resumed = await sessions(session_factory).resume(session_id, key)
    assert (resumed.current_focus, resumed.current_focus_revision) == ("slot body", 0)


async def test_ruling_1_post_close_revision_is_slot_closed_and_the_session_stays_open(
    session_factory, slot_project
):
    session_id, key, slot_id = await bound(session_factory, slot_project)
    await _receipt_close(session_factory, slot_id)
    with pytest.raises(FocusSlotError, match=r"^slot_closed: .*expected_focus_revision=0 "):
        await sessions(session_factory).end(session_id, key, "done", "after close", 1)
    assert (await sessions(session_factory).resume(session_id, key)).session.status == "open"
    abandoned = await sessions(session_factory).abandon(session_id, key, "slot closed on receipt")
    assert abandoned.session.status == "abandoned"


async def test_ruling_1_pre_close_revision_records_a_conflict(session_factory, slot_project):
    session_id, key, slot_id = await bound(session_factory, slot_project)
    await _receipt_close(session_factory, slot_id)
    ended = await sessions(session_factory).end(session_id, key, "done", "after close", 0)
    assert ended.focus_outcome is BrainSessionFocusOutcome.CONFLICT
    assert (ended.current_focus_revision, ended.focus_revision_at_end) == (1, 1)
    assert [row[1] for row in await history(session_factory, slot_id)] == [
        "slot_open",
        "slot_close",
    ]


async def test_the_same_bound_end_twice_concurrently_writes_one_history_row(
    session_factory, slot_project
):
    session_id, key, slot_id = await bound(session_factory, slot_project)
    svc = sessions(session_factory)
    first, second = await asyncio.gather(
        svc.end(session_id, key, "done", "concurrent", 0),
        svc.end(session_id, key, "done", "concurrent", 0),
    )
    assert sorted([first.replayed, second.replayed]) == [False, True]
    assert [row[1] for row in await history(session_factory, slot_id)] == [
        "slot_open",
        "session_end",
    ]
