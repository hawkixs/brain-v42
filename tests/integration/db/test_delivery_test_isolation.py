"""Regression witness for delivery-history isolation in the shared session database."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from brain_v42.db.tables import (
    delivery_contract_revisions,
    delivery_events,
    delivery_receipts,
    delivery_workflows,
)
from brain_v42.delivery_config import DeliverySettings
from brain_v42.models.delivery import ContractInput, Deliverable, ReviewPolicy
from brain_v42.models.ticket import TicketCreate, TicketKind
from brain_v42.repositories.pg_delivery import PgDeliveryRepo
from brain_v42.repositories.pg_ticket import PgTicketRepo
from brain_v42.services.delivery_service import DeliveryService
from tests.integration.db.conftest import _delete_delivery_workflow_graphs

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

_HISTORY_TABLES = (
    delivery_workflows,
    delivery_contract_revisions,
    delivery_receipts,
    delivery_events,
)


@dataclass(slots=True)
class _IsolationWitness:
    session_factory: async_sessionmaker[AsyncSession]
    sentinel_ticket_id: UUID
    owned_ticket_id: UUID | None = None


async def _create_workflow_history(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    label: str,
) -> UUID:
    ticket = await PgTicketRepo(session_factory).create(
        TicketCreate(
            kind=TicketKind.REQUEST,
            title=f"delivery isolation {label} {uuid4()}",
            body="test-owned workflow history",
            from_project="brain-v42",
            to_project="brain-v42",
        )
    )
    service = DeliveryService(
        PgDeliveryRepo(session_factory),
        settings=DeliverySettings(enabled=True),
    )
    await service.set_contract(
        ticket.id,
        actor_project="brain-v42",
        expected_revision=0,
        idempotency_key=f"delivery-isolation-contract-{ticket.id}",
        contract=ContractInput(
            objective=f"preserve {label} history at the isolation boundary",
            priority=1,
            acceptance_mode="automatic",
            deliverables=(
                Deliverable(
                    key="implementation",
                    repository="hawkixs/brain-v42",
                    target_branch="main",
                    required_checks=(),
                    no_checks_reason="isolation witness",
                    review=ReviewPolicy(required_approvals=0, allowed_reviewers=()),
                ),
            ),
        ),
    )
    async with session_factory() as session, session.begin():
        await session.execute(
            delivery_receipts.insert().values(
                ticket_id=ticket.id,
                contract_revision=1,
                attempt=1,
                milestone="integration",
                delivery_digest=("a" if label == "sentinel" else "b") * 64,
                payload={"isolation_witness": label},
                issuer="delivery-isolation-test",
            )
        )
    assert isinstance(ticket.id, UUID)
    return ticket.id


async def _history_counts(
    session_factory: async_sessionmaker[AsyncSession],
    ticket_id: UUID,
) -> dict[str, int]:
    async with session_factory() as session:
        return {
            table.name: int(
                await session.scalar(
                    sa.select(sa.func.count())
                    .select_from(table)
                    .where(table.c.ticket_id == ticket_id)
                )
                or 0
            )
            for table in _HISTORY_TABLES
        }


@pytest_asyncio.fixture(scope="module")
async def preexisting_delivery_history(engine: AsyncEngine) -> AsyncIterator[_IsolationWitness]:
    """Seed history before the function-scoped isolation fixture takes its snapshot."""
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    sentinel_ticket_id = await _create_workflow_history(session_factory, label="sentinel")
    witness = _IsolationWitness(session_factory, sentinel_ticket_id)
    try:
        yield witness
        assert witness.owned_ticket_id is not None
        assert await _history_counts(session_factory, sentinel_ticket_id) == {
            "delivery_workflows": 1,
            "delivery_contract_revisions": 1,
            "delivery_receipts": 1,
            "delivery_events": 1,
        }
        assert await _history_counts(session_factory, witness.owned_ticket_id) == {
            table.name: 0 for table in _HISTORY_TABLES
        }
    finally:
        cleanup_ids = {sentinel_ticket_id}
        if witness.owned_ticket_id is not None:
            cleanup_ids.add(witness.owned_ticket_id)
        await _delete_delivery_workflow_graphs(session_factory, frozenset(cleanup_ids))


async def test_delivery_test_removes_only_the_history_it_created(
    preexisting_delivery_history: _IsolationWitness,
) -> None:
    """The sentinel remains while this test's complete workflow history is removed."""
    witness = preexisting_delivery_history
    witness.owned_ticket_id = await _create_workflow_history(
        witness.session_factory,
        label="owned",
    )
    assert await _history_counts(witness.session_factory, witness.sentinel_ticket_id) == {
        "delivery_workflows": 1,
        "delivery_contract_revisions": 1,
        "delivery_receipts": 1,
        "delivery_events": 1,
    }
    assert await _history_counts(witness.session_factory, witness.owned_ticket_id) == {
        "delivery_workflows": 1,
        "delivery_contract_revisions": 1,
        "delivery_receipts": 1,
        "delivery_events": 1,
    }
