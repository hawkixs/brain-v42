"""A legacy relative `plan_scan_paths` must not block work on an unrelated field.

Ticket 2f913741: red-gift carried `docs/plans`, written before the write-side
validator existed. `ProjectContext` refused it on READ, and `get_by_key` is the
existence check of `brain_ticket_create` and of every knowledge write, so one
stale entry in one unrelated column shut the project to all of them.

The row is inserted straight into the table, bypassing the model: that is the
only way a legacy value can exist.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brain_v42.db.tables import project_contexts, tickets
from brain_v42.models.ticket import TicketCreate, TicketKind
from brain_v42.repositories.pg_project_context import PgProjectContextRepo
from brain_v42.repositories.pg_ticket import PgTicketRepo
from brain_v42.services.ticket_service import TicketService

pytestmark = pytest.mark.integration


async def test_ticket_creation_survives_a_legacy_relative_scan_path(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    project_key = f"integ-legacy-scan-{uuid4().hex[:8]}"
    async with session_factory.begin() as session:
        await session.execute(
            project_contexts.insert().values(
                project_key=project_key,
                name="Legacy scan path",
                description="relative plan_scan_paths written before the validator",
                plan_scan_paths=["docs/plans"],
            )
        )

    try:
        service = TicketService(
            PgTicketRepo(session_factory), PgProjectContextRepo(session_factory)
        )

        ticket = await service.create(
            TicketCreate(
                kind=TicketKind.FYI,
                title="note to self",
                body="must not be blocked by an unrelated legacy field",
                from_project=project_key,
                to_project=project_key,
            )
        )

        assert ticket.to_project == project_key
    finally:
        async with session_factory.begin() as session:
            await session.execute(sa.delete(tickets).where(tickets.c.from_project == project_key))
            await session.execute(
                sa.delete(project_contexts).where(project_contexts.c.project_key == project_key)
            )
