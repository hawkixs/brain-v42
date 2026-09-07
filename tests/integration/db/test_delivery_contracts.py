"""PostgreSQL persistence contracts for versioned delivery workflows."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa

from brain_v42.db.tables import (
    adrs,
    decisions,
    delivery_artifact_bindings,
    delivery_confirmations,
    delivery_snapshots,
    delivery_workflows,
    indexed_plans,
    learnings,
    runbooks,
    snippets,
    tickets,
)
from brain_v42.models.delivery import ContractInput, Deliverable, ReviewPolicy
from brain_v42.models.ticket import TicketCreate, TicketKind
from brain_v42.repositories.pg_ticket import PgTicketRepo
from tests.integration.disposable_db import fresh_head_database

_PROJECT_ROOT = Path(__file__).resolve().parents[3]

_CONTEXT_CASES = (
    (
        "decision",
        decisions,
        {"title": "decision", "description": "before", "reasoning": "why"},
        "description",
    ),
    ("learning", learnings, {"topic": "topic", "insight": "before"}, "insight"),
    (
        "snippet",
        snippets,
        {"title": "snippet", "intention": "use", "code": "before", "language": "python"},
        "code",
    ),
    (
        "runbook",
        runbooks,
        {
            "title": "runbook",
            "description": "before",
            "project_key": "brain-v42",
            "trigger": "manual",
        },
        "description",
    ),
    (
        "adr",
        adrs,
        {
            "number": 9203,
            "title": "adr",
            "context": "context",
            "decision": "before",
            "consequences": "consequences",
            "project_key": "brain-v42",
        },
        "decision",
    ),
    (
        "plan",
        indexed_plans,
        {
            "file_path": "plans/delivery-context-9203.md",
            "title": "plan",
            "plan_type": "plan",
            "project_key": "brain-v42",
            "content_hash": "a" * 64,
            "content": "before",
        },
        "content",
    ),
)


async def _wait_for_blocker(
    observer, *, waiter_pid: int, blocker_pid: int, operation: asyncio.Task[object]
) -> None:
    """Prove a real PostgreSQL wait edge instead of guessing from elapsed time."""
    for _ in range(100):
        blockers = await observer.scalar(
            sa.text("SELECT pg_blocking_pids(:pid)"), {"pid": waiter_pid}
        )
        if blocker_pid in blockers:
            assert not operation.done()
            return
        if operation.done():
            await operation
            raise AssertionError("contender completed without waiting on the expected lock")
        await asyncio.sleep(0.01)
    raise AssertionError("PostgreSQL did not expose the expected blocking transaction")


async def _insert_context_decision(session_factory, description: str) -> object:
    async with session_factory() as session:
        async with session.begin():
            return (
                await session.execute(
                    decisions.insert()
                    .values(title="concurrency", description=description, reasoning="test")
                    .returning(decisions.c.id)
                )
            ).scalar_one()


async def _create_bindable_ticket(session_factory):
    from brain_v42.delivery_config import DeliverySettings
    from brain_v42.repositories.pg_delivery import PgDeliveryRepo
    from brain_v42.services.delivery_service import DeliveryService

    ticket = await PgTicketRepo(session_factory).create(
        TicketCreate(
            kind=TicketKind.REQUEST,
            title="concurrent delivery binding",
            body="serialize generation changes",
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
        idempotency_key=f"concurrent-contract-{ticket.id}",
        contract=ContractInput(
            objective="bind with a current generation",
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
    return ticket, service


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
            expected_revision=1,
            expected_workflow_version=1,
            idempotency_key="outside",
        )
    first = await service.bind_pr(
        ticket.id,
        actor_project="brain-v42",
        deliverable_key="implementation",
        repository_id=1337360966,
        pr_number=7,
        expected_revision=1,
        expected_workflow_version=1,
        idempotency_key="context-generation-first",
    )
    second = await service.bind_pr(
        ticket.id,
        actor_project="brain-v42",
        deliverable_key="implementation",
        repository_id=1337360966,
        pr_number=8,
        expected_revision=1,
        expected_workflow_version=2,
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
@pytest.mark.parametrize(("entity_type", "table", "values", "semantic_column"), _CONTEXT_CASES)
async def test_contract_context_digest_tracks_real_semantic_sources_only(
    session_factory, entity_type, table, values, semantic_column
) -> None:
    """Every supported mutable Brain source drifts only when its semantic content changes."""
    from brain_v42.delivery_config import DeliverySettings
    from brain_v42.repositories.pg_delivery import PgDeliveryRepo
    from brain_v42.services.delivery_service import DeliveryService

    async with session_factory() as session:
        async with session.begin():
            source_id = (
                await session.execute(table.insert().values(**values).returning(table.c.id))
            ).scalar_one()
    ticket = await PgTicketRepo(session_factory).create(
        TicketCreate(
            kind=TicketKind.REQUEST,
            title=f"context {entity_type}",
            body="freeze source",
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
        idempotency_key=f"context-{entity_type}",
        contract=ContractInput(
            objective="freeze semantic source",
            priority=1,
            acceptance_mode="automatic",
            context_refs=(
                {
                    "kind": "brain_entity",
                    "entity_type": entity_type,
                    "entity_id": source_id,
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
    before = await service.get(ticket.id, actor_project="brain-v42")
    pinned_snapshot = before.contract.context_refs[0].content_snapshot
    assert before.contexts[0].status == "available"
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                sa.update(table).where(table.c.id == source_id).values(access_count=1)
            )
    assert (await service.get(ticket.id, actor_project="brain-v42")).contexts[
        0
    ].status == "available"
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                sa.update(table).where(table.c.id == source_id).values({semantic_column: "after"})
            )
    after = await service.get(ticket.id, actor_project="brain-v42")
    assert after.contexts[0].status == "changed"
    assert after.contract.context_refs[0].content_snapshot == pinned_snapshot


@pytest.mark.asyncio
async def test_context_guard_first_holds_for_share_until_the_updater_commits(
    session_factory,
) -> None:
    """A guarded context read holds a real shared lock against a native writer."""
    from brain_v42.models.delivery import BrainEntityReference
    from brain_v42.repositories.pg_delivery import PgDeliveryRepo

    decision_id = await _insert_context_decision(session_factory, "before")
    repo = PgDeliveryRepo(session_factory)
    async with (
        session_factory() as guard,
        session_factory() as updater,
        session_factory() as observer,
    ):
        guard_transaction = await guard.begin()
        updater_transaction = await updater.begin()
        try:
            guard_pid = await guard.scalar(sa.text("SELECT pg_backend_pid()"))
            pinned = await repo.resolve_context_references(
                guard,
                (
                    BrainEntityReference(
                        kind="brain_entity",
                        entity_type="decision",
                        entity_id=decision_id,
                    ),
                ),
            )
            updater_pid = await updater.scalar(sa.text("SELECT pg_backend_pid()"))
            update = asyncio.create_task(
                updater.execute(
                    sa.update(decisions)
                    .where(decisions.c.id == decision_id)
                    .values(description="after")
                )
            )
            await _wait_for_blocker(
                observer, waiter_pid=updater_pid, blocker_pid=guard_pid, operation=update
            )
            assert '"description":"before"' in pinned[0].content_snapshot
            await guard_transaction.commit()
            await asyncio.wait_for(update, timeout=5)
            await updater_transaction.commit()
        finally:
            if guard.in_transaction():
                await guard.rollback()
            if updater.in_transaction():
                await updater.rollback()


@pytest.mark.asyncio
async def test_context_updater_first_blocks_guard_then_exposes_committed_drift(
    session_factory,
) -> None:
    """A shared guard waits for a native writer and snapshots its committed content."""
    from brain_v42.models.delivery import BrainEntityReference
    from brain_v42.repositories.pg_delivery import PgDeliveryRepo

    decision_id = await _insert_context_decision(session_factory, "before")
    repo = PgDeliveryRepo(session_factory)
    async with (
        session_factory() as updater,
        session_factory() as guard,
        session_factory() as observer,
    ):
        updater_transaction = await updater.begin()
        guard_transaction = await guard.begin()
        try:
            updater_pid = await updater.scalar(sa.text("SELECT pg_backend_pid()"))
            await updater.execute(
                sa.update(decisions)
                .where(decisions.c.id == decision_id)
                .values(description="after")
            )
            guard_pid = await guard.scalar(sa.text("SELECT pg_backend_pid()"))
            guarded_read = asyncio.create_task(
                repo.resolve_context_references(
                    guard,
                    (
                        BrainEntityReference(
                            kind="brain_entity",
                            entity_type="decision",
                            entity_id=decision_id,
                        ),
                    ),
                )
            )
            await _wait_for_blocker(
                observer, waiter_pid=guard_pid, blocker_pid=updater_pid, operation=guarded_read
            )
            await updater_transaction.commit()
            pinned = await asyncio.wait_for(guarded_read, timeout=5)
            assert '"description":"after"' in pinned[0].content_snapshot
            await guard_transaction.commit()
        finally:
            if updater.in_transaction():
                await updater.rollback()
            if guard.in_transaction():
                await guard.rollback()


@pytest.mark.asyncio
async def test_binding_persists_canonical_registry_name_and_replays_its_uuid(
    session_factory,
) -> None:
    """Numeric repository identity binds the configured name and a retry is immutable."""
    from brain_v42.delivery_config import DeliverySettings
    from brain_v42.models.delivery import DeliveryError
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
        expected_revision=1,
        expected_workflow_version=1,
        idempotency_key="same-request",
    )
    replay = await service.bind_pr(
        ticket.id,
        actor_project="executor",
        deliverable_key="implementation",
        repository_id=7,
        pr_number=12,
        expected_revision=1,
        expected_workflow_version=1,
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
    with pytest.raises(DeliveryError, match="revision_conflict"):
        await service.bind_pr(
            ticket.id,
            actor_project="executor",
            deliverable_key="implementation",
            repository_id=7,
            pr_number=13,
            expected_revision=1,
            expected_workflow_version=1,
            idempotency_key="stale-generation",
        )


@pytest.mark.asyncio
async def test_amendment_first_forces_a_stale_binding_generation_to_conflict(
    session_factory,
) -> None:
    """A binding cannot publish against revision 1 after an in-flight amendment wins."""
    from brain_v42.models.delivery import ArtifactBinding, ContractRevision, DeliveryError
    from brain_v42.models.delivery_hashes import canonical_digest
    from brain_v42.repositories.pg_delivery import PgDeliveryRepo

    ticket, service = await _create_bindable_ticket(session_factory)
    current = await service.get(ticket.id, actor_project="brain-v42")
    amendment = ContractRevision.model_validate(
        {
            **current.contract.model_dump(mode="python"),
            "ticket_id": ticket.id,
            "contract_revision": 2,
            "objective": "amended while binding waits",
            "author_project": "brain-v42",
            "amendment_reason": "concurrency regression",
            "content_digest": None,
        }
    )
    repo = PgDeliveryRepo(session_factory)
    async with (
        session_factory() as amend_session,
        session_factory() as bind_session,
        session_factory() as observer,
    ):
        amend_transaction = await amend_session.begin()
        await bind_session.begin()
        try:
            await repo.set_contract(
                amendment,
                expected_revision=1,
                actor_project="brain-v42",
                idempotency_key=f"amend-race-{ticket.id}",
                request_digest=canonical_digest({"race": "amend"}, domain="request"),
                session=amend_session,
            )
            amend_pid = await amend_session.scalar(sa.text("SELECT pg_backend_pid()"))
            bind_pid = await bind_session.scalar(sa.text("SELECT pg_backend_pid()"))
            bind = asyncio.create_task(
                repo.bind_pr(
                    ArtifactBinding(
                        ticket_id=ticket.id,
                        contract_revision=1,
                        attempt=1,
                        deliverable_key="implementation",
                        repository_id=1337360966,
                        pr_number=91,
                    ),
                    repository_name="hawkixs/brain-v42",
                    actor_project="brain-v42",
                    expected_revision=1,
                    expected_workflow_version=1,
                    idempotency_key=f"bind-race-{ticket.id}",
                    request_digest=canonical_digest({"race": "bind"}, domain="request"),
                    session=bind_session,
                )
            )
            await _wait_for_blocker(
                observer, waiter_pid=bind_pid, blocker_pid=amend_pid, operation=bind
            )
            await amend_transaction.commit()
            with pytest.raises(DeliveryError, match="revision_conflict"):
                await asyncio.wait_for(bind, timeout=5)
        finally:
            if amend_session.in_transaction():
                await amend_session.rollback()
            if bind_session.in_transaction():
                await bind_session.rollback()


@pytest.mark.asyncio
async def test_terminal_transition_first_refuses_a_waiting_binding(session_factory) -> None:
    """A binding rechecks ticket status after a concurrent terminal transition commits."""
    from brain_v42.models.delivery import ArtifactBinding, DeliveryError
    from brain_v42.models.delivery_hashes import canonical_digest
    from brain_v42.repositories.pg_delivery import PgDeliveryRepo

    ticket, _service = await _create_bindable_ticket(session_factory)
    repo = PgDeliveryRepo(session_factory)
    async with (
        session_factory() as terminal_session,
        session_factory() as bind_session,
        session_factory() as observer,
    ):
        terminal_transaction = await terminal_session.begin()
        await bind_session.begin()
        try:
            terminal_pid = await terminal_session.scalar(sa.text("SELECT pg_backend_pid()"))
            await terminal_session.execute(
                sa.update(tickets).where(tickets.c.id == ticket.id).values(status="closed")
            )
            bind_pid = await bind_session.scalar(sa.text("SELECT pg_backend_pid()"))
            bind = asyncio.create_task(
                repo.bind_pr(
                    ArtifactBinding(
                        ticket_id=ticket.id,
                        contract_revision=1,
                        attempt=1,
                        deliverable_key="implementation",
                        repository_id=1337360966,
                        pr_number=92,
                    ),
                    repository_name="hawkixs/brain-v42",
                    actor_project="brain-v42",
                    expected_revision=1,
                    expected_workflow_version=1,
                    idempotency_key=f"terminal-race-{ticket.id}",
                    request_digest=canonical_digest({"race": "terminal"}, domain="request"),
                    session=bind_session,
                )
            )
            await _wait_for_blocker(
                observer, waiter_pid=bind_pid, blocker_pid=terminal_pid, operation=bind
            )
            await terminal_transaction.commit()
            with pytest.raises(DeliveryError, match="ticket_not_contractable"):
                await asyncio.wait_for(bind, timeout=5)
        finally:
            if terminal_session.in_transaction():
                await terminal_session.rollback()
            if bind_session.in_transaction():
                await bind_session.rollback()


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
        expected_revision=1,
        expected_workflow_version=1,
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


@pytest.mark.asyncio
async def test_dependency_requires_an_existing_visible_generation_and_rejects_a_cycle(
    session_factory,
) -> None:
    """Graph edges pin an observable upstream generation and never close a cycle."""
    from brain_v42.delivery_config import DeliverySettings
    from brain_v42.models.delivery import DeliveryDependency, DeliveryError
    from brain_v42.repositories.pg_delivery import PgDeliveryRepo
    from brain_v42.services.delivery_service import DeliveryService

    tickets = [
        await PgTicketRepo(session_factory).create(
            TicketCreate(
                kind=TicketKind.REQUEST,
                title=f"dependency {label}",
                body="validate graph edge",
                from_project="brain-v42",
                to_project="brain-v42",
            )
        )
        for label in ("a", "b")
    ]
    service = DeliveryService(
        PgDeliveryRepo(session_factory), settings=DeliverySettings(enabled=True)
    )

    def contract(
        objective: str, dependencies: tuple[DeliveryDependency, ...] = ()
    ) -> ContractInput:
        return ContractInput(
            objective=objective,
            priority=1,
            acceptance_mode="automatic",
            dependencies=dependencies,
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
        tickets[0].id,
        actor_project="brain-v42",
        contract=contract("a-v1"),
        expected_revision=0,
        idempotency_key="dependency-a-v1",
    )
    await service.set_contract(
        tickets[1].id,
        actor_project="brain-v42",
        contract=contract(
            "b-v1",
            (
                DeliveryDependency(
                    ticket_id=tickets[0].id, contract_revision=1, attempt=1, milestone="integrated"
                ),
            ),
        ),
        expected_revision=0,
        idempotency_key="dependency-b-v1",
    )
    with pytest.raises(DeliveryError, match="dependency_cycle"):
        await service.set_contract(
            tickets[0].id,
            actor_project="brain-v42",
            contract=contract(
                "a-v2",
                (
                    DeliveryDependency(
                        ticket_id=tickets[1].id,
                        contract_revision=1,
                        attempt=1,
                        milestone="integrated",
                    ),
                ),
            ),
            expected_revision=1,
            idempotency_key="dependency-a-v2",
        )


@pytest.mark.asyncio
async def test_load_inputs_hydrates_the_persisted_dependency_generation(session_factory) -> None:
    """The evaluator receives pinned upstream state instead of an empty dependency list."""
    from brain_v42.delivery_config import DeliverySettings
    from brain_v42.models.delivery import DeliveryDependency
    from brain_v42.repositories.pg_delivery import PgDeliveryRepo
    from brain_v42.services.delivery_service import DeliveryService

    upstream, downstream = [
        await PgTicketRepo(session_factory).create(
            TicketCreate(
                kind=TicketKind.REQUEST,
                title=f"hydrate dependency {label}",
                body="hydrate graph input",
                from_project="brain-v42",
                to_project="brain-v42",
            )
        )
        for label in ("upstream", "downstream")
    ]
    repo = PgDeliveryRepo(session_factory)
    service = DeliveryService(repo, settings=DeliverySettings(enabled=True))

    def contract(
        objective: str, dependencies: tuple[DeliveryDependency, ...] = ()
    ) -> ContractInput:
        return ContractInput(
            objective=objective,
            priority=1,
            acceptance_mode="automatic",
            dependencies=dependencies,
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
        upstream.id,
        actor_project="brain-v42",
        contract=contract("upstream"),
        expected_revision=0,
        idempotency_key="hydrate-upstream",
    )
    await service.set_contract(
        downstream.id,
        actor_project="brain-v42",
        contract=contract(
            "downstream",
            (
                DeliveryDependency(
                    ticket_id=upstream.id, contract_revision=1, attempt=1, milestone="integrated"
                ),
            ),
        ),
        expected_revision=0,
        idempotency_key="hydrate-downstream",
    )
    inputs = await repo.load_inputs(downstream.id, feature_enabled=True, freshness_seconds=60)
    assert inputs is not None
    assert inputs.dependencies[0].ticket_id == upstream.id


def test_delivery_migration_downgrades_an_empty_fresh_head(migration_database_url) -> None:
    """053 is reversible on a fresh disposable database with no delivery history."""
    db_url = migration_database_url
    with fresh_head_database(db_url, prefix="brain_delivery_downgrade") as empty_url:
        empty = subprocess.run(
            [sys.executable, "-m", "alembic", "downgrade", "052"],
            cwd=_PROJECT_ROOT,
            env={**os.environ, "POSTGRES_URL": empty_url},
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert empty.returncode == 0, empty.stderr
        restored = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=_PROJECT_ROOT,
            env={**os.environ, "POSTGRES_URL": empty_url},
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert restored.returncode == 0, restored.stderr


@pytest.mark.asyncio
async def test_delivery_migration_refuses_to_downgrade_history(
    session_factory, migration_downgrade_fence
) -> None:
    """053 is fail-closed once a workflow exists."""
    from brain_v42.delivery_config import DeliverySettings
    from brain_v42.repositories.pg_delivery import PgDeliveryRepo
    from brain_v42.services.delivery_service import DeliveryService

    migration_downgrade_fence("052")
    db_url = os.environ["BRAIN_V42_TEST_DB_URL"]

    ticket = await PgTicketRepo(session_factory).create(
        TicketCreate(
            kind=TicketKind.REQUEST,
            title="downgrade fence",
            body="retain delivery history",
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
        idempotency_key="downgrade-fence",
        contract=ContractInput(
            objective="retain history",
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
    refused = subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "052"],
        cwd=_PROJECT_ROOT,
        env={**os.environ, "POSTGRES_URL": db_url},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert refused.returncode != 0
    assert "cannot downgrade 053: delivery workflow history exists" in refused.stderr


@pytest.mark.asyncio
async def test_cross_ticket_roles_and_registered_repository_identity(session_factory) -> None:
    """Requester owns contracts, executor owns bindings, and registry scope is exact."""
    from brain_v42.delivery_config import DeliverySettings
    from brain_v42.models.delivery import DeliveryError
    from brain_v42.repositories.pg_delivery import PgDeliveryRepo
    from brain_v42.services.delivery_service import DeliveryService

    ticket = await PgTicketRepo(session_factory).create(
        TicketCreate(
            kind=TicketKind.REQUEST,
            title="cross roles",
            body="enforce contract roles",
            from_project="requester",
            to_project="executor",
        )
    )
    service = DeliveryService(
        PgDeliveryRepo(session_factory),
        settings=DeliverySettings(
            enabled=True,
            repository_registry={"executor": {7: "example/executor", 8: "example/other"}},
        ),
    )
    contract = ContractInput(
        objective="cross roles",
        priority=1,
        acceptance_mode="automatic",
        deliverables=(
            Deliverable(
                key="implementation",
                repository="example/executor",
                target_branch="main",
                required_checks=(),
                no_checks_reason="covered later",
                review=ReviewPolicy(required_approvals=0, allowed_reviewers=()),
            ),
        ),
    )
    with pytest.raises(DeliveryError, match="not_allowed"):
        await service.set_contract(
            ticket.id,
            actor_project="executor",
            contract=contract,
            expected_revision=0,
            idempotency_key="cross-executor-contract",
        )
    await service.set_contract(
        ticket.id,
        actor_project="requester",
        contract=contract,
        expected_revision=0,
        idempotency_key="cross-requester-contract",
    )
    with pytest.raises(DeliveryError, match="not_allowed"):
        await service.bind_pr(
            ticket.id,
            actor_project="requester",
            deliverable_key="implementation",
            repository_id=7,
            pr_number=1,
            expected_revision=1,
            expected_workflow_version=1,
            idempotency_key="cross-requester-bind",
        )
    with pytest.raises(DeliveryError, match="repository_mismatch"):
        await service.bind_pr(
            ticket.id,
            actor_project="executor",
            deliverable_key="implementation",
            repository_id=8,
            pr_number=1,
            expected_revision=1,
            expected_workflow_version=1,
            idempotency_key="cross-wrong-repo",
        )
    with pytest.raises(DeliveryError, match="not_allowed"):
        await service.get(ticket.id, actor_project="outside")


@pytest.mark.asyncio
async def test_evidence_foreign_keys_reject_cross_binding_snapshot_and_error_success_pointer(
    session_factory,
) -> None:
    """Proof rows and success pointers are bound to the exact artifact subject."""
    from brain_v42.delivery_config import DeliverySettings
    from brain_v42.repositories.pg_delivery import PgDeliveryRepo
    from brain_v42.services.delivery_service import DeliveryService

    ticket = await PgTicketRepo(session_factory).create(
        TicketCreate(
            kind=TicketKind.REQUEST,
            title="evidence identity",
            body="reject substituted proof subjects",
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
        idempotency_key="evidence-contract",
        contract=ContractInput(
            objective="evidence identity",
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
    first = await service.bind_pr(
        ticket.id,
        actor_project="brain-v42",
        deliverable_key="implementation",
        repository_id=1337360966,
        pr_number=1,
        expected_revision=1,
        expected_workflow_version=1,
        idempotency_key="evidence-first",
    )
    second = await service.bind_pr(
        ticket.id,
        actor_project="brain-v42",
        deliverable_key="implementation",
        repository_id=1337360966,
        pr_number=2,
        expected_revision=1,
        expected_workflow_version=2,
        idempotency_key="evidence-second",
    )
    now = datetime.now(UTC)
    async with session_factory() as session:
        async with session.begin():
            context_digest = (
                await session.execute(
                    sa.select(delivery_workflows.c.context_set_digest).where(
                        delivery_workflows.c.ticket_id == ticket.id
                    )
                )
            ).scalar_one()
            with pytest.raises(sa.exc.IntegrityError):
                async with session.begin_nested():
                    await session.execute(
                        delivery_snapshots.insert().values(
                            subject_kind="repository_context",
                            ticket_id=ticket.id,
                            contract_revision=1,
                            attempt=0,
                            context_set_digest=context_digest,
                            semantic_digest="0" * 64,
                            evidence={},
                        )
                    )
            snapshot = (
                await session.execute(
                    delivery_snapshots.insert()
                    .values(
                        subject_kind="artifact_binding",
                        binding_id=first.id,
                        semantic_digest="1" * 64,
                        evidence={},
                    )
                    .returning(delivery_snapshots.c.id)
                )
            ).scalar_one()
            with pytest.raises(sa.exc.IntegrityError):
                async with session.begin_nested():
                    await session.execute(
                        delivery_confirmations.insert().values(
                            subject_kind="artifact_binding",
                            binding_id=second.id,
                            snapshot_id=snapshot,
                            collection_started_at=now,
                            collection_finished_at=now,
                            outcome="success",
                        )
                    )
            error = (
                await session.execute(
                    delivery_confirmations.insert()
                    .values(
                        subject_kind="artifact_binding",
                        binding_id=second.id,
                        collection_started_at=now,
                        collection_finished_at=now,
                        outcome="error",
                        error_code="provider_error",
                    )
                    .returning(delivery_confirmations.c.id)
                )
            ).scalar_one()
            with pytest.raises(sa.exc.IntegrityError):
                async with session.begin_nested():
                    await session.execute(
                        delivery_artifact_bindings.update()
                        .where(delivery_artifact_bindings.c.id == second.id)
                        .values(latest_success_confirmation_id=error)
                    )


@pytest.mark.asyncio
async def test_fyi_and_terminal_tickets_never_accept_delivery_contracts(session_factory) -> None:
    """Legacy FYI and closed request lifecycles remain outside delivery contracts."""
    from brain_v42.delivery_config import DeliverySettings
    from brain_v42.models.delivery import DeliveryError
    from brain_v42.repositories.pg_delivery import PgDeliveryRepo
    from brain_v42.services.delivery_service import DeliveryService

    service = DeliveryService(
        PgDeliveryRepo(session_factory), settings=DeliverySettings(enabled=True)
    )
    fyi = await PgTicketRepo(session_factory).create(
        TicketCreate(
            kind=TicketKind.FYI,
            title="fyi",
            body="legacy",
            from_project="brain-v42",
            to_project="brain-v42",
        )
    )
    closed = await PgTicketRepo(session_factory).create(
        TicketCreate(
            kind=TicketKind.REQUEST,
            title="closed",
            body="terminal",
            from_project="brain-v42",
            to_project="brain-v42",
        )
    )
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                sa.update(tickets).where(tickets.c.id == closed.id).values(status="closed")
            )
    contract = ContractInput(
        objective="reject",
        priority=1,
        acceptance_mode="automatic",
        deliverables=(
            Deliverable(
                key="implementation",
                repository="hawkixs/brain-v42",
                target_branch="main",
                required_checks=(),
                no_checks_reason="none",
                review=ReviewPolicy(required_approvals=0, allowed_reviewers=()),
            ),
        ),
    )
    for ticket in (fyi, closed):
        with pytest.raises(DeliveryError, match="ticket_not_contractable"):
            await service.set_contract(
                ticket.id,
                actor_project="brain-v42",
                contract=contract,
                expected_revision=0,
                idempotency_key=f"terminal-{ticket.id}",
            )
