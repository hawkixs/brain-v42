"""Real PostgreSQL RED races between delivery attachment and legacy ticket closure."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from unittest.mock import MagicMock

import pytest
import sqlalchemy as sa

from brain_v42.db.tables import delivery_workflows
from brain_v42.delivery_config import DeliverySettings
from brain_v42.models.delivery import DeliveryError
from brain_v42.models.ticket import TicketCreate, TicketKind, TicketStatus
from brain_v42.repositories.pg_delivery import PgDeliveryRepo
from brain_v42.repositories.pg_ticket import PgTicketRepo
from brain_v42.services.delivery_service import DeliveryService
from brain_v42.services.ticket_service import TicketService, TicketTransitionConflictError

from .test_delivery_receipt_issuance import RID, _contract

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


def _settings() -> DeliverySettings:
    return DeliverySettings(
        enabled=True,
        freshness_seconds=3600,
        repository_registry={"executor": {RID: "hawkixs/brain-v42"}},
    )


async def _ticket(factory, *, self_ticket: bool = False):
    return await PgTicketRepo(factory).create(
        TicketCreate(
            kind=TicketKind.REQUEST,
            title="attachment race",
            body="a real delivery setter races real lifecycle closure",
            from_project="executor" if self_ticket else "requester",
            to_project="executor",
        )
    )


async def _status(factory, ticket_id):
    ticket = await PgTicketRepo(factory).get_by_id(ticket_id)
    assert ticket is not None
    return ticket.status


async def _workflow_count(factory, ticket_id) -> int:
    async with factory() as session:
        return int(
            await session.scalar(
                sa.select(sa.func.count())
                .select_from(delivery_workflows)
                .where(delivery_workflows.c.ticket_id == ticket_id)
            )
            or 0
        )


async def _wait_for_blocking_close(observer, *, setter_pid: int, close) -> None:
    """Use a third PostgreSQL connection to prove the closer waits on the setter."""
    for _ in range(100):
        pids = list(
            await observer.scalars(
                sa.text(
                    "SELECT pid FROM pg_stat_activity "
                    "WHERE datname = current_database() "
                    "AND pid <> pg_backend_pid() "
                    "AND :setter_pid = ANY(pg_blocking_pids(pid))"
                ),
                {"setter_pid": setter_pid},
            )
        )
        if pids:
            assert len(pids) == 1
            assert not close.done()
            return
        await observer.commit()
        await asyncio.sleep(0.01)
    raise AssertionError("third PostgreSQL connection did not observe the close lock wait")


class _GateAfterOuterDeliveryLock(PgDeliveryRepo):
    def __init__(self, factory, entered: asyncio.Event, release: asyncio.Event) -> None:
        super().__init__(factory)
        self._entered = entered
        self._release = release
        self.pid: int | None = None

    async def set_contract(self, *args, **kwargs):
        session = kwargs["session"]
        self.pid = await session.scalar(sa.text("SELECT pg_backend_pid()"))
        self._entered.set()
        await self._release.wait()
        return await super().set_contract(*args, **kwargs)


async def test_close_first_makes_waiting_real_setter_refuse_terminal_ticket(
    session_factory, monkeypatch
):
    """The setter must re-read status under its lock after its old pre-lock view becomes stale."""
    ticket = await _ticket(session_factory, self_ticket=True)
    service = DeliveryService(PgDeliveryRepo(session_factory), settings=_settings())
    entered = asyncio.Event()
    release = asyncio.Event()
    import brain_v42.services.delivery_service as delivery_service_module

    original_lock = delivery_service_module.lock_workflows

    @asynccontextmanager
    async def pause_before_real_lock(*args, **kwargs):
        entered.set()
        await release.wait()
        async with original_lock(*args, **kwargs):
            yield

    monkeypatch.setattr(delivery_service_module, "lock_workflows", pause_before_real_lock)
    setter = asyncio.create_task(
        service.set_contract(
            ticket.id,
            actor_project="executor",
            expected_revision=0,
            idempotency_key=f"close-first-{ticket.id}",
            contract=_contract(),
        )
    )
    await asyncio.wait_for(entered.wait(), timeout=5)
    try:
        closed = await TicketService(PgTicketRepo(session_factory), MagicMock()).transition(
            ticket.id, "executor", "resolve", message="close wins"
        )
        assert closed.status is TicketStatus.CLOSED
        release.set()
        with pytest.raises(DeliveryError, match="ticket_not_contractable"):
            await setter
        assert await _status(session_factory, ticket.id) is TicketStatus.CLOSED
        assert await _workflow_count(session_factory, ticket.id) == 0
    finally:
        release.set()
        if not setter.done():
            setter.cancel()
        with suppress(asyncio.CancelledError, DeliveryError):
            await setter


async def test_attach_first_makes_waiting_legacy_close_enforce_the_new_contract(session_factory):
    """A close begun behind the actual attachment lock must observe the committed workflow."""
    ticket = await _ticket(session_factory, self_ticket=True)
    entered = asyncio.Event()
    release = asyncio.Event()
    repo = _GateAfterOuterDeliveryLock(session_factory, entered, release)
    service = DeliveryService(repo, settings=_settings())
    setter = asyncio.create_task(
        service.set_contract(
            ticket.id,
            actor_project="executor",
            expected_revision=0,
            idempotency_key=f"attach-first-{ticket.id}",
            contract=_contract(),
        )
    )
    await asyncio.wait_for(entered.wait(), timeout=5)
    close = asyncio.create_task(
        TicketService(PgTicketRepo(session_factory), MagicMock()).transition(
            ticket.id, "executor", "resolve", message="requires fulfillment"
        )
    )
    try:
        assert isinstance(repo.pid, int)
        async with session_factory() as observer:
            await _wait_for_blocking_close(observer, setter_pid=repo.pid, close=close)
        release.set()
        await setter
        with pytest.raises(
            TicketTransitionConflictError, match="delivery_requirements_unsatisfied"
        ):
            await close
        assert await _status(session_factory, ticket.id) is TicketStatus.OPEN
        assert await _workflow_count(session_factory, ticket.id) == 1
    finally:
        release.set()
        for task in (setter, close):
            if not task.done():
                task.cancel()
        for task in (setter, close):
            with suppress(asyncio.CancelledError, DeliveryError, TicketTransitionConflictError):
                await task
