"""`brain_slot_open` against a receipt that is still uncommitted (ticket 5caae01d).

A receipt committing between open's receipt check and open's own commit cannot see the
uncommitted slot, so its hook closes nothing and the slot would stay open with
`receipt_pending` for good. The writer holds `tickets ... FOR UPDATE`; open must queue
behind it, then see the committed receipt and refuse.

Tickets becoming eligible after open's ticket-lock statement need the project fence:
the hook waits for open's commit, then sees and closes its slot (ticket d2dbf163).
In the reverse order, the hook holds the exclusive fence for a newly eligible ticket;
open waits on the shared fence, then refuses the committed receipt.

Writes slot rows: private head database, like the other slot-writing modules. The interleaving
is forced, not raced: the receipt transaction stays open while a second connection is observed
blocked on it through `pg_blocking_pids`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brain_v42.db.focus_slots import received_anchor
from brain_v42.db.tables import delivery_artifact_bindings, delivery_attestations, focus_slots
from brain_v42.models.focus_slot import FocusSlotError, SlotAnchor
from tests.integration.db import test_delivery_focus_slots as _slots
from tests.integration.db.test_delivery_focus_slot_receipts import (
    EXECUTOR,
    integrate,
    integrate_in,
    plan,
    release_in,
    workflow,
)
from tests.integration.db.test_delivery_focus_slots import service
from tests.integration.db.test_delivery_receipt_issuance import RID

# Fixtures are rebound by assignment: a plain import would be shadowed by the parameters.
session_factory = _slots.session_factory

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]
WAIT_SECONDS = 30


async def wait_until_blocked_on(factory, holder_pid: int, *, statement: str) -> int:
    """Return the pid of a backend blocked on the holder, running a `statement` that names it.

    Naming the statement keeps the wait honest: the backend we mean to see blocked, not any
    other connection that happens to queue behind the holder.
    """
    async with asyncio.timeout(WAIT_SECONDS):
        while True:
            async with factory() as session:
                blocked = await session.scalar(
                    sa.text(
                        "SELECT min(pid) FROM pg_stat_activity "
                        "WHERE :pid = ANY(pg_blocking_pids(pid)) "
                        "AND wait_event_type = 'Lock' AND query ILIKE :statement"
                    ),
                    {"pid": holder_pid, "statement": f"%{statement}%"},
                )
            if blocked:
                return int(blocked)
            await asyncio.sleep(0.02)


async def open_behind_uncommitted(factory, anchors, title: str, writer) -> None:
    """Hold the writer's transaction open, queue an open behind it, then commit the writer.

    The open must refuse: the slot it would insert cannot be seen by a receipt that has
    already committed, so the only safe outcome is no slot at all.
    """
    async with factory() as holder, holder.begin():
        holder_pid = await holder.scalar(sa.text("SELECT pg_backend_pid()"))
        await writer(holder)
        queued = asyncio.create_task(service(factory).open(EXECUTOR, title, "b", anchors))
        try:
            await wait_until_blocked_on(factory, holder_pid, statement="FROM tickets")
        except TimeoutError:
            queued.cancel()
            raise AssertionError("open did not wait for the uncommitted receipt writer") from None
        assert not queued.done()
    with pytest.raises(FocusSlotError, match="^anchor_already_received: "):
        await asyncio.wait_for(queued, timeout=WAIT_SECONDS)
    async with factory() as session:
        assert not await session.scalar(
            sa.select(sa.func.count())
            .select_from(focus_slots)
            .where(focus_slots.c.project_key == EXECUTOR, focus_slots.c.title == title)
        )


async def test_a_ticket_open_queues_behind_an_uncommitted_receipt_then_refuses(session_factory):
    ticket, binding, _, number = await workflow(session_factory)
    await open_behind_uncommitted(
        session_factory,
        [SlotAnchor(kind="ticket", ticket_id=ticket.id)],
        f"ticket race {uuid4().hex[:6]}",
        lambda holder: integrate_in(session_factory, holder, ticket.id, binding, number),
    )


async def test_a_pr_open_queues_behind_an_uncommitted_receipt_then_refuses(session_factory):
    ticket, binding, _, number = await workflow(session_factory)
    await open_behind_uncommitted(
        session_factory,
        [SlotAnchor(kind="pr", repository_id=RID, pr_number=number)],
        f"pr race {uuid4().hex[:6]}",
        lambda holder: integrate_in(session_factory, holder, ticket.id, binding, number),
    )


async def test_a_lot_open_queues_behind_an_uncommitted_release_then_refuses(session_factory):
    ticket, binding, _, number = await workflow(session_factory)
    await plan(session_factory, ticket.id, "8.5.1")
    await integrate(session_factory, ticket.id, binding, number)  # the release candidate's merge
    await open_behind_uncommitted(
        session_factory,
        [SlotAnchor(kind="lot", target_release="8.5.1")],
        f"lot race {uuid4().hex[:6]}",
        lambda holder: release_in(holder, ticket.id, "v8.5.1"),
    )


async def test_an_open_that_got_its_locks_first_makes_the_later_receipt_find_the_slot(
    session_factory,
):
    """The other order: the open owns the ticket lock, so the receipt queues behind the open's
    commit and its hook sees the committed slot. A squatter on the title holds the open between
    its ticket lock and its slot insert, the one place a receipt can arrive."""
    ticket, binding, _, number = await workflow(session_factory)
    title = f"open first {uuid4().hex[:6]}"
    anchors = [SlotAnchor(kind="ticket", ticket_id=ticket.id)]
    async with session_factory() as squatter, squatter.begin():
        squatter_pid = await squatter.scalar(sa.text("SELECT pg_backend_pid()"))
        await squatter.execute(
            focus_slots.insert().values(project_key=EXECUTOR, title=title, body="x")
        )
        opening = asyncio.create_task(service(session_factory).open(EXECUTOR, title, "b", anchors))
        open_pid = await wait_until_blocked_on(
            session_factory, squatter_pid, statement="INSERT INTO focus_slots"
        )
        receipt = asyncio.create_task(integrate(session_factory, ticket.id, binding, number))
        await wait_until_blocked_on(
            session_factory, open_pid, statement="FROM tickets"
        )  # the writer queues on the open
        assert not receipt.done()
        await squatter.rollback()
    opened = await asyncio.wait_for(opening, timeout=WAIT_SECONDS)
    issued = await asyncio.wait_for(receipt, timeout=WAIT_SECONDS)
    async with session_factory() as session:
        row = (
            (
                await session.execute(
                    sa.select(focus_slots).where(focus_slots.c.id == opened.slot.id)
                )
            )
            .mappings()
            .one()
        )
    assert row["close_reason"] == f"receipt:{issued.id}" and row["closed_at"] is not None


async def open_before_new_candidate_receipt(
    factory: async_sessionmaker[AsyncSession],
    anchors: Sequence[SlotAnchor],
    title: str,
    writer: Callable[[], Awaitable[None]],
) -> None:
    """Pause after the receipt read, then let a previously ineligible ticket write.

    The title squatter blocks only the slot insert, after open's ticket locks and receipt
    read. Without the project fence the writer commits and misses the uncommitted slot;
    with it the hook queues behind open and closes the slot after open commits. Observe
    either completion or the actual lock wait so the regression fails on slot state,
    rather than timing out while expecting a lock that the old code does not take.
    """
    tasks: list[asyncio.Task] = []
    try:
        async with asyncio.timeout(WAIT_SECONDS):
            async with factory() as squatter, squatter.begin():
                squatter_pid = await squatter.scalar(sa.text("SELECT pg_backend_pid()"))
                await squatter.execute(
                    focus_slots.insert().values(project_key=EXECUTOR, title=title, body="x")
                )
                opening = asyncio.create_task(service(factory).open(EXECUTOR, title, "b", anchors))
                tasks.append(opening)
                open_pid = await wait_until_blocked_on(
                    factory, squatter_pid, statement="INSERT INTO focus_slots"
                )
                receipt = asyncio.create_task(writer())
                tasks.append(receipt)
                blocked = asyncio.create_task(
                    wait_until_blocked_on(factory, open_pid, statement="pg_advisory_xact_lock(")
                )
                tasks.append(blocked)
                done, _ = await asyncio.wait(
                    [receipt, blocked], return_when=asyncio.FIRST_COMPLETED
                )
                for task in done:
                    task.result()  # propagate a writer failure or an unexpected lock timeout
                assert not opening.done()
                await squatter.rollback()
            opened, _ = await asyncio.gather(opening, receipt)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async with factory() as session:
        row = (
            (
                await session.execute(
                    sa.select(focus_slots).where(focus_slots.c.id == opened.slot.id)
                )
            )
            .mappings()
            .one()
        )
        completing_row = await received_anchor(session, project_key=EXECUTOR, anchor=anchors[0])
    assert completing_row is not None  # the later writer really satisfied this anchor
    assert row["closed_at"] is not None, "the later receipt left the slot open with receipt_pending"
    assert row["close_reason"] == f"receipt:{completing_row}"
    listed = await service(factory).list(EXECUTOR, "closed", limit=100)
    assert not next(item.receipt_pending for item in listed.slots if item.id == opened.slot.id)


async def test_a_lot_open_is_closed_by_a_ticket_integrated_after_its_lock_statement(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A planned ticket without an integration SHA is outside the lot's initial lock set."""
    ticket, binding, _, number = await workflow(session_factory)
    release = f"9.{uuid4().int % 1000}.{uuid4().int % 1000}"
    await plan(session_factory, ticket.id, release)

    async def writer() -> None:
        async with session_factory() as session, session.begin():
            await integrate_in(session_factory, session, ticket.id, binding, number)
            await release_in(session, ticket.id, f"v{release}")

    await open_before_new_candidate_receipt(
        session_factory,
        [SlotAnchor(kind="lot", target_release=release)],
        f"new lot candidate {uuid4().hex[:6]}",
        writer,
    )


async def test_a_pr_open_is_closed_by_a_ticket_bound_after_its_lock_statement(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A second ticket newly bound to the same PR is outside the PR's initial lock set."""
    _, _, _, number = await workflow(session_factory)

    async def writer() -> None:
        ticket, binding, _, _ = await workflow(session_factory, pr_number=number)
        await integrate(session_factory, ticket.id, binding, number)

    await open_before_new_candidate_receipt(
        session_factory,
        [SlotAnchor(kind="pr", repository_id=RID, pr_number=number)],
        f"new pr candidate {uuid4().hex[:6]}",
        writer,
    )


async def open_behind_new_candidate_receipt(
    factory: async_sessionmaker[AsyncSession],
    anchors: Sequence[SlotAnchor],
    title: str,
    writer: Callable[[AsyncSession], Awaitable[None]],
) -> None:
    """A newly eligible writer owns the exclusive fence before open takes its locks.

    Its new binding or integration SHA is uncommitted, so open's ticket-lock statement
    cannot select its ticket. Only the project fence orders the receipt read after
    the writer's commit. Without the fence, open inserts before that commit and this
    helper times out waiting for the shared advisory lock that was never taken.
    """
    tasks: list[asyncio.Task] = []
    try:
        async with asyncio.timeout(WAIT_SECONDS):
            async with factory() as holder, holder.begin():
                holder_pid = await holder.scalar(sa.text("SELECT pg_backend_pid()"))
                await writer(holder)  # close_slots_satisfied_by now holds the exclusive fence
                assert await received_anchor(holder, project_key=EXECUTOR, anchor=anchors[0])
                async with factory() as observer:
                    assert (
                        await received_anchor(observer, project_key=EXECUTOR, anchor=anchors[0])
                        is None
                    )
                opening = asyncio.create_task(service(factory).open(EXECUTOR, title, "b", anchors))
                tasks.append(opening)
                await wait_until_blocked_on(
                    factory, holder_pid, statement="pg_advisory_xact_lock_shared("
                )
                assert not opening.done()
            with pytest.raises(FocusSlotError) as refused:
                await opening
            assert refused.value.code == "anchor_already_received"
            async with factory() as session:
                assert not await session.scalar(
                    sa.select(sa.func.count())
                    .select_from(focus_slots)
                    .where(focus_slots.c.project_key == EXECUTOR, focus_slots.c.title == title)
                )
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        async with asyncio.timeout(WAIT_SECONDS):
            await asyncio.gather(*tasks, return_exceptions=True)


async def test_a_lot_open_queues_on_the_fence_of_a_newly_integrated_ticket_then_refuses(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The uncommitted integration SHA keeps the writer out of open's lot ticket locks."""
    async with asyncio.timeout(WAIT_SECONDS):
        ticket, binding, _, number = await workflow(session_factory)
        release = f"9.{uuid4().int % 1000}.{uuid4().int % 1000}"
        await plan(session_factory, ticket.id, release)

        async def writer(holder: AsyncSession) -> None:
            await integrate_in(session_factory, holder, ticket.id, binding, number)
            await release_in(holder, ticket.id, f"v{release}")

        await open_behind_new_candidate_receipt(
            session_factory,
            [SlotAnchor(kind="lot", target_release=release)],
            f"lot writer first {uuid4().hex[:6]}",
            writer,
        )


async def test_a_pr_open_queues_on_the_fence_of_a_newly_bound_ticket_then_refuses(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The second ticket's uncommitted PR binding is outside open's PR ticket locks."""
    async with asyncio.timeout(WAIT_SECONDS):
        _, _, _, number = await workflow(session_factory)

        async def writer(holder: AsyncSession) -> None:
            # workflow's commits release savepoints, not the holder's transaction: the
            # second ticket, its binding and its receipt become visible together.
            transaction_factory = async_sessionmaker(
                await holder.connection(),
                expire_on_commit=False,
                join_transaction_mode="create_savepoint",
            )
            ticket, binding, _, _ = await workflow(transaction_factory, pr_number=number)
            await integrate_in(transaction_factory, holder, ticket.id, binding, number)

        await open_behind_new_candidate_receipt(
            session_factory,
            [SlotAnchor(kind="pr", repository_id=RID, pr_number=number)],
            f"pr writer first {uuid4().hex[:6]}",
            writer,
        )


@pytest.mark.parametrize(
    "staled",
    [{"active": False}, {"integration_sha": None}],
    ids=["reopened_binding_deactivated", "rebound_without_sha"],
)
async def test_a_release_from_a_stale_candidate_is_skipped_not_attested(session_factory, staled):
    """The observer picks a candidate, goes to the network, and records in a NEW transaction: the
    binding may have been deactivated or rebound meanwhile. The release must re-check under the
    ticket lock and skip, else it commits an attestation the lot lock of an open never saw."""
    ticket, binding, _, number = await workflow(session_factory)
    release = f"9.{uuid4().int % 1000}.{uuid4().int % 1000}"
    await plan(session_factory, ticket.id, release)
    await integrate(session_factory, ticket.id, binding, number)
    async with session_factory.begin() as session:  # what a reopen or a rebind does
        await session.execute(
            delivery_artifact_bindings.update()
            .where(delivery_artifact_bindings.c.ticket_id == ticket.id)
            .values(**staled)
        )
    anchors = [SlotAnchor(kind="lot", target_release=release)]
    async with session_factory() as holder, holder.begin():
        await release_in(holder, ticket.id, f"v{release}")
        opened = await asyncio.wait_for(
            service(session_factory).open(EXECUTOR, f"stale {uuid4().hex[:6]}", "b", anchors),
            timeout=WAIT_SECONDS,
        )
    async with session_factory() as session:
        row = (
            (
                await session.execute(
                    sa.select(focus_slots).where(focus_slots.c.id == opened.slot.id)
                )
            )
            .mappings()
            .one()
        )
        received = await received_anchor(session, project_key=EXECUTOR, anchor=anchors[0])
        attested = await session.scalar(
            sa.select(sa.func.count())
            .select_from(delivery_attestations)
            .where(delivery_attestations.c.ticket_id == ticket.id)
        )
    assert received is None and attested == 0 and row["closed_at"] is None
