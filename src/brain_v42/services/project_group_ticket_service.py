"""Project-group boundary around the canonical ticket service."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING
from uuid import UUID

import sqlalchemy as sa

from brain_v42.db.tables import project_contexts, tickets
from brain_v42.services.ticket_service import NotAllowedError, TicketNotFoundError

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from brain_v42.models.ticket import Ticket, TicketCreate, TicketMessage
    from brain_v42.services.ticket_service import TicketService


class ProjectGroupTicketService:
    """Hide and reject tickets outside one configured project group."""

    def __init__(
        self,
        service: TicketService,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        project_group: str,
    ) -> None:
        self._service = service
        self._session_factory = session_factory
        self._project_group = project_group

    async def create(self, data: TicketCreate) -> Ticket:
        async with self._participants_scope_fence(
            data.from_project,
            data.to_project,
            actor_project=data.from_project,
        ) as in_scope:
            if not in_scope:
                raise NotAllowedError(
                    f"ticket creator must belong to project group '{self._project_group}'"
                )
            return await self._service.create(data)

    async def reply(self, ticket_id: UUID, author_project: str, body: str) -> TicketMessage:
        async with self._ticket_scope_fence(
            ticket_id,
            actor_project=author_project,
        ) as in_scope:
            self._require_ticket(ticket_id, in_scope=in_scope)
            return await self._service.reply(ticket_id, author_project, body)

    async def transition(
        self,
        ticket_id: UUID,
        author_project: str,
        action: str,
        message: str | None = None,
    ) -> Ticket:
        async with self._ticket_scope_fence(
            ticket_id,
            actor_project=author_project,
        ) as in_scope:
            self._require_ticket(ticket_id, in_scope=in_scope)
            return await self._service.transition(ticket_id, author_project, action, message)

    async def get_with_thread(
        self,
        ticket_id: UUID,
    ) -> tuple[Ticket, list[TicketMessage]] | None:
        async with self._ticket_scope_fence(ticket_id) as in_scope:
            if not in_scope:
                return None
            return await self._service.get_with_thread(ticket_id)

    @asynccontextmanager
    async def _participants_scope_fence(
        self,
        from_project: str,
        to_project: str,
        *,
        actor_project: str | None = None,
    ) -> AsyncIterator[bool]:
        async with self._session_factory.begin() as session:
            yield await self._lock_participants_scope(
                session,
                from_project,
                to_project,
                actor_project=actor_project,
            )

    @asynccontextmanager
    async def _ticket_scope_fence(
        self,
        ticket_id: UUID,
        *,
        actor_project: str | None = None,
    ) -> AsyncIterator[bool]:
        async with self._session_factory.begin() as session:
            participants = (
                await session.execute(
                    sa.select(tickets.c.from_project, tickets.c.to_project)
                    .where(tickets.c.id == ticket_id)
                    .with_for_update(read=True, key_share=True)
                )
            ).one_or_none()
            if participants is None:
                yield False
                return
            yield await self._lock_participants_scope(
                session,
                *participants,
                actor_project=actor_project,
            )

    async def _lock_participants_scope(
        self,
        session: AsyncSession,
        from_project: str,
        to_project: str,
        *,
        actor_project: str | None = None,
    ) -> bool:
        base_key = project_contexts.c.project_key

        def matches(project_key: str) -> sa.ColumnElement[bool]:
            candidate = sa.literal(project_key)
            return sa.or_(
                candidate == base_key,
                sa.and_(
                    base_key.not_like("%:%"),
                    candidate.like(base_key + sa.literal(":%")),
                ),
            )

        participant_match = sa.or_(matches(from_project), matches(to_project))
        scope_match = (
            participant_match
            if actor_project is None
            else sa.or_(participant_match, matches(actor_project))
        )
        scoped_rows = (
            (
                await session.execute(
                    sa.select(base_key)
                    .where(
                        project_contexts.c.project_group == self._project_group,
                        scope_match,
                    )
                    .order_by(base_key)
                    .with_for_update(read=True)
                )
            )
            .scalars()
            .all()
        )
        if not scoped_rows:
            return False

        def in_scope(project_key: str) -> bool:
            return any(
                project_key == base_key
                or (":" not in base_key and project_key.startswith(f"{base_key}:"))
                for base_key in scoped_rows
            )

        if not (in_scope(from_project) or in_scope(to_project)):
            return False
        if actor_project is not None and not in_scope(actor_project):
            return False

        # Keep exact registry rows stable too. TicketService still owns the
        # user-facing unknown-project error when one does not exist.
        await session.execute(
            sa.select(base_key)
            .where(base_key.in_({from_project, to_project}))
            .order_by(base_key)
            .with_for_update(read=True)
        )
        return True

    @staticmethod
    def _require_ticket(ticket_id: UUID, *, in_scope: bool) -> None:
        if not in_scope:
            raise TicketNotFoundError(f"Ticket '{ticket_id}' not found")
