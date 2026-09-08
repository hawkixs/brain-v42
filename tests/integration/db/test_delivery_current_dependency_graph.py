"""Dependency amendments keep history without constraining the current graph."""

from __future__ import annotations

from uuid import UUID

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brain_v42.db.tables import delivery_dependencies, delivery_events, delivery_workflows
from brain_v42.delivery_config import DeliverySettings
from brain_v42.models.delivery import (
    ContractInput,
    ContractRevision,
    Deliverable,
    DeliveryDependency,
    DeliveryError,
    ReviewPolicy,
)
from brain_v42.models.ticket import TicketCreate, TicketKind
from brain_v42.repositories.pg_delivery import PgDeliveryRepo
from brain_v42.repositories.pg_ticket import PgTicketRepo
from brain_v42.services.delivery_service import DeliveryService

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def _ticket(session_factory: async_sessionmaker[AsyncSession], label: str) -> UUID:
    ticket = await PgTicketRepo(session_factory).create(
        TicketCreate(
            kind=TicketKind.REQUEST,
            title=f"current dependency graph {label}",
            body="amend dependencies while retaining the immutable revision history",
            from_project="brain-v42",
            to_project="brain-v42",
        )
    )
    return ticket.id


def _dependency(ticket_id: UUID, revision: int) -> DeliveryDependency:
    return DeliveryDependency(
        ticket_id=ticket_id,
        contract_revision=revision,
        attempt=1,
        milestone="integrated",
    )


async def _set(
    service: DeliveryService,
    ticket_id: UUID,
    revision: int,
    dependencies: tuple[DeliveryDependency, ...] = (),
) -> ContractRevision:
    return await service.set_contract(
        ticket_id,
        actor_project="brain-v42",
        expected_revision=revision - 1,
        idempotency_key=f"current-graph-{ticket_id}-{revision}",
        contract=ContractInput(
            objective="preserve an acyclic current dependency graph",
            priority=1,
            acceptance_mode="automatic",
            dependencies=dependencies,
            deliverables=(
                Deliverable(
                    key="implementation",
                    repository="hawkixs/brain-v42",
                    target_branch="main",
                    required_checks=(),
                    no_checks_reason="database dependency graph regression",
                    review=ReviewPolicy(required_approvals=0, allowed_reviewers=()),
                ),
            ),
        ),
    )


@pytest.mark.parametrize("transitive", [False, True])
async def test_removed_dependency_allows_reversal_but_current_cycles_still_fail(
    session_factory: async_sessionmaker[AsyncSession], transitive: bool
) -> None:
    service = DeliveryService(
        PgDeliveryRepo(session_factory), settings=DeliverySettings(enabled=True)
    )
    a = await _ticket(session_factory, "A")
    b = await _ticket(session_factory, "B")
    await _set(service, b, 1)
    await _set(service, a, 1, (_dependency(b, 1),))
    await _set(service, a, 2)

    upstream = _dependency(a, 2)
    if transitive:
        c = await _ticket(session_factory, "C")
        await _set(service, c, 1, (upstream,))
        upstream = _dependency(c, 1)

    # Current A has no outgoing edge. The old A-v1 -> B edge must not prevent
    # B -> A, or the transitive B -> C -> A variant, from being admitted.
    stored = await _set(service, b, 2, (upstream,))
    assert stored.contract_revision == 2
    assert stored.dependencies == (upstream,)

    # Reintroducing A -> B would close the current graph's cycle. Its refusal
    # must leave A-v2 current and preserve A-v1's historical edge verbatim.
    with pytest.raises(DeliveryError, match="dependency_cycle"):
        await _set(service, a, 3, (_dependency(b, 2),))

    async with session_factory() as session:
        assert (
            await session.scalar(
                sa.select(delivery_workflows.c.current_revision).where(
                    delivery_workflows.c.ticket_id == a
                )
            )
            == 2
        )
        retained_edges = (
            await session.execute(
                sa.select(
                    delivery_dependencies.c.contract_revision,
                    delivery_dependencies.c.upstream_ticket_id,
                    delivery_dependencies.c.upstream_revision,
                ).where(delivery_dependencies.c.ticket_id == a)
            )
        ).all()
        assert retained_edges == [(1, b, 1)]
        assert (
            await session.scalar(
                sa.select(sa.func.count())
                .select_from(delivery_events)
                .where(delivery_events.c.ticket_id == a)
            )
            == 2
        )
