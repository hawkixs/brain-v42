"""`brain_slot_open` against a receipt that is still uncommitted (ticket 5caae01d).

A receipt committing between open's receipt check and open's own commit cannot see the
uncommitted slot, so its hook closes nothing and the slot would stay open with
`receipt_pending` for good. The writer holds `tickets ... FOR UPDATE`; open must queue
behind it, then see the committed receipt and refuse.

Writes slot rows: private head database, like the other slot-writing modules. The interleaving
is forced, not raced: the receipt transaction stays open while a second connection is observed
blocked on it through `pg_blocking_pids`.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
import sqlalchemy as sa

from brain_v42.db.tables import focus_slots
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


async def wait_until_blocked_on(factory, holder_pid: int) -> None:
    """Return once some backend waits for a lock the holder's transaction owns."""
    async with asyncio.timeout(WAIT_SECONDS):
        while True:
            async with factory() as session:
                blocked = await session.scalar(
                    sa.text(
                        "SELECT count(*) FROM pg_stat_activity WHERE :pid = ANY(pg_blocking_pids(pid))"
                    ),
                    {"pid": holder_pid},
                )
            if blocked:
                return
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
            await wait_until_blocked_on(factory, holder_pid)
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
