"""brain_session_bind against PostgreSQL: one slot per session, one open session per slot."""

# The fixtures are imported from the slot module (private head database): a test parameter
# necessarily shadows its import.
# ruff: noqa: F811

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa

from brain_v42.db.focus_slots import close_slot, lock_slot
from brain_v42.db.tables import brain_sessions
from brain_v42.models.brain_session import BrainSessionIdentityConflictError
from brain_v42.models.focus_slot import FocusSlotError, SlotAnchor
from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo
from brain_v42.services.brain_session_service import BrainSessionService
from tests.integration.db.test_delivery_focus_slots import (  # noqa: F401 - fixtures
    ensure_project,
    service,
    session_factory,
    slot_project,
    ticket,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


def sessions(factory) -> BrainSessionService:
    return BrainSessionService(PgBrainSessionRepo(factory))


async def open_slot(factory, project: str, title: str | None = None) -> UUID:
    release = f"9.{uuid4().int % 1000}.{uuid4().int % 1000}"
    await ticket(factory, project, target_release=release)
    opened = await service(factory).open(
        project,
        title or f"slot {uuid4().hex[:6]}",
        "slot body",
        [SlotAnchor(kind="lot", target_release=release)],
    )
    return opened.slot.id


async def started(factory, project: str) -> tuple[UUID, str]:
    key = f"op-{uuid4().hex[:8]}"
    result = await sessions(factory).start(project, key)
    return result.session.id, key


async def open_holders(factory, slot_id: UUID) -> int:
    async with factory() as session:
        return int(
            await session.scalar(
                sa.select(sa.func.count())
                .select_from(brain_sessions)
                .where(brain_sessions.c.slot_id == slot_id, brain_sessions.c.status == "open")
            )
            or 0
        )


async def test_bind_returns_the_slot_revision_and_body_and_replays(session_factory, slot_project):
    slot_id = await open_slot(session_factory, slot_project)
    session_id, key = await started(session_factory, slot_project)
    first = await sessions(session_factory).bind(session_id, key, slot_id)
    again = await sessions(session_factory).bind(session_id, key, slot_id)
    assert (first.slot_id, first.slot_revision, first.slot_body) == (slot_id, 0, "slot body")
    assert again == first


async def test_bind_refusals(session_factory, slot_project):
    svc = sessions(session_factory)
    slot_id = await open_slot(session_factory, slot_project)
    other_slot = await open_slot(session_factory, slot_project)
    session_id, key = await started(session_factory, slot_project)
    with pytest.raises(BrainSessionIdentityConflictError):
        await svc.bind(session_id, "wrong-key", slot_id)
    with pytest.raises(FocusSlotError, match="^slot_not_found: "):
        await svc.bind(session_id, key, uuid4())
    foreign = f"slots-{uuid4().hex[:10]}"
    await ensure_project(session_factory, foreign)
    with pytest.raises(FocusSlotError, match="^slot_project_mismatch: "):
        await svc.bind(session_id, key, await open_slot(session_factory, foreign))
    closed = await open_slot(session_factory, slot_project)
    async with session_factory.begin() as session:
        await close_slot(session, slot_id=closed, reason="explicit", note="gone")
    with pytest.raises(FocusSlotError, match="^slot_closed: "):
        await svc.bind(session_id, key, closed)
    await svc.bind(session_id, key, slot_id)
    with pytest.raises(FocusSlotError, match="^session_already_bound: "):
        await svc.bind(session_id, key, other_slot)
    rival, rival_key = await started(session_factory, slot_project)
    with pytest.raises(FocusSlotError, match="^slot_busy: "):
        await svc.bind(rival, rival_key, slot_id)
    await svc.abandon(rival, rival_key, "test over")
    with pytest.raises(FocusSlotError, match="^session_not_open: "):
        await svc.bind(rival, rival_key, other_slot)


async def test_a_trace_never_binds(session_factory, slot_project):
    slot_id = await open_slot(session_factory, slot_project)
    async with session_factory.begin() as session:
        trace = await session.scalar(
            brain_sessions.insert()
            .values(
                project_key=slot_project,
                client_key=f"trace-{uuid4().hex[:6]}",
                started_focus_revision=0,
                nature="agent",
            )
            .returning(brain_sessions.c.id)
        )
        key = await session.scalar(
            sa.select(brain_sessions.c.client_key).where(brain_sessions.c.id == trace)
        )
    with pytest.raises(FocusSlotError, match="^session_is_agent_trace: "):
        await sessions(session_factory).bind(trace, key, slot_id)


async def test_two_concurrent_binds_one_wins_one_is_slot_busy(session_factory, slot_project):
    slot_id = await open_slot(session_factory, slot_project)
    (first, first_key), (second, second_key) = (
        await started(session_factory, slot_project),
        await started(session_factory, slot_project),
    )
    outcomes = await asyncio.gather(
        sessions(session_factory).bind(first, first_key, slot_id),
        sessions(session_factory).bind(second, second_key, slot_id),
        return_exceptions=True,
    )
    errors = [o for o in outcomes if isinstance(o, BaseException)]
    assert len(errors) == 1 and isinstance(errors[0], FocusSlotError)
    assert errors[0].code == "slot_busy"
    assert await open_holders(session_factory, slot_id) == 1


async def test_an_explicit_close_is_refused_while_a_session_is_bound(session_factory, slot_project):
    slot_id = await open_slot(session_factory, slot_project)
    session_id, key = await started(session_factory, slot_project)
    await sessions(session_factory).bind(session_id, key, slot_id)
    with pytest.raises(FocusSlotError, match="^slot_bound: "):
        await service(session_factory).close(slot_id, 0, "while bound")


async def test_a_bind_waiting_on_a_closing_slot_rechecks_it_and_is_slot_closed(
    session_factory, slot_project
):
    """The close takes the slot lock first, reads no holder, and commits: the bind that was
    queued behind it must see `closed_at` once it gets the lock, never write `slot_id`."""
    slot_id = await open_slot(session_factory, slot_project)
    session_id, key = await started(session_factory, slot_project)
    async with session_factory.begin() as closing:
        await lock_slot(closing, slot_id)
        bind = asyncio.create_task(sessions(session_factory).bind(session_id, key, slot_id))
        await asyncio.sleep(0.5)
        assert not bind.done()  # parked on the slot row lock held by the closer
        await close_slot(closing, slot_id=slot_id, reason="explicit", note="closed under a bind")
    with pytest.raises(FocusSlotError, match="^slot_closed: "):
        await bind
    assert await open_holders(session_factory, slot_id) == 0


async def test_a_bind_racing_a_close_has_exactly_one_winner(session_factory, slot_project):
    for _ in range(5):
        slot_id = await open_slot(session_factory, slot_project)
        session_id, key = await started(session_factory, slot_project)
        bound, closed = await asyncio.gather(
            sessions(session_factory).bind(session_id, key, slot_id),
            service(session_factory).close(slot_id, 0, "racing a bind"),
            return_exceptions=True,
        )
        if isinstance(bound, BaseException):
            assert isinstance(bound, FocusSlotError) and bound.code == "slot_closed"
            assert not isinstance(closed, BaseException)
            assert await open_holders(session_factory, slot_id) == 0
        else:
            assert isinstance(closed, FocusSlotError) and closed.code == "slot_bound"
            assert await open_holders(session_factory, slot_id) == 1
        await sessions(session_factory).abandon(session_id, key, "race over")
