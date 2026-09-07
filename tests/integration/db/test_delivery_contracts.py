"""PostgreSQL persistence contracts for versioned delivery workflows."""

from __future__ import annotations

import pytest

from brain_v42.models.delivery import ContractInput, Deliverable, ReviewPolicy
from brain_v42.models.ticket import TicketCreate, TicketKind
from brain_v42.repositories.pg_ticket import PgTicketRepo


@pytest.mark.asyncio
async def test_delivery_schema_exposes_all_eight_workflow_tables(session_factory) -> None:
    """The production change that fails this is omitting any persisted workflow family."""
    from brain_v42.db.delivery_tables import DELIVERY_TABLE_NAMES

    async with session_factory() as session:
        table_names = set(
            (
                await session.execute(
                    __import__("sqlalchemy").text(
                        "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
                    )
                )
            ).scalars()
        )

    assert set(DELIVERY_TABLE_NAMES) <= table_names


@pytest.mark.asyncio
async def test_stale_contract_revision_cannot_replace_another_revision(session_factory) -> None:
    """A stale compare-and-swap must leave the first immutable revision current."""
    from brain_v42.delivery_config import DeliverySettings
    from brain_v42.models.delivery import DeliveryError
    from brain_v42.repositories.pg_delivery import PgDeliveryRepo
    from brain_v42.services.delivery_service import DeliveryService

    ticket = await PgTicketRepo(session_factory).create(
        TicketCreate(
            kind=TicketKind.REQUEST,
            title="delivery persistence",
            body="persist this delivery contract",
            from_project="brain-v42",
            to_project="brain-v42",
        )
    )
    service = DeliveryService(
        PgDeliveryRepo(session_factory), settings=DeliverySettings(enabled=True)
    )
    contract = ContractInput(
        objective="persist delivery",
        priority=1,
        acceptance_mode="automatic",
        deliverables=(
            Deliverable(
                key="implementation",
                repository="hawkixs/brain-v42",
                target_branch="main",
                required_checks=(),
                no_checks_reason="covered later",
                review=ReviewPolicy(required_approvals=0, allowed_reviewers=()),
            ),
        ),
    )
    first = await service.set_contract(
        ticket.id,
        actor_project="brain-v42",
        contract=contract,
        expected_revision=0,
        idempotency_key="first",
    )
    with pytest.raises(DeliveryError, match="revision_conflict"):
        await service.set_contract(
            ticket.id,
            actor_project="brain-v42",
            contract=contract,
            expected_revision=0,
            idempotency_key="amend",
        )
    current = await service.get(ticket.id, actor_project="brain-v42")
    assert current.contract.contract_revision == first.contract_revision == 1
