"""Real PostgreSQL contracts for receipt issuance from successful publishers."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import datetime, timedelta
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa

from brain_v42.db.tables import (
    decisions,
    delivery_artifact_bindings,
    delivery_confirmations,
    delivery_receipts,
    delivery_snapshots,
    delivery_workflows,
    tickets,
)
from brain_v42.delivery_config import DeliverySettings
from brain_v42.models.delivery import (
    BrainEntityReference,
    CheckAttempt,
    ContractInput,
    Deliverable,
    DeliveryDependency,
    PullRequestEvidence,
    ReceiptIssuerProvenance,
    RepositoryContextEvidence,
    RepositoryDocumentFact,
    RepositoryDocumentReference,
    RequiredCheck,
    ReviewPolicy,
)
from brain_v42.models.ticket import TicketCreate, TicketKind
from brain_v42.repositories.pg_delivery import PgDeliveryRepo
from brain_v42.repositories.pg_delivery_evidence import PgDeliveryEvidenceRepo
from brain_v42.repositories.pg_ticket import PgTicketRepo
from brain_v42.services.delivery_service import DeliveryService

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

RID = 1_337_360_966
A, B, C, D = "a" * 40, "b" * 40, "c" * 40, "d" * 40


def _contract(*, mode="automatic", refs=(), dependencies=(), checks=(), source: UUID | None = None):
    context_refs = tuple(refs) + (
        ()
        if source is None
        else (BrainEntityReference(kind="brain_entity", entity_type="decision", entity_id=source),)
    )
    return ContractInput(
        objective="publish a receipt from current database observations",
        priority=1,
        acceptance_mode=mode,
        context_refs=context_refs,
        dependencies=tuple(dependencies),
        deliverables=(
            Deliverable(
                key="implementation",
                repository="hawkixs/brain-v42",
                target_branch="main",
                required_checks=tuple(checks),
                no_checks_reason=None if checks else "publication test",
                review=ReviewPolicy(required_approvals=0, allowed_reviewers=()),
            ),
        ),
    )


async def _workflow(factory, **options):
    ticket = await PgTicketRepo(factory).create(
        TicketCreate(
            kind=TicketKind.REQUEST,
            title=f"receipt publisher {uuid4()}",
            body="real pg fixture",
            from_project="brain-v42",
            to_project="brain-v42",
        )
    )
    service = DeliveryService(
        PgDeliveryRepo(factory), settings=DeliverySettings(enabled=True, freshness_seconds=3600)
    )
    await service.set_contract(
        ticket.id,
        actor_project="brain-v42",
        expected_revision=0,
        idempotency_key=f"publication-contract-{ticket.id}",
        contract=_contract(**options),
    )
    binding = await service.bind_pr(
        ticket.id,
        actor_project="brain-v42",
        deliverable_key="implementation",
        repository_id=RID,
        pr_number=42,
        expected_revision=1,
        expected_workflow_version=1,
        idempotency_key=f"publication-binding-{ticket.id}",
    )
    return ticket, binding, service


def _evidence(now: datetime, *, state="merged", checks=(), pr_number=42):
    return PullRequestEvidence(
        provider_id=7,
        repository_id=RID,
        pr_number=pr_number,
        author_id="executor",
        head_repository_id=8,
        head_sha=A,
        base_sha=B,
        base_ref="main",
        state=state,
        draft=False,
        complete=True,
        integration_sha=C,
        checks=tuple(checks),
        collected_at=now,
    )


def _repository_ref():
    return RepositoryDocumentReference(
        kind="repository_document", repository_id=RID, sha=A, path="docs/receipt.md"
    )


def _context():
    return RepositoryContextEvidence(
        complete=True,
        facts=(
            RepositoryDocumentFact(
                repository_id=RID,
                commit_sha=A,
                path="docs/receipt.md",
                tree_sha=C,
                blob_sha=D,
                mode="100644",
                status="available",
            ),
        ),
    )


def _issuer(factory, *, enabled=True, freshness=3600):
    return PgDeliveryEvidenceRepo(
        factory,
        settings=DeliverySettings(enabled=enabled, freshness_seconds=freshness),
        observer_provenance=ReceiptIssuerProvenance(
            issuer_project="brain-v42",
            issuer_identity="publication-receipt-test",
            issuer_kind="observer",
        ),
    )


async def _now(session):
    result = await session.scalar(sa.select(sa.func.clock_timestamp()))
    assert isinstance(result, datetime)
    return result


async def _context_row(session, ticket_id):
    return (
        (
            await session.execute(
                sa.select(delivery_workflows).where(delivery_workflows.c.ticket_id == ticket_id)
            )
        )
        .mappings()
        .one()
    )


async def _publish_context(repo, session, ticket_id, at):
    row = await _context_row(session, ticket_id)
    return await repo.publish_repository_context(
        session,
        ticket_id,
        row["current_revision"],
        row["attempt"],
        row["context_set_digest"],
        row["context_row_version"],
        _context(),
        at,
        at,
    )


async def _rows(factory, ticket_id):
    async with factory() as session:
        return (
            (
                await session.execute(
                    sa.select(delivery_receipts)
                    .where(delivery_receipts.c.ticket_id == ticket_id)
                    .order_by(delivery_receipts.c.milestone)
                )
            )
            .mappings()
            .all()
        )


async def _wait_for_blocker(observer, *, waiter_pid: int, blocker_pid: int, task):
    for _ in range(100):
        blockers = await observer.scalar(
            sa.text("SELECT pg_blocking_pids(:pid)"), {"pid": waiter_pid}
        )
        if blocker_pid in blockers:
            assert not task.done()
            return
        if task.done():
            await task
            raise AssertionError("publisher completed without the expected PostgreSQL wait")
        await asyncio.sleep(0.01)
    raise AssertionError("PostgreSQL did not report the required wait edge")


async def _cancel(task):
    if task is not None and not task.done():
        task.cancel()
    if task is not None:
        with suppress(asyncio.CancelledError):
            await task


async def test_artifact_last_publisher_issues_exact_new_confirmation_receipts(session_factory):
    """The artifact success hook must issue only after its own confirmation advances state."""
    ticket, binding, _ = await _workflow(session_factory, refs=(_repository_ref(),))
    repo = _issuer(session_factory)
    async with session_factory() as session:
        async with session.begin():
            now = await _now(session)
            await _publish_context(repo, session, ticket.id, now)
            confirmation = await repo.publish_observation(
                session, binding.id, binding.binding_version, _evidence(now), now, now
            )
    rows = await _rows(session_factory, ticket.id)
    assert [row["milestone"] for row in rows] == ["fulfilled", "integration"]
    for row in rows:
        assert row["payload"]["proof"]["artifact_proofs"][0]["success_confirmation_id"] == str(
            confirmation.id
        )


async def test_context_last_publisher_issues_exact_new_confirmation_receipts(session_factory):
    """The context success hook must rehydrate the artifact before issuing its own proof."""
    ticket, binding, _ = await _workflow(session_factory, refs=(_repository_ref(),))
    repo = _issuer(session_factory)
    async with session_factory() as session:
        async with session.begin():
            now = await _now(session)
            await repo.publish_observation(
                session, binding.id, binding.binding_version, _evidence(now), now, now
            )
            confirmation = await _publish_context(repo, session, ticket.id, now)
    rows = await _rows(session_factory, ticket.id)
    assert len(rows) == 2
    for row in rows:
        assert row["payload"]["proof"]["repository_context_proofs"][0][
            "success_confirmation_id"
        ] == str(confirmation.id)


@pytest.mark.parametrize(
    "state,required,checks",
    [
        ("open", (), ()),
        (
            "merged",
            (RequiredCheck(kind="check_run", name="ci", provider_id=7),),
            (
                CheckAttempt(
                    record_id=1,
                    provider_id=7,
                    kind="check_run",
                    name="ci",
                    head_sha=A,
                    conclusion="pending",
                ),
            ),
        ),
        ("merged", (RequiredCheck(kind="check_run", name="ci", provider_id=7),), ()),
    ],
)
async def test_ineligible_success_persists_confirmation_without_receipt(
    session_factory, state, required, checks
):
    """A valid observation that is incomplete for delivery remains append-only evidence."""
    ticket, binding, _ = await _workflow(session_factory, checks=required)
    repo = _issuer(session_factory)
    async with session_factory() as session:
        async with session.begin():
            now = await _now(session)
            confirmation = await repo.publish_observation(
                session,
                binding.id,
                binding.binding_version,
                _evidence(now, state=state, checks=checks),
                now,
                now,
            )
    assert confirmation.id is not None
    async with session_factory() as session:
        stored = (
            (
                await session.execute(
                    sa.select(delivery_confirmations).where(
                        delivery_confirmations.c.id == confirmation.id
                    )
                )
            )
            .mappings()
            .one()
        )
        pointer = await session.scalar(
            sa.select(delivery_artifact_bindings.c.latest_success_confirmation_id).where(
                delivery_artifact_bindings.c.id == binding.id
            )
        )
        snapshot = await session.scalar(
            sa.select(delivery_snapshots.c.id).where(
                delivery_snapshots.c.id == stored["snapshot_id"]
            )
        )
    assert stored["binding_id"] == binding.id
    assert pointer == confirmation.id
    assert snapshot is not None
    assert snapshot == stored["snapshot_id"]
    assert await _rows(session_factory, ticket.id) == []


async def test_disabled_publisher_remains_persistence_only(session_factory):
    """A disabled constructor must never turn an otherwise eligible success into a receipt."""
    ticket, binding, _ = await _workflow(session_factory)
    repo = _issuer(session_factory, enabled=False)
    async with session_factory() as session:
        async with session.begin():
            now = await _now(session)
            confirmation = await repo.publish_observation(
                session, binding.id, binding.binding_version, _evidence(now), now, now
            )
    assert confirmation.id is not None
    async with session_factory() as session:
        stored = (
            (
                await session.execute(
                    sa.select(delivery_confirmations).where(
                        delivery_confirmations.c.id == confirmation.id
                    )
                )
            )
            .mappings()
            .one()
        )
        pointer = await session.scalar(
            sa.select(delivery_artifact_bindings.c.latest_success_confirmation_id).where(
                delivery_artifact_bindings.c.id == binding.id
            )
        )
        snapshot = await session.scalar(
            sa.select(delivery_snapshots.c.id).where(
                delivery_snapshots.c.id == stored["snapshot_id"]
            )
        )
    assert stored["binding_id"] == binding.id
    assert pointer == confirmation.id
    assert snapshot is not None
    assert snapshot == stored["snapshot_id"]
    assert await _rows(session_factory, ticket.id) == []


async def test_identical_successes_append_confirmations_without_mutating_receipts(session_factory):
    """Repeated artifact and context hooks retain frozen receipt rows despite fresh confirmation IDs."""
    ticket, binding, _ = await _workflow(session_factory, refs=(_repository_ref(),))
    repo = _issuer(session_factory)
    async with session_factory() as session:
        async with session.begin():
            now = await _now(session)
            first_context = await _publish_context(repo, session, ticket.id, now)
            first_artifact = await repo.publish_observation(
                session, binding.id, binding.binding_version, _evidence(now), now, now
            )
    initial = await _rows(session_factory, ticket.id)
    assert [row["milestone"] for row in initial] == ["fulfilled", "integration"]
    frozen = [(row["id"], row["issued_at"], row["payload"]) for row in initial]
    async with session_factory() as session:
        async with session.begin():
            now = await _now(session)
            second_context = await _publish_context(repo, session, ticket.id, now)
            current = await session.scalar(
                sa.select(delivery_artifact_bindings.c.row_version).where(
                    delivery_artifact_bindings.c.id == binding.id
                )
            )
            second_artifact = await repo.publish_observation(
                session, binding.id, current, _evidence(now), now, now
            )
    assert first_context.id != second_context.id
    assert first_artifact.id != second_artifact.id
    assert [
        (row["id"], row["issued_at"], row["payload"])
        for row in await _rows(session_factory, ticket.id)
    ] == frozen
    async with session_factory() as session:
        artifact_confirmations = await session.scalar(
            sa.select(sa.func.count())
            .select_from(delivery_confirmations)
            .where(delivery_confirmations.c.binding_id == binding.id)
        )
        context_confirmations = await session.scalar(
            sa.select(sa.func.count())
            .select_from(delivery_confirmations)
            .where(
                delivery_confirmations.c.ticket_id == ticket.id,
                delivery_confirmations.c.subject_kind == "repository_context",
            )
        )
        artifact_snapshots = await session.scalar(
            sa.select(sa.func.count())
            .select_from(delivery_snapshots)
            .where(delivery_snapshots.c.binding_id == binding.id)
        )
        context_snapshots = await session.scalar(
            sa.select(sa.func.count())
            .select_from(delivery_snapshots)
            .where(
                delivery_snapshots.c.ticket_id == ticket.id,
                delivery_snapshots.c.subject_kind == "repository_context",
            )
        )
    assert (artifact_confirmations, context_confirmations) == (2, 2)
    assert (artifact_snapshots, context_snapshots) == (1, 1)


async def test_explicit_policy_hook_issues_integration_only(session_factory):
    """Observer publication may issue integration evidence but cannot auto-fulfil explicit policy."""
    ticket, binding, _ = await _workflow(session_factory, mode="explicit")
    repo = _issuer(session_factory)
    async with session_factory() as session:
        async with session.begin():
            now = await _now(session)
            await repo.publish_observation(
                session, binding.id, binding.binding_version, _evidence(now), now, now
            )
    rows = await _rows(session_factory, ticket.id)
    assert [row["milestone"] for row in rows] == ["integration"]


async def test_final_publication_and_receipts_follow_caller_rollback(session_factory):
    """The hook must not commit its confirmation, pointer, snapshots, or sibling receipts."""
    ticket, binding, _ = await _workflow(session_factory, refs=(_repository_ref(),))
    repo = _issuer(session_factory)
    async with session_factory() as session:
        async with session.begin():
            prerequisite_time = await _now(session)
            prerequisite = await _publish_context(repo, session, ticket.id, prerequisite_time)
    async with session_factory() as session:
        transaction = await session.begin()
        try:
            binding_before = dict(
                (
                    await session.execute(
                        sa.select(delivery_artifact_bindings).where(
                            delivery_artifact_bindings.c.id == binding.id
                        )
                    )
                )
                .mappings()
                .one()
            )
            workflow_before = dict(await _context_row(session, ticket.id))
            now = await _now(session)
            confirmation = await repo.publish_observation(
                session, binding.id, binding.binding_version, _evidence(now), now, now
            )
            binding_after = dict(
                (
                    await session.execute(
                        sa.select(delivery_artifact_bindings).where(
                            delivery_artifact_bindings.c.id == binding.id
                        )
                    )
                )
                .mappings()
                .one()
            )
            assert binding_after["latest_success_confirmation_id"] == confirmation.id
            assert binding_after["row_version"] == binding_before["row_version"] + 1
            receipt_rows = (
                (
                    await session.execute(
                        sa.select(delivery_receipts)
                        .where(delivery_receipts.c.ticket_id == ticket.id)
                        .order_by(delivery_receipts.c.milestone)
                    )
                )
                .mappings()
                .all()
            )
            assert [row["milestone"] for row in receipt_rows] == ["fulfilled", "integration"]
            assert (
                await session.scalar(
                    sa.select(sa.func.count())
                    .select_from(delivery_snapshots)
                    .where(delivery_snapshots.c.binding_id == binding.id)
                )
                == 1
            )
            assert (
                await session.scalar(
                    sa.select(sa.func.count())
                    .select_from(delivery_confirmations)
                    .where(delivery_confirmations.c.binding_id == binding.id)
                )
                == 1
            )
        finally:
            await transaction.rollback()
    async with session_factory() as session:
        counts = [
            await session.scalar(
                sa.select(sa.func.count()).select_from(table).where(column == value)
            )
            for table, column, value in (
                (delivery_snapshots, delivery_snapshots.c.binding_id, binding.id),
                (delivery_confirmations, delivery_confirmations.c.binding_id, binding.id),
                (delivery_receipts, delivery_receipts.c.ticket_id, ticket.id),
            )
        ]
        row = await session.scalar(
            sa.select(delivery_artifact_bindings.c.latest_success_confirmation_id).where(
                delivery_artifact_bindings.c.id == binding.id
            )
        )
        preserved = await session.scalar(
            sa.select(delivery_workflows.c.latest_context_success_confirmation_id).where(
                delivery_workflows.c.ticket_id == ticket.id
            )
        )
    assert counts == [0, 0, 0]
    assert row is None
    assert preserved == prerequisite.id
    async with session_factory() as session:
        binding_after = dict(
            (
                await session.execute(
                    sa.select(delivery_artifact_bindings).where(
                        delivery_artifact_bindings.c.id == binding.id
                    )
                )
            )
            .mappings()
            .one()
        )
        workflow_after = dict(await _context_row(session, ticket.id))
        prerequisite_snapshot = await session.scalar(
            sa.select(delivery_snapshots.c.id).where(
                delivery_snapshots.c.id == prerequisite.snapshot_id
            )
        )
    assert binding_after == binding_before
    assert workflow_after == workflow_before
    assert prerequisite_snapshot == prerequisite.snapshot_id


async def test_source_lock_wait_makes_final_observation_stale_but_persists_it(session_factory):
    """Receipt evaluation samples PG time after required-source waits, while publication remains valid."""
    async with session_factory() as session:
        async with session.begin():
            source_id = (
                await session.execute(
                    decisions.insert()
                    .values(title="source", description="before", reasoning="test")
                    .returning(decisions.c.id)
                )
            ).scalar_one()
    ticket, binding, _ = await _workflow(session_factory, source=source_id)
    repo = _issuer(session_factory, freshness=1)
    async with session_factory() as precondition:
        async with precondition.begin():
            now = await _now(precondition)
            # No context hook is required: the artifact is the only deliverable predicate.
            evidence = _evidence(now)
    async with session_factory() as observation:
        async with observation.begin():
            at = await _now(observation)
    async with (
        session_factory() as blocker,
        session_factory() as publisher,
        session_factory() as observer,
    ):
        blocker_tx = await blocker.begin()
        publisher_tx = await publisher.begin()
        task = None
        try:
            blocker_pid = await blocker.scalar(sa.text("SELECT pg_backend_pid()"))
            await blocker.execute(
                sa.select(decisions.c.id).where(decisions.c.id == source_id).with_for_update()
            )
            publisher_pid = await publisher.scalar(sa.text("SELECT pg_backend_pid()"))
            transaction_time = await publisher.scalar(sa.select(sa.func.transaction_timestamp()))
            assert isinstance(transaction_time, datetime)
            task = asyncio.create_task(
                repo.publish_observation(
                    publisher, binding.id, binding.binding_version, evidence, at, at
                )
            )
            await _wait_for_blocker(
                observer, waiter_pid=publisher_pid, blocker_pid=blocker_pid, task=task
            )
            await asyncio.sleep(1.1)
            release_time = await _now(blocker)
            assert transaction_time < at + timedelta(seconds=1) <= release_time
            await blocker_tx.commit()
            assert (await asyncio.wait_for(task, timeout=5)).id is not None
            await publisher_tx.commit()
        finally:
            await _cancel(task)
            if blocker_tx.is_active:
                await blocker_tx.rollback()
            if publisher_tx.is_active:
                await publisher_tx.rollback()
    assert await _rows(session_factory, ticket.id) == []
    async with session_factory() as session:
        persisted = await session.scalar(
            sa.select(sa.func.count())
            .select_from(delivery_confirmations)
            .where(delivery_confirmations.c.binding_id == binding.id)
        )
        pointer = await session.scalar(
            sa.select(delivery_artifact_bindings.c.latest_success_confirmation_id).where(
                delivery_artifact_bindings.c.id == binding.id
            )
        )
    assert persisted == 1
    assert pointer is not None


async def test_publisher_waits_for_lower_upstream_before_its_current_ticket(session_factory):
    """The publisher uses the common complete UUID order, leaving a higher current ticket free."""
    ticket_repo = PgTicketRepo(session_factory)
    candidates = [
        await ticket_repo.create(
            TicketCreate(
                kind=TicketKind.REQUEST,
                title=f"publisher lock {uuid4()}",
                body="real lock order",
                from_project="brain-v42",
                to_project="brain-v42",
            )
        )
        for _ in range(2)
    ]
    upstream, current = sorted(candidates, key=lambda ticket: str(ticket.id))
    assert str(upstream.id) < str(current.id)
    service = DeliveryService(
        PgDeliveryRepo(session_factory),
        settings=DeliverySettings(enabled=True, freshness_seconds=3600),
    )
    for ticket, dependencies in (
        (upstream, ()),
        (
            current,
            (
                DeliveryDependency(
                    ticket_id=upstream.id, contract_revision=1, attempt=1, milestone="integrated"
                ),
            ),
        ),
    ):
        await service.set_contract(
            ticket.id,
            actor_project="brain-v42",
            expected_revision=0,
            idempotency_key=f"publisher-lock-contract-{ticket.id}",
            contract=_contract(dependencies=dependencies),
        )
    upstream_binding = await service.bind_pr(
        upstream.id,
        actor_project="brain-v42",
        deliverable_key="implementation",
        repository_id=RID,
        pr_number=41,
        expected_revision=1,
        expected_workflow_version=1,
        idempotency_key=f"publisher-lock-binding-{upstream.id}",
    )
    current_binding = await service.bind_pr(
        current.id,
        actor_project="brain-v42",
        deliverable_key="implementation",
        repository_id=RID,
        pr_number=42,
        expected_revision=1,
        expected_workflow_version=1,
        idempotency_key=f"publisher-lock-binding-{current.id}",
    )
    repo = _issuer(session_factory)
    async with session_factory() as session:
        async with session.begin():
            now = await _now(session)
            await repo.publish_observation(
                session,
                upstream_binding.id,
                upstream_binding.binding_version,
                _evidence(now, pr_number=41),
                now,
                now,
            )
            await repo.publish_observation(
                session,
                current_binding.id,
                current_binding.binding_version,
                _evidence(now),
                now,
                now,
            )
    async with (
        session_factory() as blocker,
        session_factory() as publisher,
        session_factory() as probe,
        session_factory() as observer,
    ):
        blocker_tx = await blocker.begin()
        publisher_tx = await publisher.begin()
        task = None
        try:
            blocker_pid = await blocker.scalar(sa.text("SELECT pg_backend_pid()"))
            await blocker.execute(
                sa.select(tickets.c.id).where(tickets.c.id == upstream.id).with_for_update()
            )
            publisher_pid = await publisher.scalar(sa.text("SELECT pg_backend_pid()"))
            now = await _now(publisher)
            task = asyncio.create_task(
                repo.publish_observation(
                    publisher,
                    current_binding.id,
                    current_binding.binding_version + 1,
                    _evidence(now),
                    now,
                    now,
                )
            )
            await _wait_for_blocker(
                observer, waiter_pid=publisher_pid, blocker_pid=blocker_pid, task=task
            )
            await probe.execute(
                sa.select(tickets.c.id)
                .where(tickets.c.id == current.id)
                .with_for_update(nowait=True)
            )
        finally:
            await _cancel(task)
            if publisher_tx.is_active:
                await publisher_tx.rollback()
            if blocker_tx.is_active:
                await blocker_tx.rollback()
