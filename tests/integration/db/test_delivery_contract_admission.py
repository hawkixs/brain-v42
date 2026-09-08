"""Admission rules for the first delivery contract."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import sqlalchemy as sa

from brain_v42.db.tables import (
    delivery_contract_revisions,
    delivery_events,
    delivery_workflows,
    tickets,
)
from brain_v42.delivery_config import DeliverySettings
from brain_v42.models.delivery import (
    ContractInput,
    ContractRevision,
    Deliverable,
    DeliveryError,
    ReviewPolicy,
)
from brain_v42.models.delivery_hashes import canonical_digest
from brain_v42.models.ticket import TicketCreate, TicketKind, TicketStatus
from brain_v42.repositories.pg_delivery import PgDeliveryRepo
from brain_v42.repositories.pg_ticket import PgTicketRepo
from brain_v42.services.delivery_service import DeliveryService

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


def _contract() -> ContractInput:
    return ContractInput(
        objective="admission regression",
        priority=1,
        acceptance_mode="automatic",
        deliverables=(
            Deliverable(
                key="implementation",
                repository="hawkixs/brain-v42",
                target_branch="main",
                required_checks=(),
                no_checks_reason="covered by this integration test",
                review=ReviewPolicy(required_approvals=0, allowed_reviewers=()),
            ),
        ),
    )


async def _ticket_at_status(session_factory, status: TicketStatus):
    ticket = await PgTicketRepo(session_factory).create(
        TicketCreate(
            kind=TicketKind.REQUEST,
            title="contract admission",
            body="first contract must precede completion",
            from_project="brain-v42",
            to_project="brain-v42",
        )
    )
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                tickets.update().where(tickets.c.id == ticket.id).values(status=status.value)
            )
    return ticket


async def _delivery_row_counts(session_factory, ticket_id) -> tuple[int, int, int]:
    async with session_factory() as session:
        counts = []
        for table in (delivery_workflows, delivery_contract_revisions, delivery_events):
            count = await session.scalar(
                sa.select(sa.func.count()).select_from(table).where(table.c.ticket_id == ticket_id)
            )
            counts.append(int(count))
        return tuple(counts)


async def _workflow_state(session_factory, ticket_id) -> dict[str, object]:
    async with session_factory() as session:
        workflow = (
            (
                await session.execute(
                    sa.select(delivery_workflows).where(delivery_workflows.c.ticket_id == ticket_id)
                )
            )
            .mappings()
            .one()
        )
    return {
        field: workflow[field]
        for field in (
            "current_revision",
            "row_version",
            "claim_owner",
            "claim_kind",
            "claim_digest",
            "claim_expires_at",
            "claim_epoch",
        )
    }


def _revision(ticket_id) -> ContractRevision:
    contract = _contract()
    payload = contract.model_dump(mode="python")
    payload["deliverables"] = (contract.deliverables[0].model_copy(update={"repository_id": 1}),)
    return ContractRevision(
        **payload,
        ticket_id=ticket_id,
        contract_revision=1,
        author_project="brain-v42",
        created_at=datetime.now(UTC),
    )


def _amendment(first: ContractRevision) -> ContractRevision:
    payload = first.model_dump(mode="python")
    payload.update(
        contract_revision=2,
        objective="amendment must remain blocked after terminal closure",
        amendment_reason="terminal admission regression",
        content_digest=None,
    )
    return ContractRevision(**payload)


async def _contracted_ticket_at_status(session_factory, status: TicketStatus):
    ticket = await _ticket_at_status(session_factory, TicketStatus.OPEN)
    service = DeliveryService(
        PgDeliveryRepo(session_factory), settings=DeliverySettings(enabled=True)
    )
    first = await service.set_contract(
        ticket.id,
        actor_project="brain-v42",
        contract=_contract(),
        expected_revision=0,
        idempotency_key=f"first-before-terminal-{ticket.id}",
    )
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                tickets.update().where(tickets.c.id == ticket.id).values(status=status.value)
            )
    return ticket, service, first


@pytest.mark.parametrize("status", (TicketStatus.RESOLVED, TicketStatus.WONTFIX))
async def test_service_rejects_first_contract_after_execution_without_writes(
    session_factory, status: TicketStatus
) -> None:
    ticket = await _ticket_at_status(session_factory, status)
    service = DeliveryService(
        PgDeliveryRepo(session_factory), settings=DeliverySettings(enabled=True)
    )

    with pytest.raises(DeliveryError, match="ticket_not_contractable"):
        await service.set_contract(
            ticket.id,
            actor_project="brain-v42",
            contract=_contract(),
            expected_revision=0,
            idempotency_key=f"service-first-{status.value}-{ticket.id}",
        )

    assert await _delivery_row_counts(session_factory, ticket.id) == (0, 0, 0)


@pytest.mark.parametrize("status", (TicketStatus.RESOLVED, TicketStatus.WONTFIX))
async def test_repository_rejects_first_contract_after_execution_without_writes(
    session_factory, status: TicketStatus
) -> None:
    ticket = await _ticket_at_status(session_factory, status)
    repo = PgDeliveryRepo(session_factory)

    with pytest.raises(DeliveryError, match="ticket_not_contractable"):
        await repo.set_contract(
            _revision(ticket.id),
            expected_revision=0,
            actor_project="brain-v42",
            idempotency_key=f"repo-first-{status.value}-{ticket.id}",
            request_digest=canonical_digest({"status": status.value}, domain="request"),
        )

    assert await _delivery_row_counts(session_factory, ticket.id) == (0, 0, 0)


@pytest.mark.parametrize("status", (TicketStatus.OPEN, TicketStatus.IN_PROGRESS))
async def test_service_allows_first_contract_while_execution_is_active(
    session_factory, status: TicketStatus
) -> None:
    ticket = await _ticket_at_status(session_factory, status)
    service = DeliveryService(
        PgDeliveryRepo(session_factory), settings=DeliverySettings(enabled=True)
    )

    stored = await service.set_contract(
        ticket.id,
        actor_project="brain-v42",
        contract=_contract(),
        expected_revision=0,
        idempotency_key=f"active-first-{status.value}-{ticket.id}",
    )

    assert stored.contract_revision == 1


async def test_service_replays_existing_first_contract_after_resolution(session_factory) -> None:
    ticket = await _ticket_at_status(session_factory, TicketStatus.OPEN)
    service = DeliveryService(
        PgDeliveryRepo(session_factory), settings=DeliverySettings(enabled=True)
    )
    contract = _contract()
    idempotency_key = f"replay-first-{ticket.id}"
    first = await service.set_contract(
        ticket.id,
        actor_project="brain-v42",
        contract=contract,
        expected_revision=0,
        idempotency_key=idempotency_key,
    )
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                tickets.update()
                .where(tickets.c.id == ticket.id)
                .values(status=TicketStatus.RESOLVED.value)
            )

    replay = await service.set_contract(
        ticket.id,
        actor_project="brain-v42",
        contract=contract,
        expected_revision=0,
        idempotency_key=idempotency_key,
    )

    assert replay == first
    assert await _delivery_row_counts(session_factory, ticket.id) == (1, 1, 1)


@pytest.mark.parametrize("status", (TicketStatus.CLOSED, TicketStatus.ACKED))
async def test_service_rejects_amendment_after_terminal_closure_without_writes(
    session_factory, status: TicketStatus
) -> None:
    ticket, service, first = await _contracted_ticket_at_status(session_factory, status)
    before_counts = await _delivery_row_counts(session_factory, ticket.id)
    before_workflow = await _workflow_state(session_factory, ticket.id)

    with pytest.raises(DeliveryError, match="ticket_not_contractable"):
        await service.set_contract(
            ticket.id,
            actor_project="brain-v42",
            contract=_contract().model_copy(
                update={"objective": "terminal amendment must be rejected"}
            ),
            expected_revision=first.contract_revision,
            idempotency_key=f"service-amend-{status.value}-{ticket.id}",
            reason="terminal admission regression",
        )

    assert await _delivery_row_counts(session_factory, ticket.id) == before_counts
    assert await _workflow_state(session_factory, ticket.id) == before_workflow


@pytest.mark.parametrize("status", (TicketStatus.CLOSED, TicketStatus.ACKED))
async def test_repository_rejects_amendment_after_terminal_closure_without_writes(
    session_factory, status: TicketStatus
) -> None:
    ticket, _, first = await _contracted_ticket_at_status(session_factory, status)
    repo = PgDeliveryRepo(session_factory)
    before_counts = await _delivery_row_counts(session_factory, ticket.id)
    before_workflow = await _workflow_state(session_factory, ticket.id)

    with pytest.raises(DeliveryError, match="ticket_not_contractable"):
        await repo.set_contract(
            _amendment(first),
            expected_revision=first.contract_revision,
            actor_project="brain-v42",
            idempotency_key=f"repo-amend-{status.value}-{ticket.id}",
            request_digest=canonical_digest(
                {"status": status.value, "amendment": True}, domain="request"
            ),
        )

    assert await _delivery_row_counts(session_factory, ticket.id) == before_counts
    assert await _workflow_state(session_factory, ticket.id) == before_workflow
