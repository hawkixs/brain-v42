"""Real PostgreSQL locking contracts for decision-input hydration."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
import sqlalchemy as sa

from brain_v42.db.tables import decisions, tickets
from brain_v42.delivery_config import DeliverySettings
from brain_v42.models.delivery import (
    BrainEntityReference,
    ContractInput,
    ContractRevision,
    Deliverable,
    DeliveryDependency,
    PullRequestEvidence,
    ReviewPolicy,
)
from brain_v42.models.delivery_evaluator import evaluate_delivery
from brain_v42.models.delivery_hashes import canonical_digest
from brain_v42.models.ticket import TicketCreate, TicketKind
from brain_v42.repositories.pg_delivery import PgDeliveryRepo
from brain_v42.repositories.pg_delivery_evidence import PgDeliveryEvidenceRepo
from brain_v42.repositories.pg_ticket import PgTicketRepo
from brain_v42.services.delivery_service import DeliveryService

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def _wait_for_blocker(observer, *, waiter_pid: int, blocker_pid: int, operation) -> None:
    """Prove a real PostgreSQL wait edge rather than relying on elapsed time."""
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


async def _cancel_waiter(operation: asyncio.Task[object] | None) -> None:
    """Stop a blocked contender before rolling back the connection it uses."""
    if operation is None:
        return
    if not operation.done():
        operation.cancel()
    with suppress(asyncio.CancelledError):
        await operation


def _contract(*, source_id: UUID | None = None, dependencies=()) -> ContractInput:
    return ContractInput(
        objective="serialize one delivery decision",
        priority=1,
        acceptance_mode="automatic",
        context_refs=(
            ()
            if source_id is None
            else (
                BrainEntityReference(
                    kind="brain_entity", entity_type="decision", entity_id=source_id
                ),
            )
        ),
        dependencies=tuple(dependencies),
        deliverables=(
            Deliverable(
                key="implementation",
                repository="hawkixs/brain-v42",
                target_branch="main",
                required_checks=(),
                no_checks_reason="locking test",
                review=ReviewPolicy(required_approvals=0, allowed_reviewers=()),
            ),
        ),
    )


async def _source(session_factory, description: str = "before") -> UUID:
    async with session_factory() as session:
        async with session.begin():
            return (
                await session.execute(
                    decisions.insert()
                    .values(title="decision lock", description=description, reasoning="test")
                    .returning(decisions.c.id)
                )
            ).scalar_one()


async def _new_ticket(session_factory):
    return await PgTicketRepo(session_factory).create(
        TicketCreate(
            kind=TicketKind.REQUEST,
            title="locked decision inputs",
            body="transaction boundary",
            from_project="brain-v42",
            to_project="brain-v42",
        )
    )


async def _contracted_ticket(session_factory, *, source_id: UUID | None = None, dependencies=()):
    ticket = await _new_ticket(session_factory)
    service = DeliveryService(
        PgDeliveryRepo(session_factory), settings=DeliverySettings(enabled=True)
    )
    await service.set_contract(
        ticket.id,
        actor_project="brain-v42",
        contract=_contract(source_id=source_id, dependencies=dependencies),
        expected_revision=0,
        idempotency_key=f"locked-inputs-contract-{ticket.id}",
    )
    return ticket, service


def _stored_contract(ticket_id: UUID) -> ContractRevision:
    payload = _contract().model_dump()
    payload["deliverables"][0]["repository_id"] = 1337360966
    return ContractRevision(
        **payload,
        ticket_id=ticket_id,
        contract_revision=1,
        author_project="brain-v42",
        created_at=datetime.now(UTC),
    )


async def _bind_observed_evidence(
    session_factory, ticket_id: UUID, service: DeliveryService
) -> None:
    binding = await service.bind_pr(
        ticket_id,
        actor_project="brain-v42",
        deliverable_key="implementation",
        repository_id=1337360966,
        pr_number=91,
        expected_revision=1,
        expected_workflow_version=1,
        idempotency_key=f"locked-inputs-binding-{ticket_id}",
    )
    async with session_factory() as session:
        async with session.begin():
            now = await session.scalar(sa.select(sa.func.clock_timestamp()))
            assert isinstance(now, datetime)
            await PgDeliveryEvidenceRepo(session_factory).publish_observation(
                session,
                binding.id,
                1,
                PullRequestEvidence(
                    provider_id=1,
                    repository_id=1337360966,
                    pr_number=91,
                    author_id="executor",
                    head_repository_id=1337360966,
                    head_sha="a" * 40,
                    base_sha="b" * 40,
                    base_ref="main",
                    state="open",
                    draft=False,
                    complete=True,
                    collected_at=now,
                ),
                now,
                now,
            )


async def test_locked_inputs_wait_for_source_update_then_hydrate_committed_source(
    session_factory,
) -> None:
    """Removing source FOR SHARE lets a decision race an already-running source update."""
    source_id = await _source(session_factory)
    ticket, _service = await _contracted_ticket(session_factory, source_id=source_id)
    repo = PgDeliveryRepo(session_factory)
    async with (
        session_factory() as updater,
        session_factory() as decision,
        session_factory() as observer,
    ):
        update_transaction = await updater.begin()
        decision_transaction = await decision.begin()
        operation: asyncio.Task[object] | None = None
        try:
            updater_pid = await updater.scalar(sa.text("SELECT pg_backend_pid()"))
            await updater.execute(
                sa.update(decisions).where(decisions.c.id == source_id).values(description="after")
            )
            decision_pid = await decision.scalar(sa.text("SELECT pg_backend_pid()"))
            operation = asyncio.create_task(
                repo.load_locked_decision_inputs(
                    decision, ticket.id, feature_enabled=True, freshness_seconds=60
                )
            )
            await _wait_for_blocker(
                observer, waiter_pid=decision_pid, blocker_pid=updater_pid, operation=operation
            )
            await update_transaction.commit()
            locked = await asyncio.wait_for(operation, timeout=5)
            assert locked.inputs is not None
            assert locked.inputs.contexts[0].status == "changed"
            await decision_transaction.commit()
        finally:
            await _cancel_waiter(operation)
            if updater.in_transaction():
                await updater.rollback()
            if decision.in_transaction():
                await decision.rollback()


async def test_locked_inputs_hold_source_share_lock_before_source_update(session_factory) -> None:
    """Dropping retained source locks allows a writer through after the decision reads it."""
    source_id = await _source(session_factory)
    ticket, _service = await _contracted_ticket(session_factory, source_id=source_id)
    repo = PgDeliveryRepo(session_factory)
    async with (
        session_factory() as decision,
        session_factory() as updater,
        session_factory() as observer,
    ):
        decision_transaction = await decision.begin()
        update_transaction = await updater.begin()
        operation: asyncio.Task[object] | None = None
        try:
            decision_pid = await decision.scalar(sa.text("SELECT pg_backend_pid()"))
            locked = await repo.load_locked_decision_inputs(
                decision, ticket.id, feature_enabled=True, freshness_seconds=60
            )
            assert locked.ticket.id == ticket.id
            updater_pid = await updater.scalar(sa.text("SELECT pg_backend_pid()"))
            operation = asyncio.create_task(
                updater.execute(
                    sa.update(decisions)
                    .where(decisions.c.id == source_id)
                    .values(description="after")
                )
            )
            await _wait_for_blocker(
                observer, waiter_pid=updater_pid, blocker_pid=decision_pid, operation=operation
            )
            await decision_transaction.commit()
            await asyncio.wait_for(operation, timeout=5)
            await update_transaction.commit()
        finally:
            await _cancel_waiter(operation)
            if decision.in_transaction():
                await decision.rollback()
            if updater.in_transaction():
                await updater.rollback()


async def test_locked_inputs_wait_on_lower_upstream_before_locking_higher_current_ticket(
    session_factory,
) -> None:
    """Locking the current ticket first would expose the reverse-order deadlock window."""
    first, second = await _new_ticket(session_factory), await _new_ticket(session_factory)
    upstream, current = sorted((first, second), key=lambda ticket: str(ticket.id))
    service = DeliveryService(
        PgDeliveryRepo(session_factory), settings=DeliverySettings(enabled=True)
    )
    await service.set_contract(
        upstream.id,
        actor_project="brain-v42",
        contract=_contract(),
        expected_revision=0,
        idempotency_key=f"locked-inputs-upstream-{upstream.id}",
    )
    await service.set_contract(
        current.id,
        actor_project="brain-v42",
        contract=_contract(
            dependencies=(
                DeliveryDependency(
                    ticket_id=upstream.id, contract_revision=1, attempt=1, milestone="integrated"
                ),
            )
        ),
        expected_revision=0,
        idempotency_key=f"locked-inputs-current-{current.id}",
    )
    assert str(upstream.id) < str(current.id)
    low, high = upstream.id, current.id
    repo = PgDeliveryRepo(session_factory)
    async with (
        session_factory() as blocker,
        session_factory() as decision,
        session_factory() as probe,
        session_factory() as observer,
    ):
        blocker_transaction = await blocker.begin()
        decision_transaction = await decision.begin()
        probe_transaction = await probe.begin()
        operation: asyncio.Task[object] | None = None
        try:
            blocker_pid = await blocker.scalar(sa.text("SELECT pg_backend_pid()"))
            await blocker.execute(
                sa.select(tickets.c.id).where(tickets.c.id == low).with_for_update()
            )
            decision_pid = await decision.scalar(sa.text("SELECT pg_backend_pid()"))
            operation = asyncio.create_task(
                repo.load_locked_decision_inputs(
                    decision, current.id, feature_enabled=True, freshness_seconds=60
                )
            )
            await _wait_for_blocker(
                observer, waiter_pid=decision_pid, blocker_pid=blocker_pid, operation=operation
            )
            locked_high = await probe.scalar(
                sa.select(tickets.c.id).where(tickets.c.id == high).with_for_update(nowait=True)
            )
            assert locked_high == high
            await probe_transaction.commit()
            await blocker_transaction.commit()
            await asyncio.wait_for(operation, timeout=5)
            await decision_transaction.commit()
        finally:
            await _cancel_waiter(operation)
            if blocker.in_transaction():
                await blocker.rollback()
            if decision.in_transaction():
                await decision.rollback()
            if probe.in_transaction():
                await probe.rollback()


async def test_locked_inputs_without_workflow_serializes_contract_attachment(
    session_factory,
) -> None:
    """Skipping legacy tickets lets a contract attach between decision facts and transition."""
    ticket = await PgTicketRepo(session_factory).create(
        TicketCreate(
            kind=TicketKind.REQUEST,
            title="legacy decision",
            body="no delivery workflow yet",
            from_project="brain-v42",
            to_project="brain-v42",
        )
    )
    repo = PgDeliveryRepo(session_factory)
    contract = _stored_contract(ticket.id)
    async with (
        session_factory() as decision,
        session_factory() as attach,
        session_factory() as observer,
    ):
        decision_transaction = await decision.begin()
        operation: asyncio.Task[object] | None = None
        try:
            decision_pid = await decision.scalar(sa.text("SELECT pg_backend_pid()"))
            locked = await repo.load_locked_decision_inputs(
                decision, ticket.id, feature_enabled=True, freshness_seconds=60
            )
            assert locked.inputs is None
            attach_pid = await attach.scalar(sa.text("SELECT pg_backend_pid()"))
            operation = asyncio.create_task(
                repo.set_contract(
                    contract,
                    actor_project="brain-v42",
                    expected_revision=0,
                    idempotency_key=f"legacy-attach-{ticket.id}",
                    request_digest=canonical_digest(
                        {"ticket_id": str(ticket.id)}, domain="request"
                    ),
                    session=attach,
                )
            )
            await _wait_for_blocker(
                observer, waiter_pid=attach_pid, blocker_pid=decision_pid, operation=operation
            )
            await decision_transaction.commit()
            attached = await asyncio.wait_for(operation, timeout=5)
            assert attached.contract_revision == 1
        finally:
            await _cancel_waiter(operation)
            if decision.in_transaction():
                await decision.rollback()
            if attach.in_transaction():
                await attach.rollback()


async def test_locked_inputs_sample_pg_clock_after_source_wait_crosses_freshness(
    session_factory,
) -> None:
    """Sampling before waits makes fresh evidence appear valid after the blocked decision resumes."""
    source_id = await _source(session_factory)
    ticket, service = await _contracted_ticket(session_factory, source_id=source_id)
    await _bind_observed_evidence(session_factory, ticket.id, service)
    repo = PgDeliveryRepo(session_factory)
    async with (
        session_factory() as updater,
        session_factory() as decision,
        session_factory() as observer,
    ):
        update_transaction = await updater.begin()
        decision_transaction = await decision.begin()
        operation: asyncio.Task[object] | None = None
        try:
            await updater.execute(
                sa.select(decisions.c.id).where(decisions.c.id == source_id).with_for_update()
            )
            updater_pid = await updater.scalar(sa.text("SELECT pg_backend_pid()"))
            started_at = await decision.scalar(sa.select(sa.func.transaction_timestamp()))
            decision_pid = await decision.scalar(sa.text("SELECT pg_backend_pid()"))
            operation = asyncio.create_task(
                repo.load_locked_decision_inputs(
                    decision, ticket.id, feature_enabled=True, freshness_seconds=1
                )
            )
            await _wait_for_blocker(
                observer, waiter_pid=decision_pid, blocker_pid=updater_pid, operation=operation
            )
            await asyncio.sleep(1.1)
            released_at = await updater.scalar(sa.select(sa.func.clock_timestamp()))
            await update_transaction.commit()
            locked = await asyncio.wait_for(operation, timeout=5)
            assert locked.decision_time > started_at
            assert locked.decision_time >= released_at
            assert locked.inputs is not None
            assert locked.inputs.active_bindings[0].confirmation is not None
            confirmation = locked.inputs.active_bindings[0].confirmation
            assert started_at < confirmation.collection_finished_at + timedelta(seconds=1)
            assert locked.decision_time >= confirmation.collection_finished_at + timedelta(
                seconds=1
            )
            assessment = evaluate_delivery(locked.inputs, now=locked.decision_time)
            assert assessment.observation_health == "stale"
            await decision_transaction.commit()
        finally:
            await _cancel_waiter(operation)
            if updater.in_transaction():
                await updater.rollback()
            if decision.in_transaction():
                await decision.rollback()


async def test_locked_inputs_leave_commit_and_rollback_to_the_caller(session_factory) -> None:
    """Committing inside the primitive would release its locks before the caller decides."""
    ticket, _service = await _contracted_ticket(session_factory)
    repo = PgDeliveryRepo(session_factory)
    async with session_factory() as decision, session_factory() as contender:
        transaction = await decision.begin()
        transient_id = (
            await decision.execute(
                decisions.insert()
                .values(title="transient caller write", description="rollback", reasoning="test")
                .returning(decisions.c.id)
            )
        ).scalar_one()
        locked = await repo.load_locked_decision_inputs(
            decision, ticket.id, feature_enabled=True, freshness_seconds=60
        )
        assert locked.ticket.id == ticket.id
        await transaction.rollback()
        async with contender.begin():
            absent = await contender.scalar(
                sa.select(decisions.c.id).where(decisions.c.id == transient_id)
            )
            assert absent is None
            updated = await contender.execute(
                sa.update(tickets).where(tickets.c.id == ticket.id).values(title="released")
            )
            assert updated.rowcount == 1
