"""PostgreSQL persistence contracts for versioned delivery workflows."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa

from brain_v42.db.tables import decisions, delivery_artifact_bindings, delivery_workflows
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
        idempotency_key="generation-first",
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


@pytest.mark.asyncio
async def test_binding_requires_a_ticket_participant_and_replaces_the_active_identity(
    session_factory,
) -> None:
    """An outsider cannot replace proof eligibility; a replacement receives a new UUID."""
    from brain_v42.delivery_config import DeliverySettings
    from brain_v42.models.delivery import DeliveryError
    from brain_v42.repositories.pg_delivery import PgDeliveryRepo
    from brain_v42.services.delivery_service import DeliveryService

    ticket = await PgTicketRepo(session_factory).create(
        TicketCreate(
            kind=TicketKind.REQUEST,
            title="delivery binding",
            body="persist this delivery binding",
            from_project="brain-v42",
            to_project="brain-v42",
        )
    )
    service = DeliveryService(
        PgDeliveryRepo(session_factory), settings=DeliverySettings(enabled=True)
    )
    await service.set_contract(
        ticket.id,
        actor_project="brain-v42",
        contract=ContractInput(
            objective="persist binding",
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
        ),
        expected_revision=0,
        idempotency_key="contract",
    )
    with pytest.raises(DeliveryError, match="not_allowed"):
        await service.bind_pr(
            ticket.id,
            actor_project="outside",
            deliverable_key="implementation",
            repository_id=1337360966,
            pr_number=7,
            idempotency_key="outside",
        )
    first = await service.bind_pr(
        ticket.id,
        actor_project="brain-v42",
        deliverable_key="implementation",
        repository_id=1337360966,
        pr_number=7,
        idempotency_key="context-generation-first",
    )
    second = await service.bind_pr(
        ticket.id,
        actor_project="brain-v42",
        deliverable_key="implementation",
        repository_id=1337360966,
        pr_number=8,
        idempotency_key="generation-second",
    )
    assert first.id != second.id


@pytest.mark.asyncio
async def test_contract_pins_brain_context_and_view_reports_a_later_content_drift(
    session_factory,
) -> None:
    """Changing a pinned semantic field must become a `changed` context predicate."""
    from brain_v42.delivery_config import DeliverySettings
    from brain_v42.repositories.pg_delivery import PgDeliveryRepo
    from brain_v42.services.delivery_service import DeliveryService

    async with session_factory() as session:
        async with session.begin():
            decision_id = (
                await session.execute(
                    decisions.insert()
                    .values(title="decision", description="before", reasoning="because")
                    .returning(decisions.c.id)
                )
            ).scalar_one()
    ticket = await PgTicketRepo(session_factory).create(
        TicketCreate(
            kind=TicketKind.REQUEST,
            title="context",
            body="pin decision",
            from_project="brain-v42",
            to_project="brain-v42",
        )
    )
    service = DeliveryService(
        PgDeliveryRepo(session_factory), settings=DeliverySettings(enabled=True)
    )
    await service.set_contract(
        ticket.id,
        actor_project="brain-v42",
        expected_revision=0,
        idempotency_key="context",
        contract=ContractInput(
            objective="pin source",
            priority=1,
            acceptance_mode="automatic",
            context_refs=(
                {
                    "kind": "brain_entity",
                    "entity_type": "decision",
                    "entity_id": decision_id,
                    "required": True,
                },
            ),
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
        ),
    )
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                sa.update(decisions)
                .where(decisions.c.id == decision_id)
                .values(description="after")
            )
    view = await service.get(ticket.id, actor_project="brain-v42")
    assert view.contexts[0].status == "changed"


@pytest.mark.asyncio
async def test_binding_persists_canonical_registry_name_and_replays_its_uuid(
    session_factory,
) -> None:
    """Numeric repository identity binds the configured name and a retry is immutable."""
    from brain_v42.delivery_config import DeliverySettings
    from brain_v42.repositories.pg_delivery import PgDeliveryRepo
    from brain_v42.services.delivery_service import DeliveryService

    ticket = await PgTicketRepo(session_factory).create(
        TicketCreate(
            kind=TicketKind.REQUEST,
            title="binding identity",
            body="persist canonical repository identity",
            from_project="requester",
            to_project="executor",
        )
    )
    service = DeliveryService(
        PgDeliveryRepo(session_factory),
        settings=DeliverySettings(
            enabled=True,
            repository_registry={"executor": {7: "example/executor-repo"}},
        ),
    )
    await service.set_contract(
        ticket.id,
        actor_project="requester",
        expected_revision=0,
        idempotency_key="refresh-contract",
        contract=ContractInput(
            objective="bind named repository",
            priority=1,
            acceptance_mode="automatic",
            deliverables=(
                Deliverable(
                    key="implementation",
                    repository="example/executor-repo",
                    target_branch="main",
                    required_checks=(),
                    no_checks_reason="covered later",
                    review=ReviewPolicy(required_approvals=0, allowed_reviewers=()),
                ),
            ),
        ),
    )
    first = await service.bind_pr(
        ticket.id,
        actor_project="executor",
        deliverable_key="implementation",
        repository_id=7,
        pr_number=12,
        idempotency_key="same-request",
    )
    replay = await service.bind_pr(
        ticket.id,
        actor_project="executor",
        deliverable_key="implementation",
        repository_id=7,
        pr_number=12,
        idempotency_key="same-request",
    )
    async with session_factory() as session:
        name = (
            await session.execute(
                sa.select(delivery_artifact_bindings.c.repository_name).where(
                    delivery_artifact_bindings.c.id == first.id
                )
            )
        ).scalar_one()
    assert replay.id == first.id
    assert name == "example/executor-repo"


@pytest.mark.asyncio
async def test_concurrent_identical_contract_requests_replay_after_the_graph_lock(
    session_factory,
) -> None:
    """The second writer must recheck its key after waiting for the graph writer."""
    from brain_v42.delivery_config import DeliverySettings
    from brain_v42.repositories.pg_delivery import PgDeliveryRepo
    from brain_v42.services.delivery_service import DeliveryService

    ticket = await PgTicketRepo(session_factory).create(
        TicketCreate(
            kind=TicketKind.REQUEST,
            title="concurrent replay",
            body="serialize one immutable request",
            from_project="brain-v42",
            to_project="brain-v42",
        )
    )
    service = DeliveryService(
        PgDeliveryRepo(session_factory), settings=DeliverySettings(enabled=True)
    )
    contract = ContractInput(
        objective="one revision",
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
    results = await asyncio.gather(
        *(
            service.set_contract(
                ticket.id,
                actor_project="brain-v42",
                contract=contract,
                expected_revision=0,
                idempotency_key="concurrent",
            )
            for _ in range(2)
        )
    )
    assert [result.contract_revision for result in results] == [1, 1]


@pytest.mark.asyncio
async def test_amendment_invalidates_the_repository_context_publication_token(
    session_factory,
) -> None:
    """A new contract generation fences a late repository-context publication."""
    from brain_v42.delivery_config import DeliverySettings
    from brain_v42.repositories.pg_delivery import PgDeliveryRepo
    from brain_v42.services.delivery_service import DeliveryService

    ticket = await PgTicketRepo(session_factory).create(
        TicketCreate(
            kind=TicketKind.REQUEST,
            title="context generation",
            body="amend context token",
            from_project="brain-v42",
            to_project="brain-v42",
        )
    )
    service = DeliveryService(
        PgDeliveryRepo(session_factory), settings=DeliverySettings(enabled=True)
    )

    def contract(objective: str) -> ContractInput:
        return ContractInput(
            objective=objective,
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

    await service.set_contract(
        ticket.id,
        actor_project="brain-v42",
        contract=contract("first"),
        expected_revision=0,
        idempotency_key="first",
    )
    await service.set_contract(
        ticket.id,
        actor_project="brain-v42",
        contract=contract("second"),
        expected_revision=1,
        idempotency_key="context-generation-second",
    )
    async with session_factory() as session:
        versions = (
            await session.execute(
                sa.select(
                    delivery_workflows.c.row_version, delivery_workflows.c.context_row_version
                ).where(delivery_workflows.c.ticket_id == ticket.id)
            )
        ).one()
    assert versions == (2, 2)


@pytest.mark.asyncio
async def test_refresh_marks_context_and_active_bindings_due_without_changing_versions(
    session_factory,
) -> None:
    """Refresh queues both subjects for observation; scheduling is not an evidence publication."""
    from brain_v42.delivery_config import DeliverySettings
    from brain_v42.repositories.pg_delivery import PgDeliveryRepo
    from brain_v42.services.delivery_service import DeliveryService

    ticket = await PgTicketRepo(session_factory).create(
        TicketCreate(
            kind=TicketKind.REQUEST,
            title="refresh subjects",
            body="queue every active observation subject",
            from_project="brain-v42",
            to_project="brain-v42",
        )
    )
    service = DeliveryService(
        PgDeliveryRepo(session_factory), settings=DeliverySettings(enabled=True)
    )
    await service.set_contract(
        ticket.id,
        actor_project="brain-v42",
        expected_revision=0,
        idempotency_key="refresh-contract-create",
        contract=ContractInput(
            objective="refresh all subjects",
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
        ),
    )
    binding = await service.bind_pr(
        ticket.id,
        actor_project="brain-v42",
        deliverable_key="implementation",
        repository_id=1337360966,
        pr_number=33,
        idempotency_key="refresh-binding",
    )
    future = datetime.now(UTC) + timedelta(days=1)
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                sa.update(delivery_workflows)
                .where(delivery_workflows.c.ticket_id == ticket.id)
                .values(context_due_at=future)
            )
            await session.execute(
                sa.update(delivery_artifact_bindings)
                .where(delivery_artifact_bindings.c.id == binding.id)
                .values(due_at=future)
            )
    await service.refresh(ticket.id, actor_project="brain-v42")
    async with session_factory() as session:
        row = (
            await session.execute(
                sa.select(
                    delivery_workflows.c.context_due_at,
                    delivery_workflows.c.context_row_version,
                    delivery_artifact_bindings.c.due_at,
                    delivery_artifact_bindings.c.row_version,
                )
                .join(
                    delivery_artifact_bindings,
                    delivery_artifact_bindings.c.ticket_id == delivery_workflows.c.ticket_id,
                )
                .where(delivery_workflows.c.ticket_id == ticket.id)
            )
        ).one()
    assert row[0] < future and row[2] < future
    assert row[1] == row[3] == 1
