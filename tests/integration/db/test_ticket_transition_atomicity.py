"""Real PostgreSQL proofs for atomic ticket transitions."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import MagicMock
from uuid import UUID

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brain_v42.db.tables import tickets
from brain_v42.models.ticket import Ticket, TicketCreate, TicketKind, TicketStatus
from brain_v42.repositories.pg_ticket import PgTicketRepo
from brain_v42.services.ticket_service import TicketError, TicketService

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

FROM, TO = "red-shrik", "red-data"


class _TwoPartyReadBarrier:
    def __init__(self) -> None:
        self._arrived = 0
        self._lock = asyncio.Lock()
        self._open = asyncio.Event()

    async def wait(self) -> None:
        async with self._lock:
            self._arrived += 1
            if self._arrived == 2:
                self._open.set()
        await asyncio.wait_for(self._open.wait(), timeout=5)


class _ReadBarrierTicketRepo(PgTicketRepo):
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        barrier: _TwoPartyReadBarrier,
    ) -> None:
        super().__init__(session_factory)
        self._barrier = barrier

    async def get_by_id(self, ticket_id: UUID) -> Ticket | None:
        ticket = await super().get_by_id(ticket_id)
        await self._barrier.wait()
        return ticket


async def _delete_ticket(
    session_factory: async_sessionmaker[AsyncSession], ticket_id: UUID
) -> None:
    async with session_factory() as session:
        async with session.begin():
            await session.execute(sa.delete(tickets).where(tickets.c.id == ticket_id))


async def test_message_insert_failure_rolls_back_status_update(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repo = PgTicketRepo(session_factory)
    created = await repo.create(
        TicketCreate(
            kind=TicketKind.REQUEST,
            title="COR2 rollback proof",
            body="the message insert must fail after the update",
            from_project=FROM,
            to_project=TO,
        )
    )
    try:
        with pytest.raises(sa.exc.DBAPIError):
            await repo.apply_transition(
                created.id,
                TicketStatus.RESOLVED,
                expected_status=TicketStatus.OPEN,
                resolved_at=datetime.now(UTC),
                closed_at=None,
                extraction_status=None,
                message_author="x" * 51,
                message_body="this insert violates varchar(50)",
            )

        refreshed = await repo.get_by_id(created.id)
        messages = await repo.get_messages(created.id)
        assert refreshed is not None
        assert refreshed.status is TicketStatus.OPEN
        assert refreshed.resolved_at is None
        assert messages == []
    finally:
        await _delete_ticket(session_factory, created.id)


async def test_two_transitions_from_same_read_have_one_matching_winner(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    plain_repo = PgTicketRepo(session_factory)
    created = await plain_repo.create(
        TicketCreate(
            kind=TicketKind.REQUEST,
            title="COR2 concurrency proof",
            body="resolve and cancel race from open",
            from_project=FROM,
            to_project=TO,
        )
    )
    barrier = _TwoPartyReadBarrier()
    resolve_service = TicketService(
        repo=_ReadBarrierTicketRepo(session_factory, barrier),
        project_context_repo=MagicMock(),
    )
    cancel_service = TicketService(
        repo=_ReadBarrierTicketRepo(session_factory, barrier),
        project_context_repo=MagicMock(),
    )
    try:
        outcomes = await asyncio.wait_for(
            asyncio.gather(
                resolve_service.transition(
                    created.id,
                    TO,
                    "resolve",
                    message="resolved winner",
                ),
                cancel_service.transition(
                    created.id,
                    FROM,
                    "cancel",
                    message="cancelled winner",
                ),
                return_exceptions=True,
            ),
            timeout=10,
        )

        winners = [outcome for outcome in outcomes if isinstance(outcome, Ticket)]
        conflicts = [outcome for outcome in outcomes if isinstance(outcome, TicketError)]
        assert len(winners) == 1
        assert len(conflicts) == 1
        assert conflicts[0].__class__.__name__ == "TicketTransitionConflictError"

        final_ticket = await plain_repo.get_by_id(created.id)
        messages = await plain_repo.get_messages(created.id)
        assert final_ticket is not None
        assert len(messages) == 1
        assert winners[0].status is final_ticket.status
        assert messages[0].status_to is final_ticket.status
        expected_body = {
            TicketStatus.RESOLVED: "resolved winner",
            TicketStatus.CLOSED: "cancelled winner",
        }
        assert messages[0].body == expected_body[final_ticket.status]
    finally:
        await _delete_ticket(session_factory, created.id)
