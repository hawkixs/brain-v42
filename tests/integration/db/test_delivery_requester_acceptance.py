"""Real PostgreSQL contracts for explicit requester acceptance of delivery proof."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import datetime, timedelta
from uuid import uuid4

import pytest
import sqlalchemy as sa

from brain_v42.db.tables import decisions, delivery_receipts, delivery_workflows
from brain_v42.delivery_config import DeliverySettings
from brain_v42.models.delivery import (
    DeliveryDependency,
    DeliveryError,
    PullRequestEvidence,
    ReceiptIssuerProvenance,
)
from brain_v42.models.delivery_evaluator import evaluate_delivery
from brain_v42.models.ticket import TicketCreate, TicketKind
from brain_v42.repositories.pg_delivery import PgDeliveryRepo
from brain_v42.repositories.pg_delivery_evidence import PgDeliveryEvidenceRepo
from brain_v42.repositories.pg_ticket import PgTicketRepo
from brain_v42.services.delivery_service import DeliveryService

from .test_delivery_receipt_issuance import RID, A, B, C, _contract

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


def _settings(*, enabled: bool = True, freshness: int = 3600) -> DeliverySettings:
    return DeliverySettings(
        enabled=enabled,
        freshness_seconds=freshness,
        repository_registry={
            "brain-v42": {RID: "hawkixs/brain-v42"},
            "executor": {RID: "hawkixs/brain-v42"},
        },
    )


def _service(factory, *, enabled: bool = True, freshness: int = 3600) -> DeliveryService:
    """Keep setup on the existing public constructor until accept is introduced."""
    return DeliveryService(
        PgDeliveryRepo(factory), settings=_settings(enabled=enabled, freshness=freshness)
    )


def _observer(factory, *, freshness: int = 3600) -> PgDeliveryEvidenceRepo:
    return PgDeliveryEvidenceRepo(
        factory,
        settings=_settings(freshness=freshness),
        observer_provenance=ReceiptIssuerProvenance(
            issuer_project="brain-v42", issuer_identity="delivery-observer", issuer_kind="observer"
        ),
    )


async def _now(session) -> datetime:
    value = await session.scalar(sa.select(sa.func.clock_timestamp()))
    assert isinstance(value, datetime)
    return value


async def _workflow(
    factory,
    *,
    mode: str = "explicit",
    requester: str = "requester",
    executor: str = "executor",
    **contract_options,
):
    ticket = await PgTicketRepo(factory).create(
        TicketCreate(
            kind=TicketKind.REQUEST,
            title=f"requester acceptance {uuid4()}",
            body="real PG acceptance fixture",
            from_project=requester,
            to_project=executor,
        )
    )
    service = _service(factory)
    await service.set_contract(
        ticket.id,
        actor_project=requester,
        expected_revision=0,
        idempotency_key=f"contract-{ticket.id}",
        contract=_contract(mode=mode, **contract_options),
    )
    binding = await service.bind_pr(
        ticket.id,
        actor_project=executor,
        deliverable_key="implementation",
        repository_id=RID,
        pr_number=42,
        expected_revision=1,
        expected_workflow_version=1,
        idempotency_key=f"binding-{ticket.id}",
    )
    return ticket, binding, service


def _proof(now: datetime) -> PullRequestEvidence:
    return PullRequestEvidence(
        provider_id=7,
        repository_id=RID,
        pr_number=42,
        author_id="executor",
        head_repository_id=8,
        head_sha=A,
        base_sha=B,
        base_ref="main",
        state="merged",
        draft=False,
        complete=True,
        integration_sha=C,
        collected_at=now,
    )


async def _publish_and_issue(factory, ticket_id, binding, *, freshness: int = 3600):
    async with factory() as session:
        async with session.begin():
            now = await _now(session)
            await PgDeliveryEvidenceRepo(factory).publish_observation(
                session, binding.id, binding.binding_version, _proof(now), now, now
            )
            integration = await _observer(factory, freshness=freshness).issue_integration_receipt(
                session, ticket_id
            )
            assert integration is not None
            return integration


async def _fulfilled_rows(factory, ticket_id):
    async with factory() as session:
        return (
            (
                await session.execute(
                    sa.select(delivery_receipts)
                    .where(
                        delivery_receipts.c.ticket_id == ticket_id,
                        delivery_receipts.c.milestone == "fulfilled",
                    )
                    .order_by(delivery_receipts.c.issued_at)
                )
            )
            .mappings()
            .all()
        )


async def _accept(service, ticket_id, integration, **overrides):
    values = {
        "actor_project": "requester",
        "caller_identity": "requester-mcp-client",
        "rationale": "The exact merged delivery meets the agreed acceptance criteria.",
        "expected_revision": integration.contract_revision,
        "expected_attempt": integration.attempt,
        "expected_delivery_digest": integration.delivery_digest,
    }
    values.update(overrides)
    return await service.accept(ticket_id, **values)


async def test_requester_accepts_exact_current_integration_with_frozen_provenance(session_factory):
    ticket, binding, service = await _workflow(session_factory)
    integration = await _publish_and_issue(session_factory, ticket.id, binding)

    receipt = await _accept(service, ticket.id, integration)

    assert receipt is not None
    assert receipt.milestone == "fulfilled"
    assert receipt.acceptance_basis == "explicit"
    assert receipt.proof.issuer.issuer_kind == "requester"
    assert receipt.proof.issuer.issuer_identity == "requester-mcp-client"
    assert receipt.explicit_acceptance is not None
    assert receipt.explicit_acceptance.requester_project == "requester"


@pytest.mark.parametrize(
    "overrides",
    (
        {"actor_project": "executor"},
        {"actor_project": "outsider"},
        {"caller_identity": "   "},
        {"rationale": "\t"},
    ),
)
async def test_accept_rejects_non_requester_or_blank_declared_provenance(
    session_factory, overrides
):
    ticket, binding, service = await _workflow(session_factory)
    integration = await _publish_and_issue(session_factory, ticket.id, binding)

    with pytest.raises(DeliveryError):
        await _accept(service, ticket.id, integration, **overrides)

    assert await _fulfilled_rows(session_factory, ticket.id) == []


@pytest.mark.parametrize(
    "overrides",
    (
        {"expected_revision": 2},
        {"expected_attempt": 2},
        {"expected_delivery_digest": "d" * 64},
    ),
)
async def test_accept_requires_exact_current_generation_and_digest(session_factory, overrides):
    ticket, binding, service = await _workflow(session_factory)
    integration = await _publish_and_issue(session_factory, ticket.id, binding)

    with pytest.raises(DeliveryError, match="generation_conflict|acceptance_not_eligible"):
        await _accept(service, ticket.id, integration, **overrides)
    assert await _fulfilled_rows(session_factory, ticket.id) == []


async def test_accept_requires_enabled_explicit_policy_and_matching_integration(session_factory):
    ticket, binding, service = await _workflow(session_factory, mode="explicit")
    async with session_factory() as session:
        async with session.begin():
            now = await _now(session)
            await PgDeliveryEvidenceRepo(session_factory).publish_observation(
                session, binding.id, binding.binding_version, _proof(now), now, now
            )
            inputs = await PgDeliveryRepo(session_factory).load_inputs(
                ticket.id, feature_enabled=True, freshness_seconds=3600, session=session
            )
            assert inputs is not None and inputs.integration_receipt is None
            current = type(
                "Current",
                (),
                {
                    "contract_revision": inputs.contract.contract_revision,
                    "attempt": inputs.attempt,
                    "delivery_digest": evaluate_delivery(inputs, now=now).delivery_digest,
                },
            )()
    with pytest.raises(DeliveryError, match="acceptance_not_eligible"):
        await _accept(service, ticket.id, current)
    async with session_factory() as session:
        async with session.begin():
            integration = await _observer(session_factory).issue_integration_receipt(
                session, ticket.id
            )
            assert integration is not None
    with pytest.raises(DeliveryError):
        await _accept(_service(session_factory, enabled=False), ticket.id, current)
    automatic, automatic_binding, automatic_service = await _workflow(
        session_factory, mode="automatic"
    )
    automatic_integration = await _publish_and_issue(
        session_factory, automatic.id, automatic_binding
    )
    with pytest.raises(DeliveryError, match="acceptance_not_eligible"):
        await _accept(automatic_service, automatic.id, automatic_integration)


async def test_accept_rechecks_current_freshness_and_provider_error_without_erasing_history(
    session_factory,
):
    ticket, binding, service = await _workflow(session_factory)
    integration = await _publish_and_issue(session_factory, ticket.id, binding, freshness=1)
    accepted = await _accept(_service(session_factory, freshness=1), ticket.id, integration)
    assert accepted is not None
    historical = await _fulfilled_rows(session_factory, ticket.id)
    async with session_factory() as session:
        async with session.begin():
            now = await _now(session)
            await PgDeliveryEvidenceRepo(session_factory).record_observation_error(
                session, binding.id, binding.binding_version + 1, "provider_unavailable", now, now
            )

    with pytest.raises(DeliveryError, match="acceptance_not_eligible"):
        await _accept(_service(session_factory, freshness=1), ticket.id, integration)
    assert [
        (row["id"], row["payload"], row["issued_at"])
        for row in await _fulfilled_rows(session_factory, ticket.id)
    ] == [(row["id"], row["payload"], row["issued_at"]) for row in historical]
    view = await PgDeliveryRepo(session_factory).get_view(
        ticket.id, feature_enabled=True, freshness_seconds=1
    )
    assert view is not None and view.integration_receipt is not None
    assert view.fulfillment_receipt is not None and view.assessment.completion_eligible_now is False


async def test_accept_replay_is_immutable_and_caller_rollback_owns_transaction(session_factory):
    ticket, binding, service = await _workflow(session_factory)
    integration = await _publish_and_issue(session_factory, ticket.id, binding)
    async with session_factory() as session:
        transaction = await session.begin()
        try:
            direct = await PgDeliveryEvidenceRepo(session_factory).accept(
                session,
                ticket.id,
                settings=_settings(),
                actor_project="requester",
                caller_identity="requester-mcp-client",
                rationale="rollback acceptance",
                expected_revision=integration.contract_revision,
                expected_attempt=integration.attempt,
                expected_delivery_digest=integration.delivery_digest,
            )
            assert direct is not None
            assert (
                await session.scalar(
                    sa.select(sa.func.count())
                    .select_from(delivery_receipts)
                    .where(
                        delivery_receipts.c.ticket_id == ticket.id,
                        delivery_receipts.c.milestone == "fulfilled",
                    )
                )
                == 1
            )
        finally:
            await transaction.rollback()
    assert await _fulfilled_rows(session_factory, ticket.id) == []
    async with session_factory() as session:
        assert (
            await session.scalar(
                sa.select(sa.func.count())
                .select_from(delivery_receipts)
                .where(
                    delivery_receipts.c.ticket_id == ticket.id,
                    delivery_receipts.c.milestone == "integration",
                )
            )
            == 1
        )
    first = await _accept(service, ticket.id, integration)
    assert first is not None
    before_replay = await _fulfilled_rows(session_factory, ticket.id)
    replay = await _accept(
        service,
        ticket.id,
        integration,
        caller_identity="different-requester-client",
        rationale="Changed rationale must not rewrite proof.",
    )
    assert replay == first
    assert [
        (row["id"], row["payload"], row["issued_at"])
        for row in await _fulfilled_rows(session_factory, ticket.id)
    ] == [(row["id"], row["payload"], row["issued_at"]) for row in before_replay]


async def test_accept_rechecks_changed_required_brain_context_before_fulfillment(session_factory):
    async with session_factory() as session:
        async with session.begin():
            source = (
                await session.execute(
                    decisions.insert()
                    .values(title="accept context", description="before", reasoning="test")
                    .returning(decisions.c.id)
                )
            ).scalar_one()
    ticket, binding, service = await _workflow(session_factory, source=source)
    integration = await _publish_and_issue(session_factory, ticket.id, binding)
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                sa.update(decisions).where(decisions.c.id == source).values(description="after")
            )

    with pytest.raises(DeliveryError, match="generation_conflict|acceptance_not_eligible"):
        await _accept(service, ticket.id, integration)
    assert await _fulfilled_rows(session_factory, ticket.id) == []


async def test_accept_rechecks_rebound_binding_before_fulfillment(session_factory):
    ticket, binding, service = await _workflow(session_factory)
    integration = await _publish_and_issue(session_factory, ticket.id, binding)
    inputs = await PgDeliveryRepo(session_factory).load_inputs(
        ticket.id, feature_enabled=True, freshness_seconds=3600
    )
    assert inputs is not None
    await service.bind_pr(
        ticket.id,
        actor_project="executor",
        deliverable_key="implementation",
        repository_id=RID,
        pr_number=43,
        expected_revision=1,
        expected_workflow_version=inputs.workflow_version,
        idempotency_key=f"rebind-drift-{ticket.id}",
    )
    with pytest.raises(DeliveryError, match="generation_conflict|acceptance_not_eligible"):
        await _accept(service, ticket.id, integration)
    assert await _fulfilled_rows(session_factory, ticket.id) == []


async def test_accept_rechecks_amended_dependency_before_fulfillment(session_factory):
    upstream, upstream_binding, upstream_service = await _workflow(session_factory)
    upstream_integration = await _publish_and_issue(session_factory, upstream.id, upstream_binding)
    ticket, binding, service = await _workflow(
        session_factory,
        dependencies=(
            DeliveryDependency(
                ticket_id=upstream.id,
                contract_revision=upstream_integration.contract_revision,
                attempt=upstream_integration.attempt,
                milestone="integrated",
            ),
        ),
    )
    integration = await _publish_and_issue(session_factory, ticket.id, binding)
    await upstream_service.set_contract(
        upstream.id,
        actor_project="requester",
        expected_revision=1,
        idempotency_key=f"amend-dependency-{upstream.id}",
        contract=_contract(mode="explicit"),
    )
    with pytest.raises(DeliveryError, match="generation_conflict|acceptance_not_eligible"):
        await _accept(service, ticket.id, integration)
    assert await _fulfilled_rows(session_factory, ticket.id) == []


async def test_accept_reads_clock_after_lock_wait_and_rejects_expired_evidence(session_factory):
    ticket, binding, service = await _workflow(session_factory)
    integration = await _publish_and_issue(session_factory, ticket.id, binding, freshness=1)
    async with (
        session_factory() as blocker,
        session_factory() as requester,
        session_factory() as observer,
    ):
        first = await blocker.begin()
        second = await requester.begin()
        try:
            await blocker.execute(
                sa.select(delivery_workflows.c.ticket_id)
                .where(delivery_workflows.c.ticket_id == ticket.id)
                .with_for_update()
            )
            blocker_pid = await blocker.scalar(sa.text("SELECT pg_backend_pid()"))
            requester_pid = await requester.scalar(sa.text("SELECT pg_backend_pid()"))
            before_wait = await requester.scalar(sa.select(sa.func.transaction_timestamp()))
            assert isinstance(before_wait, datetime)
            task = asyncio.create_task(
                PgDeliveryEvidenceRepo(session_factory).accept(
                    requester,
                    ticket.id,
                    settings=_settings(freshness=1),
                    actor_project="requester",
                    caller_identity="requester-mcp-client",
                    rationale="wait across expiry",
                    expected_revision=integration.contract_revision,
                    expected_attempt=integration.attempt,
                    expected_delivery_digest=integration.delivery_digest,
                )
            )
            await _wait_for_blocker(observer, requester_pid, blocker_pid, task)
            await asyncio.sleep(1.1)
            released = await blocker.scalar(sa.select(sa.func.clock_timestamp()))
            deadline = integration.proof.artifact_proofs[0].collection_finished_at + timedelta(
                seconds=1
            )
            assert before_wait < deadline <= released
            await first.commit()
            with pytest.raises(DeliveryError, match="acceptance_not_eligible"):
                await asyncio.wait_for(task, 5)
            await second.commit()
        finally:
            if "task" in locals() and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            if blocker.in_transaction():
                await blocker.rollback()
            if requester.in_transaction():
                await requester.rollback()
    assert await _fulfilled_rows(session_factory, ticket.id) == []


async def _wait_for_blocker(
    observer, waiter_pid: int, blocker_pid: int, task: asyncio.Task[object]
) -> None:
    for _ in range(100):
        if blocker_pid in await observer.scalar(
            sa.text("SELECT pg_blocking_pids(:pid)"), {"pid": waiter_pid}
        ):
            assert not task.done()
            return
        await asyncio.sleep(0.01)
    raise AssertionError("PostgreSQL did not report the expected acceptance wait")


async def test_accept_revalidates_when_amendment_wins_the_workflow_lock(session_factory):
    ticket, binding, service = await _workflow(session_factory)
    integration = await _publish_and_issue(session_factory, ticket.id, binding)
    repo = PgDeliveryRepo(session_factory)
    async with (
        session_factory() as mutator,
        session_factory() as requester,
        session_factory() as observer,
    ):
        transaction = await mutator.begin()
        accept: asyncio.Task[object] | None = None
        try:
            inputs = await repo.load_inputs(
                ticket.id, feature_enabled=True, freshness_seconds=3600, session=mutator
            )
            assert inputs is not None
            amended = inputs.contract.model_copy(
                update={
                    "contract_revision": 2,
                    "created_at": await _now(mutator),
                    "amendment_reason": "race amendment",
                }
            )
            await repo.set_contract(
                amended,
                expected_revision=1,
                actor_project="requester",
                idempotency_key=f"amend-race-{ticket.id}",
                request_digest="a" * 64,
                session=mutator,
            )
            mutator_pid = await mutator.scalar(sa.text("SELECT pg_backend_pid()"))
            requester_pid = await requester.scalar(sa.text("SELECT pg_backend_pid()"))
            accept = asyncio.create_task(
                PgDeliveryEvidenceRepo(session_factory).accept(
                    requester,
                    ticket.id,
                    settings=_settings(),
                    actor_project="requester",
                    caller_identity="requester-mcp-client",
                    rationale="race",
                    expected_revision=1,
                    expected_attempt=1,
                    expected_delivery_digest=integration.delivery_digest,
                )
            )
            await _wait_for_blocker(observer, requester_pid, mutator_pid, accept)
            await transaction.commit()
            with pytest.raises(DeliveryError, match="generation_conflict|acceptance_not_eligible"):
                await asyncio.wait_for(accept, 5)
        finally:
            for task in (accept,):
                if task is not None and not task.done():
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
            if mutator.in_transaction():
                await mutator.rollback()
    assert await _fulfilled_rows(session_factory, ticket.id) == []


async def test_accept_revalidates_when_rebind_wins_the_workflow_lock(session_factory):
    ticket, binding, service = await _workflow(session_factory)
    integration = await _publish_and_issue(session_factory, ticket.id, binding)
    repo = PgDeliveryRepo(session_factory)
    async with (
        session_factory() as mutator,
        session_factory() as requester,
        session_factory() as observer,
    ):
        transaction = await mutator.begin()
        accept: asyncio.Task[object] | None = None
        try:
            inputs = await repo.load_inputs(
                ticket.id, feature_enabled=True, freshness_seconds=3600, session=mutator
            )
            assert inputs is not None
            rebound = binding.model_copy(update={"pr_number": 43})
            await repo.bind_pr(
                rebound,
                repository_name="hawkixs/brain-v42",
                actor_project="executor",
                expected_revision=1,
                expected_workflow_version=inputs.workflow_version,
                idempotency_key=f"rebind-race-{ticket.id}",
                request_digest="b" * 64,
                session=mutator,
            )
            mutator_pid = await mutator.scalar(sa.text("SELECT pg_backend_pid()"))
            requester_pid = await requester.scalar(sa.text("SELECT pg_backend_pid()"))
            accept = asyncio.create_task(
                PgDeliveryEvidenceRepo(session_factory).accept(
                    requester,
                    ticket.id,
                    settings=_settings(),
                    actor_project="requester",
                    caller_identity="requester-mcp-client",
                    rationale="race",
                    expected_revision=1,
                    expected_attempt=1,
                    expected_delivery_digest=integration.delivery_digest,
                )
            )
            await _wait_for_blocker(observer, requester_pid, mutator_pid, accept)
            await transaction.commit()
            with pytest.raises(DeliveryError, match="generation_conflict|acceptance_not_eligible"):
                await asyncio.wait_for(accept, 5)
        finally:
            for task in (accept,):
                if task is not None and not task.done():
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
            if mutator.in_transaction():
                await mutator.rollback()
    assert await _fulfilled_rows(session_factory, ticket.id) == []
