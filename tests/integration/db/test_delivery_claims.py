"""Real PostgreSQL RED contracts for fenced delivery claims."""

from __future__ import annotations

import asyncio
import importlib
from contextlib import suppress
from datetime import datetime
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa

from brain_v42.db.tables import decisions, delivery_workflows
from brain_v42.delivery_config import DeliverySettings
from brain_v42.models.delivery import (
    BrainEntityReference,
    ContractInput,
    Deliverable,
    DeliveryDependency,
    DeliveryError,
    PullRequestEvidence,
    ReceiptIssuerProvenance,
    ReviewPolicy,
)
from brain_v42.models.ticket import TicketCreate, TicketKind
from brain_v42.repositories.pg_delivery import PgDeliveryRepo
from brain_v42.repositories.pg_delivery_evidence import PgDeliveryEvidenceRepo
from brain_v42.repositories.pg_ticket import PgTicketRepo
from brain_v42.services.delivery_service import DeliveryService

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

RID = 1_337_360_966
A, B, C = "a" * 40, "b" * 40, "c" * 40


def _contract(*, source: UUID | None = None, dependencies=()) -> ContractInput:
    return ContractInput(
        objective="fence one mutable delivery work item",
        priority=1,
        acceptance_mode="automatic",
        context_refs=(
            ()
            if source is None
            else (
                BrainEntityReference(kind="brain_entity", entity_type="decision", entity_id=source),
            )
        ),
        dependencies=tuple(dependencies),
        deliverables=(
            Deliverable(
                key="implementation",
                repository="hawkixs/brain-v42",
                target_branch="main",
                required_checks=(),
                no_checks_reason="claim test",
                review=ReviewPolicy(required_approvals=0, allowed_reviewers=()),
            ),
        ),
    )


def _service(factory, *, enabled: bool = True, freshness: int = 3600) -> DeliveryService:
    return DeliveryService(
        PgDeliveryRepo(factory),
        settings=DeliverySettings(enabled=enabled, freshness_seconds=freshness),
    )


async def _workflow(
    factory,
    *,
    requester: str = "requester",
    source: UUID | None = None,
    enabled: bool = True,
    freshness: int = 3600,
) -> tuple[object, DeliveryService]:
    ticket = await PgTicketRepo(factory).create(
        TicketCreate(
            kind=TicketKind.REQUEST,
            title=f"claims {uuid4()}",
            body="real PostgreSQL claim fixture",
            from_project=requester,
            to_project="brain-v42",
        )
    )
    service = _service(factory, enabled=enabled, freshness=freshness)
    await service.set_contract(
        ticket.id,
        actor_project=requester,
        expected_revision=0,
        idempotency_key=f"claim-contract-{ticket.id}",
        contract=_contract(source=source),
    )
    return ticket, service


async def _bind_observation(
    factory, ticket_id: UUID, service: DeliveryService, *, state: str = "open"
):
    binding = await service.bind_pr(
        ticket_id,
        actor_project="brain-v42",
        deliverable_key="implementation",
        repository_id=RID,
        pr_number=42,
        expected_revision=1,
        expected_workflow_version=1,
        idempotency_key=f"claim-binding-{ticket_id}",
    )
    await _publish_observation(factory, binding, state=state)
    return binding


async def _publish_observation(
    factory, binding, *, state: str = "open", draft: bool = False
) -> None:
    async with factory() as session:
        async with session.begin():
            now = await session.scalar(sa.select(sa.func.clock_timestamp()))
            assert isinstance(now, datetime)
            await PgDeliveryEvidenceRepo(factory).publish_observation(
                session,
                binding.id,
                1,
                PullRequestEvidence(
                    provider_id=7,
                    repository_id=RID,
                    pr_number=binding.pr_number,
                    author_id="executor",
                    head_repository_id=8,
                    head_sha=A,
                    base_sha=B,
                    base_ref="main",
                    state=state,
                    draft=draft,
                    mergeable=True,
                    complete=True,
                    integration_sha=C,
                    collected_at=now,
                ),
                now,
                now,
            )


async def _bind_open_observation(factory, ticket_id: UUID, service: DeliveryService) -> None:
    await _bind_observation(factory, ticket_id, service)


async def _issue_integration_receipt(factory, ticket_id: UUID) -> None:
    issuer = PgDeliveryEvidenceRepo(
        factory,
        settings=DeliverySettings(enabled=True, freshness_seconds=3600),
        observer_provenance=ReceiptIssuerProvenance(
            issuer_project="brain-v42",
            issuer_identity="claim-test-observer",
            issuer_kind="observer",
        ),
    )
    async with factory() as session:
        async with session.begin():
            receipt = await issuer.issue_integration_receipt(session, ticket_id)
    assert receipt is not None and receipt.milestone == "integration"


async def _view(service: DeliveryService, ticket_id: UUID):
    return await service.get(ticket_id, actor_project="brain-v42")


async def _claim(
    service: DeliveryService,
    ticket_id: UUID,
    *,
    actor: str = "brain-v42",
    owner: str = "orchestrator-a",
    work: str = "implement",
    ttl: int = 900,
):
    """Call the public boundary after proving the existing fixture is valid."""
    view = await _view(service, ticket_id)
    return await service.claim(
        ticket_id,
        actor_project=actor,
        owner_key=owner,
        work_kind=work,
        expected_workflow_version=view.assessment.assessment_version,
        expected_assessment_id=view.assessment.assessment_id,
        ttl_seconds=ttl,
    )


async def _renew(service: DeliveryService, ticket_id: UUID, claim, *, actor="brain-v42", ttl=900):
    return await service.renew_claim(
        ticket_id,
        actor_project=actor,
        owner_key="orchestrator-a",
        claim_token=claim.claim_token,
        epoch=claim.epoch,
        ttl_seconds=ttl,
    )


async def _release(service: DeliveryService, ticket_id: UUID, claim, *, actor="brain-v42"):
    return await service.release_claim(
        ticket_id,
        actor_project=actor,
        owner_key="orchestrator-a",
        claim_token=claim.claim_token,
        epoch=claim.epoch,
    )


async def _claim_row(factory, ticket_id: UUID) -> dict[str, object]:
    async with factory() as session:
        row = (
            (
                await session.execute(
                    sa.select(
                        delivery_workflows.c.claim_owner,
                        delivery_workflows.c.claim_kind,
                        delivery_workflows.c.claim_digest,
                        delivery_workflows.c.claim_expires_at,
                        delivery_workflows.c.claim_epoch,
                        delivery_workflows.c.row_version,
                    ).where(delivery_workflows.c.ticket_id == ticket_id)
                )
            )
            .mappings()
            .one()
        )
    return dict(row)


async def _wait_for_blocker(observer, *, waiter_pid: int, blocker_pid: int, task) -> None:
    """Require PostgreSQL's wait graph to prove the race has occurred."""
    for _ in range(100):
        blockers = await observer.scalar(
            sa.text("SELECT pg_blocking_pids(:pid)"), {"pid": waiter_pid}
        )
        if blocker_pid in blockers:
            assert not task.done()
            return
        if task.done():
            await task
            raise AssertionError("claim operation completed without the required lock wait")
        await asyncio.sleep(0.01)
    raise AssertionError("PostgreSQL did not report the required lock wait")


async def _cancel(task) -> None:
    if task is not None and not task.done():
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def test_only_one_owner_acquires_current_implementation_work(session_factory) -> None:
    """Dropping the current-owner predicate would grant two active owners."""
    ticket, service = await _workflow(session_factory)
    results = await asyncio.gather(
        _claim(service, ticket.id, owner="orchestrator-a"),
        _claim(service, ticket.id, owner="orchestrator-b"),
        return_exceptions=True,
    )
    acquired = [result for result in results if not isinstance(result, Exception)]
    assert len(acquired) == 1
    row = await _claim_row(session_factory, ticket.id)
    assert row["claim_owner"] in {"orchestrator-a", "orchestrator-b"}


async def test_expired_claim_reacquisition_fences_old_token_and_epoch(session_factory) -> None:
    """Keeping an expired digest valid would let a former owner mutate current work."""
    ticket, service = await _workflow(session_factory)
    first = await _claim(service, ticket.id)
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                delivery_workflows.update()
                .where(delivery_workflows.c.ticket_id == ticket.id)
                .values(claim_expires_at=sa.func.clock_timestamp() - sa.text("interval '1 second'"))
            )
    second = await _claim(service, ticket.id, owner="orchestrator-b")
    assert second.epoch > first.epoch
    with pytest.raises(DeliveryError):
        await _renew(service, ticket.id, first)
    with pytest.raises(DeliveryError):
        await _release(service, ticket.id, first)


async def test_release_then_reacquire_always_advances_the_fencing_epoch(session_factory) -> None:
    """Reusing an epoch after release permits an ABA lease to impersonate its predecessor."""
    ticket, service = await _workflow(session_factory)
    first = await _claim(service, ticket.id)
    await _release(service, ticket.id, first)
    second = await _claim(service, ticket.id, owner="orchestrator-b")
    assert second.epoch > first.epoch


@pytest.mark.parametrize("owner", ["x" * 201, "   "])
async def test_claim_rejects_unsafe_owner_before_any_database_write(
    session_factory, owner: str
) -> None:
    """Passing an oversized owner to PostgreSQL can leak claim parameters through a driver error."""
    ticket, service = await _workflow(session_factory)
    before = await _claim_row(session_factory, ticket.id)
    with pytest.raises(DeliveryError) as error:
        await _claim(service, ticket.id, owner=owner)
    assert owner not in str(error.value)
    assert await _claim_row(session_factory, ticket.id) == before


@pytest.mark.parametrize("ttl", [True, 900.0])
async def test_claim_rejects_non_integer_ttl_without_writing_a_lease(session_factory, ttl) -> None:
    """Coercing bool or float TTLs changes lease boundaries before the caller can see the refusal."""
    ticket, service = await _workflow(session_factory)
    before = await _claim_row(session_factory, ticket.id)
    with pytest.raises(DeliveryError):
        await _claim(service, ticket.id, ttl=ttl)
    assert await _claim_row(session_factory, ticket.id) == before


async def test_renew_and_release_succeed_without_returning_a_bearer_token(session_factory) -> None:
    """A successful lease lifecycle must change stored state without reissuing its secret."""
    ticket, service = await _workflow(session_factory)
    claim = await _claim(service, ticket.id)
    before = await _claim_row(session_factory, ticket.id)
    renewed = await _renew(service, ticket.id, claim, ttl=1200)
    after_renew = await _claim_row(session_factory, ticket.id)
    assert after_renew["claim_owner"] == before["claim_owner"] == "orchestrator-a"
    assert after_renew["claim_digest"] == before["claim_digest"]
    assert after_renew["claim_epoch"] == before["claim_epoch"] == claim.epoch
    assert after_renew["claim_expires_at"] > before["claim_expires_at"]
    assert not hasattr(renewed, "claim_token")
    released = await _release(service, ticket.id, claim)
    after_release = await _claim_row(session_factory, ticket.id)
    assert after_release["claim_owner"] is None
    assert after_release["claim_kind"] is None
    assert after_release["claim_digest"] is None
    assert after_release["claim_expires_at"] is None
    assert not hasattr(released, "claim_token")


async def test_live_claim_rejects_bad_token_and_bad_epoch_independently(session_factory) -> None:
    """Checking only owner or expiry would let one wrong fence value pass on a live lease."""
    ticket, service = await _workflow(session_factory)
    claim = await _claim(service, ticket.id)
    before = await _claim_row(session_factory, ticket.id)
    with pytest.raises(DeliveryError):
        await service.renew_claim(
            ticket.id,
            actor_project="brain-v42",
            owner_key="orchestrator-a",
            claim_token="not-the-live-token",
            epoch=claim.epoch,
            ttl_seconds=900,
        )
    with pytest.raises(DeliveryError):
        await service.release_claim(
            ticket.id,
            actor_project="brain-v42",
            owner_key="orchestrator-a",
            claim_token=claim.claim_token,
            epoch=claim.epoch + 1,
        )
    assert await _claim_row(session_factory, ticket.id) == before


@pytest.mark.parametrize(("token", "epoch"), [(123, 1), ("unexpected-bearer", "1")])
async def test_invalid_token_or_epoch_is_a_redacted_domain_refusal(
    session_factory, token, epoch
) -> None:
    """Malformed credentials must not raise driver/type errors or echo a supplied bearer string."""
    ticket, service = await _workflow(session_factory)
    await _claim(service, ticket.id)
    with pytest.raises(DeliveryError) as error:
        await service.release_claim(
            ticket.id,
            actor_project="brain-v42",
            owner_key="orchestrator-a",
            claim_token=token,
            epoch=epoch,
        )
    assert "unexpected-bearer" not in str(error.value)
    assert (await _claim_row(session_factory, ticket.id))["claim_owner"] == "orchestrator-a"


@pytest.mark.parametrize("work", ["accept", "repair", "review", "integrate"])
async def test_claim_rejects_wrong_role_or_unavailable_work_kind(
    session_factory, work: str
) -> None:
    """Trusting caller work labels would bypass the evaluator's role and work fences."""
    ticket, service = await _workflow(session_factory)
    actor = "requester" if work == "accept" else "brain-v42"
    with pytest.raises(DeliveryError):
        await _claim(service, ticket.id, actor=actor, work=work)


@pytest.mark.parametrize("actor", ["requester", "outside-project"])
async def test_claim_rejects_wrong_actor_for_eligible_implementation(
    session_factory, actor: str
) -> None:
    """Comparing only owner labels would let a non-executor take eligible implementation work."""
    ticket, service = await _workflow(session_factory)
    view = await _view(service, ticket.id)
    assert {(item.kind, item.role) for item in view.assessment.eligible_work} == {
        ("implement", "executor")
    }
    with pytest.raises(DeliveryError, match="not_allowed"):
        await service.claim(
            ticket.id,
            actor_project=actor,
            owner_key="orchestrator-a",
            work_kind="implement",
            expected_workflow_version=view.assessment.assessment_version,
            expected_assessment_id=view.assessment.assessment_id,
        )


@pytest.mark.parametrize("ttl", [59, 3601])
async def test_claim_rejects_ttl_outside_bounded_lease_window(session_factory, ttl: int) -> None:
    """Accepting an unbounded TTL can turn a temporary lease into permanent ownership."""
    ticket, service = await _workflow(session_factory)
    with pytest.raises(DeliveryError, match="ttl"):
        await _claim(service, ticket.id, ttl=ttl)


async def test_disabled_delivery_refuses_new_claims(session_factory) -> None:
    """A disabled observer feature must not silently grant a new external-work lease."""
    ticket, _enabled_service = await _workflow(session_factory)
    service = _service(session_factory, enabled=False)
    with pytest.raises(DeliveryError, match="delivery_disabled"):
        await _claim(service, ticket.id)


async def test_fresh_context_only_workflow_can_claim_implementation(session_factory) -> None:
    """Requiring an artifact for initial implementation would deadlock context-only work."""
    async with session_factory() as session:
        async with session.begin():
            source = (
                await session.execute(
                    decisions.insert()
                    .values(title="claim source", description="current", reasoning="test")
                    .returning(decisions.c.id)
                )
            ).scalar_one()
    ticket, service = await _workflow(session_factory, source=source)
    claim = await _claim(service, ticket.id)
    assert claim.work.kind == "implement"
    assert claim.work.role == "executor"


async def test_stale_workflow_version_is_rejected_with_current_assessment(session_factory) -> None:
    """Ignoring workflow version allows a claim despite a caller's stale generation."""
    ticket, service = await _workflow(session_factory)
    await _bind_open_observation(session_factory, ticket.id, service)
    view = await _view(service, ticket.id)
    assert {item.kind for item in view.assessment.eligible_work} == {"integrate"}
    with pytest.raises(DeliveryError, match="stale"):
        await service.claim(
            ticket.id,
            actor_project="brain-v42",
            owner_key="orchestrator-a",
            work_kind="integrate",
            expected_workflow_version=view.assessment.assessment_version - 1,
            expected_assessment_id=view.assessment.assessment_id,
        )


async def test_stale_assessment_is_rejected_with_current_workflow_version(session_factory) -> None:
    """Ignoring assessment identity allows a claim against facts the caller did not inspect."""
    ticket, service = await _workflow(session_factory)
    await _bind_open_observation(session_factory, ticket.id, service)
    view = await _view(service, ticket.id)
    assert {item.kind for item in view.assessment.eligible_work} == {"integrate"}
    with pytest.raises(DeliveryError, match="stale"):
        await service.claim(
            ticket.id,
            actor_project="brain-v42",
            owner_key="orchestrator-a",
            work_kind="integrate",
            expected_workflow_version=view.assessment.assessment_version,
            expected_assessment_id="0" * 64,
        )


async def test_context_change_refuses_claim(session_factory) -> None:
    """Hydrating context before locking would grant work against changed required context."""
    async with session_factory() as session:
        async with session.begin():
            source = (
                await session.execute(
                    decisions.insert()
                    .values(title="claim context", description="before", reasoning="test")
                    .returning(decisions.c.id)
                )
            ).scalar_one()
    ticket, service = await _workflow(session_factory, source=source)
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                decisions.update().where(decisions.c.id == source).values(description="after")
            )
    with pytest.raises(DeliveryError):
        await _claim(service, ticket.id)


async def test_changed_dependency_invalidates_displayed_assessment_before_claim(
    session_factory,
) -> None:
    """Ignoring a locked upstream generation would claim work based on stale dependency facts."""
    upstream, upstream_service = await _workflow(session_factory)
    await _bind_observation(session_factory, upstream.id, upstream_service, state="merged")
    await _issue_integration_receipt(session_factory, upstream.id)
    upstream_inputs = await PgDeliveryRepo(session_factory).load_inputs(
        upstream.id, feature_enabled=True, freshness_seconds=3600
    )
    assert upstream_inputs is not None
    downstream = await PgTicketRepo(session_factory).create(
        TicketCreate(
            kind=TicketKind.REQUEST,
            title=f"dependent claim {uuid4()}",
            body="dependency generation must fence claims",
            from_project="requester",
            to_project="brain-v42",
        )
    )
    service = _service(session_factory)
    await service.set_contract(
        downstream.id,
        actor_project="requester",
        expected_revision=0,
        idempotency_key=f"dependent-claim-contract-{downstream.id}",
        contract=_contract(
            dependencies=(
                DeliveryDependency(
                    ticket_id=upstream.id,
                    contract_revision=upstream_inputs.contract.contract_revision,
                    attempt=upstream_inputs.attempt,
                    milestone="integrated",
                ),
            )
        ),
    )
    displayed = await _view(service, downstream.id)
    assert {item.kind for item in displayed.assessment.eligible_work} == {"implement"}
    await upstream_service.set_contract(
        upstream.id,
        actor_project="requester",
        expected_revision=1,
        idempotency_key=f"upstream-amend-{upstream.id}",
        contract=_contract(),
    )
    with pytest.raises(DeliveryError, match="stale"):
        await service.claim(
            downstream.id,
            actor_project="brain-v42",
            owner_key="orchestrator-a",
            work_kind="implement",
            expected_workflow_version=displayed.assessment.assessment_version,
            expected_assessment_id=displayed.assessment.assessment_id,
        )


async def test_required_source_wait_crossing_deadline_uses_post_wait_pg_clock(
    session_factory,
) -> None:
    """Using transaction_timestamp before a source wait would accept evidence that expired in the wait."""
    async with session_factory() as session:
        async with session.begin():
            source = (
                await session.execute(
                    decisions.insert()
                    .values(title="claim deadline", description="before", reasoning="test")
                    .returning(decisions.c.id)
                )
            ).scalar_one()
    ticket, service = await _workflow(session_factory, source=source, freshness=1)
    await _bind_open_observation(session_factory, ticket.id, service)
    view = await _view(service, ticket.id)
    assert {item.kind for item in view.assessment.eligible_work} == {"integrate"}
    assert view.assessment.fresh_until is not None
    module = importlib.import_module("brain_v42.repositories.pg_delivery_claims")
    claim_repo = module.PgDeliveryClaimsRepo(session_factory)
    async with (
        session_factory() as blocker,
        session_factory() as waiter,
        session_factory() as observer,
    ):
        blocker_transaction = await blocker.begin()
        waiter_transaction = await waiter.begin()
        task = None
        try:
            await blocker.execute(
                sa.select(decisions.c.id).where(decisions.c.id == source).with_for_update()
            )
            blocker_pid = await blocker.scalar(sa.text("SELECT pg_backend_pid()"))
            waiter_pid = await waiter.scalar(sa.text("SELECT pg_backend_pid()"))
            transaction_time = await waiter.scalar(sa.select(sa.func.transaction_timestamp()))
            assert isinstance(blocker_pid, int)
            assert isinstance(waiter_pid, int)
            assert isinstance(transaction_time, datetime)
            task = asyncio.create_task(
                claim_repo.acquire(
                    waiter,
                    ticket.id,
                    settings=DeliverySettings(enabled=True, freshness_seconds=1),
                    actor_project="brain-v42",
                    owner_key="orchestrator-a",
                    work_kind="integrate",
                    expected_workflow_version=view.assessment.assessment_version,
                    expected_assessment_id=view.assessment.assessment_id,
                    ttl_seconds=900,
                )
            )
            await _wait_for_blocker(
                observer, waiter_pid=waiter_pid, blocker_pid=blocker_pid, task=task
            )
            await asyncio.sleep(1.1)
            released = await blocker.scalar(sa.select(sa.func.clock_timestamp()))
            assert isinstance(released, datetime)
            assert transaction_time < view.assessment.fresh_until <= released
            await blocker_transaction.commit()
            with pytest.raises(DeliveryError):
                await asyncio.wait_for(task, 5)
            await waiter_transaction.commit()
        finally:
            await _cancel(task)
            if blocker.in_transaction():
                await blocker.rollback()
            if waiter.in_transaction():
                await waiter.rollback()


async def test_amendment_fences_old_renew_and_release_but_replay_does_not_increment_twice(
    session_factory,
) -> None:
    """Revoking only on observation or not revoking on a real amendment breaks lease fencing."""
    ticket, service = await _workflow(session_factory)
    claim = await _claim(service, ticket.id)
    before = await _claim_row(session_factory, ticket.id)
    contract = _contract()
    await service.set_contract(
        ticket.id,
        actor_project="requester",
        expected_revision=1,
        idempotency_key=f"claim-amend-{ticket.id}",
        contract=contract,
    )
    amended = await _claim_row(session_factory, ticket.id)
    assert amended["claim_owner"] is None
    assert amended["claim_epoch"] == before["claim_epoch"] + 1
    with pytest.raises(DeliveryError):
        await _renew(service, ticket.id, claim)
    with pytest.raises(DeliveryError):
        await _release(service, ticket.id, claim)
    await service.set_contract(
        ticket.id,
        actor_project="requester",
        expected_revision=1,
        idempotency_key=f"claim-amend-{ticket.id}",
        contract=contract,
    )
    assert (await _claim_row(session_factory, ticket.id))["claim_epoch"] == amended["claim_epoch"]


async def test_first_pr_binding_preserves_a_live_implementation_lease(session_factory) -> None:
    """Opening the first draft PR records work without changing its leased generation."""
    ticket, service = await _workflow(session_factory)
    claim = await _claim(service, ticket.id, work="implement")
    before = await _claim_row(session_factory, ticket.id)
    binding = await service.bind_pr(
        ticket.id,
        actor_project="brain-v42",
        deliverable_key="implementation",
        repository_id=RID,
        pr_number=42,
        expected_revision=1,
        expected_workflow_version=1,
        idempotency_key=f"claim-first-bind-preserves-{ticket.id}",
    )
    assert binding.pr_number == 42
    after = await _claim_row(session_factory, ticket.id)
    for field in ("claim_owner", "claim_kind", "claim_digest", "claim_expires_at", "claim_epoch"):
        assert after[field] == before[field]
    renewed = await _renew(service, ticket.id, claim, ttl=1200)
    assert renewed.epoch == claim.epoch


async def test_binding_replacement_fences_lease_but_identical_reconfirmation_preserves_it(
    session_factory,
) -> None:
    """A replacement changes the work generation; equal provider confirmation does not."""
    ticket, service = await _workflow(session_factory)
    first = await service.bind_pr(
        ticket.id,
        actor_project="brain-v42",
        deliverable_key="implementation",
        repository_id=RID,
        pr_number=42,
        expected_revision=1,
        expected_workflow_version=1,
        idempotency_key=f"claim-first-bind-{ticket.id}",
    )
    assert first.pr_number == 42
    await _publish_observation(session_factory, first, draft=True)
    before_replacement = await _view(service, ticket.id)
    assert {item.kind for item in before_replacement.assessment.eligible_work} == {"implement"}
    claim = await _claim(service, ticket.id, work="implement")
    before = await _claim_row(session_factory, ticket.id)
    view = await _view(service, ticket.id)
    replacement = await service.bind_pr(
        ticket.id,
        actor_project="brain-v42",
        deliverable_key="implementation",
        repository_id=RID,
        pr_number=43,
        expected_revision=1,
        expected_workflow_version=view.assessment.assessment_version,
        idempotency_key=f"claim-rebind-{ticket.id}",
    )
    assert replacement.pr_number == 43
    replaced = await _claim_row(session_factory, ticket.id)
    assert replaced["claim_owner"] is None
    assert replaced["claim_epoch"] == before["claim_epoch"] + 1
    replay = await service.bind_pr(
        ticket.id,
        actor_project="brain-v42",
        deliverable_key="implementation",
        repository_id=RID,
        pr_number=43,
        expected_revision=1,
        expected_workflow_version=view.assessment.assessment_version,
        idempotency_key=f"claim-rebind-{ticket.id}",
    )
    assert replay.id == replacement.id
    assert (await _claim_row(session_factory, ticket.id))["claim_epoch"] == replaced["claim_epoch"]
    with pytest.raises(DeliveryError):
        await _renew(service, ticket.id, claim)
    with pytest.raises(DeliveryError):
        await _release(service, ticket.id, claim)
    await _publish_observation(session_factory, replacement, draft=True)
    second_view = await _view(service, ticket.id)
    assert {item.kind for item in second_view.assessment.eligible_work} == {"implement"}
    second = await _claim(service, ticket.id, owner="orchestrator-b", work="implement")
    held = await _claim_row(session_factory, ticket.id)
    async with session_factory() as session:
        async with session.begin():
            now = await session.scalar(sa.select(sa.func.clock_timestamp()))
            assert isinstance(now, datetime)
            await PgDeliveryEvidenceRepo(session_factory).publish_observation(
                session,
                replacement.id,
                2,
                PullRequestEvidence(
                    provider_id=7,
                    repository_id=RID,
                    pr_number=43,
                    author_id="executor",
                    head_repository_id=8,
                    head_sha=A,
                    base_sha=B,
                    base_ref="main",
                    state="open",
                    draft=True,
                    mergeable=True,
                    complete=True,
                    integration_sha=C,
                    collected_at=now,
                ),
                now,
                now,
            )
            await PgDeliveryEvidenceRepo(session_factory).publish_observation(
                session,
                replacement.id,
                3,
                PullRequestEvidence(
                    provider_id=7,
                    repository_id=RID,
                    pr_number=43,
                    author_id="executor",
                    head_repository_id=8,
                    head_sha=A,
                    base_sha=B,
                    base_ref="main",
                    state="open",
                    draft=True,
                    mergeable=True,
                    complete=True,
                    integration_sha=C,
                    collected_at=now,
                ),
                now,
                now,
            )
    after = await _claim_row(session_factory, ticket.id)
    assert after["claim_owner"] == held["claim_owner"] == "orchestrator-b"
    assert after["claim_digest"] == held["claim_digest"]
    assert after["claim_expires_at"] == held["claim_expires_at"]
    assert after["claim_epoch"] == held["claim_epoch"] == second.epoch


async def test_token_and_digest_are_redacted_from_views_errors_and_repr(session_factory) -> None:
    """Returning a bearer token from a read model or error would defeat hashing at rest."""
    ticket, service = await _workflow(session_factory)
    claim = await _claim(service, ticket.id)
    row = await _claim_row(session_factory, ticket.id)
    digest = row["claim_digest"]
    assert isinstance(digest, str) and len(digest) == 64
    assert digest != claim.claim_token
    view = await _view(service, ticket.id)
    page = await service.list(actor_project="brain-v42")
    for rendered in (repr(claim), repr(view), view.model_dump_json(), page.model_dump_json()):
        assert claim.claim_token not in rendered
        assert digest not in rendered
    with pytest.raises(DeliveryError) as error:
        await service.release_claim(
            ticket.id,
            actor_project="brain-v42",
            owner_key="orchestrator-a",
            claim_token="not-the-live-token",
            epoch=claim.epoch,
        )
    assert claim.claim_token not in str(error.value)
    assert digest not in str(error.value)


async def test_wrong_actor_with_correct_owner_token_and_epoch_is_refused(session_factory) -> None:
    """Binding a lease only to owner text lets the other ticket participant steal it."""
    ticket, service = await _workflow(session_factory)
    claim = await _claim(service, ticket.id)
    with pytest.raises(DeliveryError):
        await _release(service, ticket.id, claim, actor="requester")


async def test_caller_rollback_removes_new_claim_row_after_it_was_visible_in_transaction(
    session_factory,
) -> None:
    """Opening an independent transaction inside acquisition would leak a rolled-back lease."""
    ticket, service = await _workflow(session_factory)
    module = importlib.import_module("brain_v42.repositories.pg_delivery_claims")
    claim_repo = module.PgDeliveryClaimsRepo(session_factory)
    view = await _view(service, ticket.id)
    async with session_factory() as session:
        transaction = await session.begin()
        try:
            claim = await claim_repo.acquire(
                session,
                ticket.id,
                settings=DeliverySettings(enabled=True, freshness_seconds=3600),
                actor_project="brain-v42",
                owner_key="orchestrator-a",
                work_kind="implement",
                expected_workflow_version=view.assessment.assessment_version,
                expected_assessment_id=view.assessment.assessment_id,
                ttl_seconds=900,
            )
            row = (
                await session.execute(
                    sa.select(delivery_workflows.c.claim_owner).where(
                        delivery_workflows.c.ticket_id == ticket.id
                    )
                )
            ).scalar_one()
            assert row == "orchestrator-a"
            assert claim.claim_token
        finally:
            await transaction.rollback()
    assert (await _claim_row(session_factory, ticket.id))["claim_owner"] is None
