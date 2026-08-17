"""Real-PostgreSQL proof for proposal/entity atomicity and safe retry."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brain_v42.db.tables import decisions, learnings, ticket_extraction_proposals, tickets
from brain_v42.models.ticket import ExtractionStatus, TicketCreate, TicketKind
from brain_v42.repositories.pg_decision import PgDecisionRepo
from brain_v42.repositories.pg_learning import PgLearningRepo
from brain_v42.repositories.pg_ticket import PgTicketRepo
from brain_v42.services.decision_service import DecisionService
from brain_v42.services.learning_service import LearningService
from brain_v42.services.proposal_service import ProposalApplyError, ProposalService

pytestmark = pytest.mark.integration


class _FailAfterEntityInsertProposalService(ProposalService):
    """Inject a finalization failure after the entity insert has executed."""

    async def _mark_ticket_done_if_triaged(
        self,
        session: Any,
        ticket_id: UUID | None,
    ) -> None:
        del session, ticket_id
        raise RuntimeError("injected proposal finalization failure")


@pytest.mark.asyncio
@pytest.mark.parametrize("target_type", ["learning", "decision"])
async def test_failed_finalization_rolls_back_entity_and_retry_creates_once(
    session_factory: async_sessionmaker[AsyncSession],
    target_type: str,
) -> None:
    project_key = f"integ-proposal-atomic-{uuid4().hex[:8]}"
    embedding_service = AsyncMock()
    embedding_service.embed.return_value = [0.1] * 1536
    ticket = await PgTicketRepo(session_factory).create(
        TicketCreate(
            kind=TicketKind.REQUEST,
            title="Atomic proposal integration proof",
            body="The entity and proposal must share one PostgreSQL transaction.",
            from_project=project_key,
            to_project=project_key,
            extraction_status=ExtractionStatus.PROPOSED,
        )
    )
    if target_type == "learning":
        payload = {
            "topic": "Atomic extraction",
            "insight": "A failed proposal finalization must not leave an entity behind.",
            "tags": ["integration", "atomicity"],
        }
        entity_table = learnings
        learning_service: Any = LearningService(
            PgLearningRepo(session_factory),
            embedding_svc=embedding_service,
        )
        decision_service: Any = AsyncMock()
    else:
        payload = {
            "title": "Atomic extraction",
            "description": "Entity and proposal share one commit.",
            "reasoning": "A finalization retry must not duplicate the decision.",
            "tags": ["integration", "atomicity"],
        }
        entity_table = decisions
        learning_service = AsyncMock()
        decision_service = DecisionService(PgDecisionRepo(session_factory), embedding_service)
    async with session_factory.begin() as session:
        proposal_id = (
            await session.execute(
                ticket_extraction_proposals.insert()
                .values(
                    ticket_id=ticket.id,
                    target_type=target_type,
                    target_project=project_key,
                    payload=payload,
                    rationale="integration proof",
                )
                .returning(ticket_extraction_proposals.c.id)
            )
        ).scalar_one()

    failing_service = _FailAfterEntityInsertProposalService(
        session_factory,
        learning_service,
        decision_service,
    )

    with pytest.raises(ProposalApplyError, match="injected proposal finalization failure"):
        await failing_service.apply_ticket_extraction(proposal_id)

    async with session_factory() as observer:
        count_after_failure = (
            await observer.execute(
                sa.select(sa.func.count())
                .select_from(entity_table)
                .where(entity_table.c.project_key == project_key)
            )
        ).scalar_one()
        proposal_status_after_failure = (
            await observer.execute(
                sa.select(ticket_extraction_proposals.c.status).where(
                    ticket_extraction_proposals.c.id == proposal_id
                )
            )
        ).scalar_one()

    assert count_after_failure == 0
    assert proposal_status_after_failure == "proposed"
    embedding_service.embed.assert_not_awaited()

    result = await ProposalService(
        session_factory,
        learning_service,
        decision_service,
    ).apply_ticket_extraction(proposal_id)

    async with session_factory() as observer:
        rows = (
            await observer.execute(
                sa.select(entity_table.c.id, entity_table.c.embedding).where(
                    entity_table.c.project_key == project_key
                )
            )
        ).all()
        proposal_row = (
            await observer.execute(
                sa.select(
                    ticket_extraction_proposals.c.status,
                    ticket_extraction_proposals.c.applied_entity_id,
                ).where(ticket_extraction_proposals.c.id == proposal_id)
            )
        ).one()
        ticket_status = (
            await observer.execute(
                sa.select(tickets.c.extraction_status).where(tickets.c.id == ticket.id)
            )
        ).scalar_one()

    assert [row.id for row in rows] == [result.entity_id]
    assert rows[0].embedding is not None
    assert len(rows[0].embedding) == 1536
    embedding_service.embed.assert_awaited_once()
    assert proposal_row.status == "applied"
    assert proposal_row.applied_entity_id == result.entity_id
    assert ticket_status == "done"
