"""The writer module against PostgreSQL: CAS, close, history (S2, S3).

Writes slot rows, which can never be deleted: every test here runs on the
module-private head database (`private_head_engine`), never on the shared one.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from brain_v42.db.focus_slots import (
    anchor_states,
    cas_slot_body,
    close_slot,
    lock_slot,
    record_slot_history,
)
from brain_v42.db.tables import focus_slot_history, focus_slots, project_contexts
from brain_v42.models.focus_slot import FocusSlotError
from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo
from brain_v42.services.brain_session_service import BrainSessionService

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest_asyncio.fixture
async def session_factory(
    private_head_engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(private_head_engine, class_=AsyncSession, expire_on_commit=False)


async def _open(factory: async_sessionmaker[AsyncSession], body: str = "first") -> tuple[UUID, str]:
    project = f"slots-{uuid4().hex[:10]}"
    async with factory.begin() as session:
        await session.execute(
            project_contexts.insert().values(
                project_key=project, name="w", description="w", current_focus="base"
            )
        )
        slot_id = await session.scalar(
            focus_slots.insert()
            .values(project_key=project, title="writer", body=body)
            .returning(focus_slots.c.id)
        )
        await record_slot_history(
            session, slot_id=slot_id, revision=0, body=body, source="slot_open"
        )
    assert isinstance(slot_id, UUID)
    return slot_id, project


async def _ended_session(factory: async_sessionmaker[AsyncSession], project: str) -> UUID:
    """A real session, started and ended through the lifecycle service (P8)."""
    service = BrainSessionService(PgBrainSessionRepo(factory))
    started = await service.start(project, "writer-test")
    await service.end(
        started.session.id,
        "writer-test",
        "ended for the history row",
        "base",
        started.session.started_focus_revision,
        nothing_to_capture_reason="no durable knowledge produced",
    )
    return started.session.id


async def _history(factory, slot_id: UUID) -> list[tuple[int, str, str]]:
    async with factory() as session:
        rows = await session.execute(
            sa.select(
                focus_slot_history.c.revision,
                focus_slot_history.c.source,
                focus_slot_history.c.body,
            )
            .where(focus_slot_history.c.slot_id == slot_id)
            .order_by(focus_slot_history.c.revision)
        )
        return [tuple(row) for row in rows]


async def test_cas_moves_the_revision_and_body_updated_at_only_on_a_text_change(session_factory):
    slot_id, project = await _open(session_factory)
    ended = await _ended_session(session_factory, project)
    async with session_factory.begin() as session:
        before = await lock_slot(session, slot_id)
        same = await cas_slot_body(session, slot_id=slot_id, expected=0, body="first")
        assert same is not None and same["revision"] == 1
        assert same["body_updated_at"] == before["body_updated_at"]
        await record_slot_history(
            session,
            slot_id=slot_id,
            revision=1,
            body="first",
            source="session_end",
            session_id=ended,
        )
    async with session_factory.begin() as session:
        stale = await cas_slot_body(session, slot_id=slot_id, expected=0, body="lost")
        assert stale is None
        moved = await cas_slot_body(session, slot_id=slot_id, expected=1, body="second")
        assert moved is not None and moved["revision"] == 2
        assert moved["body_updated_at"] > before["body_updated_at"]
        await record_slot_history(
            session,
            slot_id=slot_id,
            revision=2,
            body="second",
            source="session_end",
            session_id=ended,
        )
    assert [row[0] for row in await _history(session_factory, slot_id)] == [0, 1, 2]


async def test_close_bumps_the_revision_writes_one_row_and_refuses_a_second_close(
    session_factory,
):
    slot_id, _ = await _open(session_factory)
    async with session_factory.begin() as session:
        closed = await close_slot(session, slot_id=slot_id, reason="explicit", note="done")
    assert (closed["revision"], closed["close_reason"]) == (1, "explicit")
    assert await _history(session_factory, slot_id) == [
        (0, "slot_open", "first"),
        (1, "slot_close", "first"),
    ]
    with pytest.raises(FocusSlotError, match="^slot_closed: "):
        async with session_factory.begin() as session:
            await close_slot(session, slot_id=slot_id, reason="explicit", note="again")
    async with session_factory.begin() as session:
        assert await cas_slot_body(session, slot_id=slot_id, expected=1, body="x") is None


async def test_record_slot_history_is_idempotent_on_a_replayed_revision(session_factory):
    slot_id, _ = await _open(session_factory)
    async with session_factory.begin() as session:
        await record_slot_history(
            session, slot_id=slot_id, revision=0, body="first", source="slot_open"
        )
    assert len(await _history(session_factory, slot_id)) == 1


async def test_a_slot_without_anchors_has_no_state(session_factory):
    slot_id, _ = await _open(session_factory)
    async with session_factory() as session:
        assert await anchor_states(session, [slot_id]) == {slot_id: []}
