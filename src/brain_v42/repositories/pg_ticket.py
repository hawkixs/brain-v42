"""PostgreSQL repository for tickets + ticket_messages (coordination family)."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

import sqlalchemy as sa
import structlog

from brain_v42.db.tables import ticket_messages, tickets
from brain_v42.models.ticket import (
    ExtractionStatus,
    Ticket,
    TicketCreate,
    TicketGroups,
    TicketMessage,
    TicketStatus,
)
from brain_v42.repositories.pg_base import BasePgRepository

logger = structlog.get_logger(__name__)

_ACTIONABLE = ("open", "in_progress")
_CONFIRMABLE = ("resolved", "wontfix")


class PgTicketRepo(BasePgRepository):
    table = tickets
    fts_columns: list[str] = []  # hors recherche — famille coordination (spec §1)

    async def create(self, data: TicketCreate) -> Ticket:  # type: ignore[override]
        values = {
            "kind": data.kind.value,
            "title": data.title,
            "body": data.body,
            "from_project": data.from_project,
            "to_project": data.to_project,
            "extraction_status": (data.extraction_status.value if data.extraction_status else None),
        }
        async with self.get_session() as session:
            async with session.begin():
                stmt = tickets.insert().values(**values).returning(tickets)
                row = (await session.execute(stmt)).mappings().one()
                logger.debug("pg_ticket.create", ticket_id=str(row["id"]))
                return Ticket.model_validate(dict(row))

    async def get_by_id(self, ticket_id: UUID) -> Ticket | None:  # type: ignore[override]
        async with self.get_session() as session:
            stmt = sa.select(tickets).where(tickets.c.id == ticket_id)
            row = (await session.execute(stmt)).mappings().first()
            return Ticket.model_validate(dict(row)) if row else None

    async def get_messages(self, ticket_id: UUID) -> list[TicketMessage]:
        async with self.get_session() as session:
            stmt = (
                sa.select(ticket_messages)
                .where(ticket_messages.c.ticket_id == ticket_id)
                .order_by(ticket_messages.c.created_at.asc())
            )
            rows = (await session.execute(stmt)).mappings().all()
            return [TicketMessage.model_validate(dict(r)) for r in rows]

    async def add_message(
        self,
        ticket_id: UUID,
        author_project: str,
        body: str,
        status_to: TicketStatus | None = None,
        new_ticket_body: str | None = None,
    ) -> TicketMessage:
        """Insert a thread message; optionally rewrite the ticket body with it.

        ``new_ticket_body`` is applied in the SAME transaction as the message.
        The two must never diverge: a body rewritten without its thread entry
        is a silent rewrite of history, and a thread entry claiming a rewrite
        that did not land is worse — it makes the record lie.
        """
        async with self.get_session() as session:
            async with session.begin():
                stmt = (
                    ticket_messages.insert()
                    .values(
                        ticket_id=ticket_id,
                        author_project=author_project,
                        body=body,
                        status_to=status_to.value if status_to else None,
                    )
                    .returning(ticket_messages)
                )
                row = (await session.execute(stmt)).mappings().one()
                # A reply is activity: bump the ticket's updated_at. An
                # amendment adds the body to it, in the same UPDATE — hence in
                # the same transaction as the message that reports it.
                ticket_values: dict[str, Any] = {"updated_at": sa.func.now()}
                if new_ticket_body is not None:
                    ticket_values["body"] = new_ticket_body
                await session.execute(
                    tickets.update().where(tickets.c.id == ticket_id).values(**ticket_values)
                )
                return TicketMessage.model_validate(dict(row))

    async def apply_transition(
        self,
        ticket_id: UUID,
        new_status: TicketStatus,
        *,
        expected_status: TicketStatus,
        resolved_at: datetime | None,
        closed_at: datetime | None,
        extraction_status: ExtractionStatus | None,
        message_author: str | None = None,
        message_body: str | None = None,
    ) -> Ticket | None:
        if (message_author is None) != (message_body is None):
            raise ValueError("message_author and message_body must be provided together")

        async with self.get_session() as session:
            async with session.begin():
                stmt = (
                    tickets.update()
                    .where(
                        tickets.c.id == ticket_id,
                        tickets.c.status == expected_status.value,
                    )
                    .values(
                        status=new_status.value,
                        resolved_at=resolved_at,
                        closed_at=closed_at,
                        extraction_status=(extraction_status.value if extraction_status else None),
                        updated_at=sa.func.now(),
                    )
                    .returning(tickets)
                )
                row = (await session.execute(stmt)).mappings().one_or_none()
                if row is None:
                    return None
                if message_author is not None and message_body is not None:
                    await session.execute(
                        ticket_messages.insert().values(
                            ticket_id=ticket_id,
                            author_project=message_author,
                            body=message_body,
                            status_to=new_status.value,
                        )
                    )
                logger.info(
                    "pg_ticket.transition",
                    ticket_id=str(ticket_id),
                    new_status=new_status.value,
                )
                return Ticket.model_validate(dict(row))

    async def list_grouped(self, project_key: str) -> TicketGroups:
        async with self.get_session() as session:

            def _q(col: sa.Column, statuses: tuple[str, ...]) -> sa.Select:
                return (
                    sa.select(tickets)
                    .where(col == project_key, tickets.c.status.in_(statuses))
                    .order_by(
                        tickets.c.updated_at.desc(),
                        tickets.c.created_at.desc(),
                        tickets.c.id.asc(),
                    )
                )

            a_traiter = (
                (await session.execute(_q(tickets.c.to_project, _ACTIONABLE))).mappings().all()
            )
            a_confirmer = (
                (await session.execute(_q(tickets.c.from_project, _CONFIRMABLE))).mappings().all()
            )
            en_attente = (
                (
                    await session.execute(
                        _q(tickets.c.from_project, _ACTIONABLE).where(
                            tickets.c.from_project != tickets.c.to_project
                        )
                    )
                )
                .mappings()
                .all()
            )
            # Mirror of en_attente: we delivered (resolved/wontfix), the
            # requester has not confirmed. The self-ticket exclusion is not
            # cosmetic — without it, a resolved self-ticket would appear twice,
            # in a_confirmer AND here (spec 2026-08-03 §2.1).
            awaiting_requester_confirmation = (
                (
                    await session.execute(
                        _q(tickets.c.to_project, _CONFIRMABLE).where(
                            tickets.c.from_project != tickets.c.to_project
                        )
                    )
                )
                .mappings()
                .all()
            )
            return TicketGroups(
                a_traiter=[Ticket.model_validate(dict(r)) for r in a_traiter],
                a_confirmer=[Ticket.model_validate(dict(r)) for r in a_confirmer],
                en_attente=[Ticket.model_validate(dict(r)) for r in en_attente],
                awaiting_requester_confirmation=[
                    Ticket.model_validate(dict(r)) for r in awaiting_requester_confirmation
                ],
            )
