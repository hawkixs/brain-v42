"""brain_slot_open/list/close against PostgreSQL, every refusal and the base untouched (S1).

Writes slot rows, which can never be deleted: every test here runs on the
module-private head database (`private_head_engine`), never on the shared one.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from brain_v42.db.tables import (
    focus_slot_history,
    focus_slots,
    project_contexts,
    project_focus_history,
)
from brain_v42.models.focus_slot import FocusSlotError, SlotAnchor
from brain_v42.models.ticket import TicketCreate, TicketKind
from brain_v42.repositories.pg_focus_slot import PgFocusSlotRepo
from brain_v42.repositories.pg_release_derivation import OBSERVER_IDENTITY
from brain_v42.repositories.pg_ticket import PgTicketRepo
from brain_v42.services.focus_slot_service import FocusSlotService
from tests.integration.db.test_delivery_requester_acceptance import _workflow

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]
NOW = datetime(2026, 10, 3, tzinfo=UTC)


@pytest_asyncio.fixture
async def session_factory(
    private_head_engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(private_head_engine, class_=AsyncSession, expire_on_commit=False)


async def ensure_project(factory, project_key: str) -> None:
    async with factory.begin() as session:
        await session.execute(
            pg_insert(project_contexts)
            .values(
                project_key=project_key,
                name=project_key,
                description="slot fixture",
                current_focus="base focus",
            )
            .on_conflict_do_nothing(index_elements=["project_key"])
        )


@pytest_asyncio.fixture
async def slot_project(session_factory) -> str:
    """A fresh project. Never deleted: slots have no delete path and history is append-only."""
    key = f"slots-{uuid4().hex[:10]}"
    await ensure_project(session_factory, key)
    return key


def service(factory) -> FocusSlotService:
    return FocusSlotService(PgFocusSlotRepo(factory))


async def ticket(factory, project: str, *, target_release: str | None = None) -> UUID:
    repo = PgTicketRepo(factory)
    created = await repo.create(
        TicketCreate(
            kind=TicketKind.REQUEST,
            title=f"slot anchor {uuid4()}",
            body="b",
            from_project=project,
            to_project=project,
        )
    )
    if target_release is not None:
        message = await repo.set_target_release(
            created.id,
            author_project=project,
            expected=None,
            new=target_release,
            message=f"planned for {target_release}",
        )
        assert message is not None
    return created.id


async def base_state(factory, project: str) -> tuple:
    async with factory() as session:
        context = (
            await session.execute(
                sa.select(
                    project_contexts.c.current_focus,
                    project_contexts.c.focus_revision,
                    project_contexts.c.focus_updated_at,
                ).where(project_contexts.c.project_key == project)
            )
        ).one()
        history = await session.scalar(
            sa.select(sa.func.count())
            .select_from(project_focus_history)
            .where(project_focus_history.c.project_key == project)
        )
    return (*context, history)


async def history_sources(factory, slot_id: UUID) -> list[str]:
    async with factory() as session:
        return list(
            (
                await session.execute(
                    sa.select(focus_slot_history.c.source)
                    .where(focus_slot_history.c.slot_id == slot_id)
                    .order_by(focus_slot_history.c.revision)
                )
            ).scalars()
        )


async def test_open_writes_revision_zero_its_anchors_and_one_history_row(
    session_factory, slot_project
):
    before = await base_state(session_factory, slot_project)
    anchor = SlotAnchor(kind="ticket", ticket_id=await ticket(session_factory, slot_project))
    opened = await service(session_factory).open(slot_project, "relay work", "handover", [anchor])
    assert (opened.replayed, opened.slot.revision, opened.slot.anchors) == (False, 0, [anchor])
    assert await history_sources(session_factory, opened.slot.id) == ["slot_open"]
    assert await base_state(session_factory, slot_project) == before  # S1


async def test_an_equal_open_is_a_replay_and_a_different_one_a_title_conflict(
    session_factory, slot_project
):
    anchor = SlotAnchor(kind="lot", target_release="9.1.0")
    await ticket(session_factory, slot_project, target_release="9.1.0")
    svc = service(session_factory)
    first = await svc.open(slot_project, "lot", "body", [anchor])
    again = await svc.open(slot_project, "lot", "body", [anchor])
    assert (again.replayed, again.slot.id) == (True, first.slot.id)
    assert await history_sources(session_factory, first.slot.id) == ["slot_open"]
    with pytest.raises(FocusSlotError, match="^slot_title_conflict: "):
        await svc.open(slot_project, "lot", "another body", [anchor])


@pytest.mark.parametrize(
    ("make_anchor", "code"),
    [
        (
            lambda other_ticket: SlotAnchor(kind="ticket", ticket_id=other_ticket),
            "anchor_ticket_foreign",
        ),
        (lambda _: SlotAnchor(kind="pr", repository_id=987_654, pr_number=1), "anchor_unbound_pr"),
        (lambda _: SlotAnchor(kind="lot", target_release="9.9.9"), "anchor_lot_unplanned"),
    ],
)
async def test_anchor_refusals(session_factory, slot_project, make_anchor, code):
    foreign = f"slots-{uuid4().hex[:10]}"
    await ensure_project(session_factory, foreign)
    other_ticket = await ticket(session_factory, foreign)
    with pytest.raises(FocusSlotError, match=f"^{code}: "):
        await service(session_factory).open(slot_project, "t", "b", [make_anchor(other_ticket)])
    async with session_factory() as session:
        count = await session.scalar(
            sa.select(sa.func.count())
            .select_from(focus_slots)
            .where(focus_slots.c.project_key == slot_project)
        )
    assert count == 0  # nothing written


async def test_a_foreign_ticket_is_refused_before_any_receipt_is_consulted(
    session_factory, slot_project, monkeypatch
):
    """Task 4's ticket receipt predicate has no project check: the refusal order is the guard."""

    async def never(*_args, **_kwargs):
        raise AssertionError("received_anchor consulted before the ticket ownership check")

    monkeypatch.setattr("brain_v42.repositories.pg_focus_slot.received_anchor", never)
    foreign = f"slots-{uuid4().hex[:10]}"
    await ensure_project(session_factory, foreign)
    other_ticket = await ticket(session_factory, foreign)
    with pytest.raises(FocusSlotError, match="^anchor_ticket_foreign: "):
        await service(session_factory).open(
            slot_project, "t", "b", [SlotAnchor(kind="ticket", ticket_id=other_ticket)]
        )


async def test_body_bound_counts_characters_not_bytes(session_factory, slot_project):
    anchor = SlotAnchor(kind="ticket", ticket_id=await ticket(session_factory, slot_project))
    svc = service(session_factory)
    with pytest.raises(FocusSlotError, match="^slot_body_too_long: "):
        await svc.open(slot_project, "too long", "é" * 4001, [anchor])
    async with session_factory() as session:
        count = await session.scalar(
            sa.select(sa.func.count())
            .select_from(focus_slots)
            .where(focus_slots.c.project_key == slot_project)
        )
    assert count == 0  # refused before any write
    body = "é" * 4000  # 8,000 bytes
    opened = await svc.open(slot_project, "exact bound", body, [anchor])
    assert opened.slot.body == body


async def test_unknown_project_is_project_not_found(session_factory):
    unknown = f"slots-{uuid4().hex[:10]}"
    with pytest.raises(FocusSlotError, match="^project_not_found: "):
        await service(session_factory).open(
            unknown, "t", "b", [SlotAnchor(kind="lot", target_release="1.0.0")]
        )
    with pytest.raises(FocusSlotError, match="^project_not_found: "):
        await service(session_factory).list(unknown, "open")


async def test_a_lot_already_released_is_anchor_already_received(session_factory):
    await ensure_project(session_factory, "executor")
    ticket_row, _binding, delivery = await _workflow(session_factory)
    await PgTicketRepo(session_factory).set_target_release(
        ticket_row.id, author_project="executor", expected=None, new="8.1.0", message="plan"
    )
    await delivery.attest(
        ticket_row.id,
        actor_project="executor",
        caller_identity=OBSERVER_IDENTITY,
        kind="released",
        payload={
            "repository_id": 1,
            "tag": "v8.1.0",
            "tag_sha": "a" * 40,
            "integration_sha": "a" * 40,
        },
        idempotency_key=f"released-slot-{ticket_row.id}",
        emitted_at=NOW,
        contract_revision=1,
    )
    with pytest.raises(FocusSlotError, match="^anchor_already_received: "):
        await service(session_factory).open(
            "executor",
            f"lot 8.1.0 {uuid4().hex[:6]}",
            "b",
            [SlotAnchor(kind="lot", target_release="8.1.0")],
        )


async def test_list_derives_staleness(session_factory, slot_project):
    await ticket(session_factory, slot_project, target_release="9.2.0")
    svc = service(session_factory)
    opened = await svc.open(
        slot_project, "aging", "b", [SlotAnchor(kind="lot", target_release="9.2.0")]
    )
    fresh = await svc.list(slot_project, "open")
    assert [(s.id, s.bound_session_id, s.is_stale, s.receipt_pending) for s in fresh.slots] == [
        (opened.slot.id, None, False, False)
    ]
    async with session_factory.begin() as session:  # not a revision move: no history needed
        await session.execute(
            focus_slots.update()
            .where(focus_slots.c.id == opened.slot.id)
            .values(body_updated_at=sa.func.now() - sa.text("interval '8 days'"))
        )
    assert (await svc.list(slot_project, "open")).slots[0].is_stale is True


async def test_close_is_explicit_bumps_the_revision_and_refuses_stale_or_closed(
    session_factory, slot_project
):
    await ticket(session_factory, slot_project, target_release="9.3.0")
    svc = service(session_factory)
    opened = await svc.open(
        slot_project, "close me", "b", [SlotAnchor(kind="lot", target_release="9.3.0")]
    )
    with pytest.raises(FocusSlotError, match="^slot_revision_conflict: .*revision 0"):
        await svc.close(opened.slot.id, 5, "done")
    closed = await svc.close(opened.slot.id, 0, "done")
    assert (closed.slot.revision, closed.slot.close_reason, closed.slot.close_note) == (
        1,
        "explicit",
        "done",
    )
    assert await history_sources(session_factory, opened.slot.id) == ["slot_open", "slot_close"]
    with pytest.raises(FocusSlotError, match="^slot_closed: "):
        await svc.close(opened.slot.id, 1, "again")
    with pytest.raises(FocusSlotError, match="^slot_not_found: "):
        await svc.close(uuid4(), 0, "nothing")
    listed = await svc.list(slot_project, "closed")
    assert [s.id for s in listed.slots] == [opened.slot.id]
