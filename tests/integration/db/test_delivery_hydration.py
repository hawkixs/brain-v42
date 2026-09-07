"""Real PostgreSQL contracts for active delivery evidence hydration."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa

from brain_v42.db.tables import decisions, delivery_workflows
from brain_v42.delivery_config import DeliverySettings
from brain_v42.models.delivery import (
    BrainEntityReference,
    ContractInput,
    Deliverable,
    PullRequestEvidence,
    RepositoryContextEvidence,
    RepositoryDocumentFact,
    RepositoryDocumentReference,
    ReviewPolicy,
)
from brain_v42.models.ticket import TicketCreate, TicketKind
from brain_v42.repositories.pg_delivery import PgDeliveryRepo
from brain_v42.repositories.pg_delivery_evidence import PgDeliveryEvidenceRepo
from brain_v42.repositories.pg_ticket import PgTicketRepo
from brain_v42.services.delivery_service import DeliveryService

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

_REPOSITORY_ID = 1_337_360_966
_SHA_A = "a" * 40
_SHA_B = "b" * 40
_SHA_C = "c" * 40
_SHA_D = "d" * 40


def _service(session_factory) -> DeliveryService:
    return DeliveryService(
        PgDeliveryRepo(session_factory),
        settings=DeliverySettings(enabled=True, freshness_seconds=3_600),
    )


def _contract(
    *, refs: tuple[RepositoryDocumentReference | BrainEntityReference, ...] = ()
) -> ContractInput:
    return ContractInput(
        objective="hydrate active immutable delivery evidence",
        priority=1,
        acceptance_mode="automatic",
        context_refs=refs,
        deliverables=(
            Deliverable(
                key="implementation",
                repository="hawkixs/brain-v42",
                target_branch="main",
                required_checks=(),
                no_checks_reason="hydration integration contract",
                review=ReviewPolicy(required_approvals=0, allowed_reviewers=()),
            ),
        ),
    )


async def _workflow(
    session_factory, *, refs: tuple[RepositoryDocumentReference | BrainEntityReference, ...] = ()
):
    ticket = await PgTicketRepo(session_factory).create(
        TicketCreate(
            kind=TicketKind.REQUEST,
            title="delivery hydration",
            body="active observations must retain immutable success proof",
            from_project="brain-v42",
            to_project="brain-v42",
        )
    )
    service = _service(session_factory)
    await service.set_contract(
        ticket.id,
        actor_project="brain-v42",
        expected_revision=0,
        idempotency_key=f"hydration-contract-{ticket.id}",
        contract=_contract(refs=refs),
    )
    return ticket, service


async def _binding(session_factory):
    ticket, service = await _workflow(session_factory)
    binding = await service.bind_pr(
        ticket.id,
        actor_project="brain-v42",
        deliverable_key="implementation",
        repository_id=_REPOSITORY_ID,
        pr_number=42,
        expected_revision=1,
        expected_workflow_version=1,
        idempotency_key=f"hydration-binding-{ticket.id}",
    )
    return ticket, service, binding


async def _optional_brain_reference(session_factory) -> BrainEntityReference:
    async with session_factory() as session:
        async with session.begin():
            identifier = await session.scalar(
                decisions.insert()
                .values(title="optional hydration", description="available", reasoning="test")
                .returning(decisions.c.id)
            )
    assert identifier is not None
    return BrainEntityReference(
        kind="brain_entity", entity_type="decision", entity_id=identifier, required=False
    )


def _pr_evidence(*, collected_at: datetime) -> PullRequestEvidence:
    return PullRequestEvidence(
        provider_id=7001,
        repository_id=_REPOSITORY_ID,
        pr_number=42,
        author_id="executor",
        head_repository_id=9988,
        head_sha=_SHA_A,
        base_sha=_SHA_B,
        base_ref="main",
        state="merged",
        draft=False,
        complete=True,
        integration_sha=_SHA_C,
        collected_at=collected_at,
    )


def _context_evidence(*refs: RepositoryDocumentReference) -> RepositoryContextEvidence:
    return RepositoryContextEvidence(
        complete=True,
        facts=tuple(
            RepositoryDocumentFact(
                repository_id=reference.repository_id,
                commit_sha=reference.sha,
                path=reference.path,
                tree_sha=_SHA_C,
                blob_sha=_SHA_D,
                mode="100644",
                status="available",
            )
            for reference in refs
        ),
    )


async def _publish_context(
    session_factory,
    ticket_id,
    evidence: RepositoryContextEvidence,
    started: datetime,
    finished: datetime,
):
    inputs = await PgDeliveryRepo(session_factory).load_inputs(
        ticket_id, feature_enabled=True, freshness_seconds=3_600
    )
    assert inputs is not None
    async with session_factory() as session:
        row = (
            (
                await session.execute(
                    sa.select(
                        delivery_workflows.c.context_row_version,
                        delivery_workflows.c.context_set_digest,
                    ).where(delivery_workflows.c.ticket_id == ticket_id)
                )
            )
            .mappings()
            .one()
        )
        context_version = row["context_row_version"]
    assert isinstance(context_version, int)
    async with session_factory() as session:
        async with session.begin():
            return await PgDeliveryEvidenceRepo(session_factory).publish_repository_context(
                session,
                ticket_id=ticket_id,
                expected_revision=inputs.contract.contract_revision,
                expected_attempt=inputs.attempt,
                expected_context_set_digest=row["context_set_digest"],
                expected_context_row_version=context_version,
                evidence=evidence,
                collection_started_at=started,
                collection_finished_at=finished,
            )


async def test_get_hydrates_active_success_snapshot_with_strict_identity_and_original_collection_time(
    session_factory,
) -> None:
    """Dropping persisted confirmation joins would erase an active merged proof."""
    ticket, service, binding = await _binding(session_factory)
    collected = datetime(2026, 9, 7, 12, tzinfo=UTC)
    async with session_factory() as session:
        async with session.begin():
            confirmation = await PgDeliveryEvidenceRepo(session_factory).publish_observation(
                session,
                binding_id=binding.id,
                expected_binding_version=1,
                evidence=_pr_evidence(collected_at=collected),
                collection_started_at=collected,
                collection_finished_at=collected,
            )

    view = await service.get(ticket.id, actor_project="brain-v42")

    hydrated = view.bindings[0]
    assert hydrated.snapshot_id is not None
    assert hydrated.success_confirmation_id == confirmation.id
    assert hydrated.latest_attempt_confirmation_id == confirmation.id
    assert hydrated.confirmation is not None
    assert hydrated.confirmation.id == confirmation.id
    assert hydrated.confirmation.evidence is not None
    assert hydrated.confirmation.evidence.collected_at == collected
    assert isinstance(hydrated.binding.id, type(binding.id))
    assert isinstance(hydrated.confirmation.collection_finished_at, datetime)


async def test_reconfirmation_keeps_snapshot_evidence_but_exposes_its_new_confirmation_interval(
    session_factory,
) -> None:
    """Hydrating the second confirmation as a new snapshot would rewrite historical proof."""
    ticket, service, binding = await _binding(session_factory)
    first = datetime(2026, 9, 7, 12, tzinfo=UTC)
    second = first + timedelta(minutes=5)
    repo = PgDeliveryEvidenceRepo(session_factory)
    async with session_factory() as session:
        async with session.begin():
            initial = await repo.publish_observation(
                session,
                binding_id=binding.id,
                expected_binding_version=1,
                evidence=_pr_evidence(collected_at=first),
                collection_started_at=first,
                collection_finished_at=first,
            )
    async with session_factory() as session:
        async with session.begin():
            renewed = await repo.publish_observation(
                session,
                binding_id=binding.id,
                expected_binding_version=2,
                evidence=_pr_evidence(collected_at=second),
                collection_started_at=second,
                collection_finished_at=second,
            )

    hydrated = (await service.get(ticket.id, actor_project="brain-v42")).bindings[0]

    assert hydrated.snapshot_id is not None
    assert hydrated.success_confirmation_id == renewed.id
    assert hydrated.latest_attempt_confirmation_id == renewed.id
    assert hydrated.confirmation is not None
    assert hydrated.confirmation.id == renewed.id
    assert hydrated.confirmation.collection_finished_at == second
    assert hydrated.confirmation.evidence is not None
    assert hydrated.confirmation.evidence.collected_at == first
    assert initial.id != renewed.id


async def test_current_error_keeps_success_digest_but_moves_latest_attempt_and_health(
    session_factory,
) -> None:
    """Replacing success evidence with an error would lose receipt identity during an outage."""
    ticket, service, binding = await _binding(session_factory)
    instant = datetime.now(UTC)
    repo = PgDeliveryEvidenceRepo(session_factory)
    async with session_factory() as session:
        async with session.begin():
            success = await repo.publish_observation(
                session,
                binding_id=binding.id,
                expected_binding_version=1,
                evidence=_pr_evidence(collected_at=instant).model_copy(
                    update={"state": "open", "mergeable": True}
                ),
                collection_started_at=instant,
                collection_finished_at=instant,
            )
    before = await service.get(ticket.id, actor_project="brain-v42")
    assert before.assessment.observation_health == "fresh"
    assert {item.kind for item in before.assessment.eligible_work} == {"integrate"}
    async with session_factory() as session:
        async with session.begin():
            failed = await repo.record_observation_error(
                session,
                binding_id=binding.id,
                expected_binding_version=2,
                code="provider_timeout",
                collection_started_at=instant,
                collection_finished_at=instant + timedelta(seconds=1),
            )

    after = await service.get(ticket.id, actor_project="brain-v42")
    hydrated = after.bindings[0]
    assert before.assessment.delivery_digest == after.assessment.delivery_digest
    assert before.assessment.assessment_id != after.assessment.assessment_id
    assert after.assessment.observation_health == "error"
    assert {item.code for item in after.assessment.blockers} >= {"observation_error"}
    assert after.assessment.eligible_work == ()
    assert hydrated.confirmation is not None
    assert hydrated.confirmation.id == success.id
    assert hydrated.confirmation.evidence is not None
    assert hydrated.last_attempt_outcome == "error"
    assert hydrated.latest_attempt_confirmation_id == failed.id


async def test_repeated_equal_errors_create_new_assessments_without_erasing_success_proof(
    session_factory,
) -> None:
    """Collapsing equal provider errors hides a real collection attempt from the assessment."""
    ticket, service, binding = await _binding(session_factory)
    instant = datetime(2026, 9, 7, 12, tzinfo=UTC)
    repo = PgDeliveryEvidenceRepo(session_factory)
    async with session_factory() as session:
        async with session.begin():
            await repo.publish_observation(
                session,
                binding_id=binding.id,
                expected_binding_version=1,
                evidence=_pr_evidence(collected_at=instant),
                collection_started_at=instant,
                collection_finished_at=instant,
            )
    async with session_factory() as session:
        async with session.begin():
            first = await repo.record_observation_error(
                session,
                binding_id=binding.id,
                expected_binding_version=2,
                code="provider_timeout",
                collection_started_at=instant,
                collection_finished_at=instant,
            )
    first_view = await service.get(ticket.id, actor_project="brain-v42")
    async with session_factory() as session:
        async with session.begin():
            second = await repo.record_observation_error(
                session,
                binding_id=binding.id,
                expected_binding_version=3,
                code="provider_timeout",
                collection_started_at=instant + timedelta(seconds=1),
                collection_finished_at=instant + timedelta(seconds=1),
            )
    second_view = await service.get(ticket.id, actor_project="brain-v42")

    assert first.id != second.id
    assert first_view.assessment.delivery_digest == second_view.assessment.delivery_digest
    assert first_view.assessment.assessment_id != second_view.assessment.assessment_id
    assert second_view.bindings[0].latest_attempt_confirmation_id == second.id
    assert second_view.bindings[0].confirmation is not None


async def test_context_success_before_pr_hydrates_exact_pin_proofs_once_per_reference(
    session_factory,
) -> None:
    """Returning one full fact bundle per pin makes 32-pin proof output quadratic."""
    paths = (
        "p" * 608,
        "a" * 4_096,
        *(f"docs/pin-{index}.md" for index in range(30)),
    )
    refs = tuple(
        RepositoryDocumentReference(
            kind="repository_document",
            repository_id=_REPOSITORY_ID,
            sha=f"{index + 1:040x}",
            path=path,
        )
        for index, path in enumerate(paths)
    )
    ticket, service = await _workflow(session_factory, refs=refs)
    instant = datetime.now(UTC)
    confirmation = await _publish_context(
        session_factory, ticket.id, _context_evidence(*refs), instant, instant
    )

    view = await service.get(ticket.id, actor_project="brain-v42")

    predicates = tuple(
        item for item in view.contexts if item.reference_identity.startswith("repository_document:")
    )
    assert len(predicates) == 32
    assert len(paths[0]) == 608
    assert len(paths[1]) == 4_096
    assert all(item.status == "available" for item in predicates)
    assert all(item.snapshot_id == confirmation.snapshot_id for item in predicates)
    assert all(item.success_confirmation_id == confirmation.id for item in predicates)
    assert all(item.latest_attempt_confirmation_id == confirmation.id for item in predicates)
    assert {item.evidence.facts[0].path for item in predicates if item.evidence is not None} == set(
        paths
    )
    assert all(len(item.evidence.facts) == 1 for item in predicates if item.evidence is not None)
    assert view.assessment.observation_health == "fresh"
    assert {item.kind for item in view.assessment.eligible_work} == {"implement"}


async def test_context_latest_error_retains_success_proof_and_blocks_new_eligibility(
    session_factory,
) -> None:
    """Reporting context success after its latest error would authorize work on stale proof."""
    reference = RepositoryDocumentReference(
        kind="repository_document", repository_id=_REPOSITORY_ID, sha=_SHA_A, path="docs/context.md"
    )
    ticket, service = await _workflow(session_factory, refs=(reference,))
    instant = datetime.now(UTC)
    success = await _publish_context(
        session_factory, ticket.id, _context_evidence(reference), instant, instant
    )
    before = await service.get(ticket.id, actor_project="brain-v42")
    assert before.assessment.observation_health == "fresh"
    assert {item.kind for item in before.assessment.eligible_work} == {"implement"}
    inputs = await PgDeliveryRepo(session_factory).load_inputs(
        ticket.id, feature_enabled=True, freshness_seconds=3_600
    )
    assert inputs is not None
    async with session_factory() as session:
        row = (
            (
                await session.execute(
                    sa.select(
                        delivery_workflows.c.context_row_version,
                        delivery_workflows.c.context_set_digest,
                    ).where(delivery_workflows.c.ticket_id == ticket.id)
                )
            )
            .mappings()
            .one()
        )
        context_version = row["context_row_version"]
    assert isinstance(context_version, int)
    async with session_factory() as session:
        async with session.begin():
            failed = await PgDeliveryEvidenceRepo(session_factory).record_repository_context_error(
                session,
                ticket_id=ticket.id,
                expected_revision=inputs.contract.contract_revision,
                expected_attempt=inputs.attempt,
                expected_context_set_digest=row["context_set_digest"],
                expected_context_row_version=context_version,
                code="provider_timeout",
                collection_started_at=instant,
                collection_finished_at=instant + timedelta(seconds=1),
            )

    after = await service.get(ticket.id, actor_project="brain-v42")
    predicate = after.contexts[0]
    assert after.assessment.observation_health == "error"
    assert {item.code for item in after.assessment.blockers} >= {
        "context_error",
        "observation_error",
    }
    assert after.assessment.eligible_work == ()
    assert predicate.status == "error"
    assert predicate.snapshot_id == success.snapshot_id
    assert predicate.success_confirmation_id == success.id
    assert predicate.latest_attempt_confirmation_id == failed.id
    assert predicate.collection_started_at == instant
    assert predicate.collection_finished_at == instant
    assert predicate.evidence is not None
    assert tuple(fact.identity() for fact in predicate.evidence.facts) == (
        (reference.repository_id, reference.sha, reference.path),
    )


async def test_context_expiring_before_pr_limits_view_freshness_to_context_interval(
    session_factory,
) -> None:
    """Using the later PR timestamp as the view bound would overstate safe context freshness."""
    reference = RepositoryDocumentReference(
        kind="repository_document", repository_id=_REPOSITORY_ID, sha=_SHA_A, path="docs/context.md"
    )
    ticket, service = await _workflow(session_factory, refs=(reference,))
    context_time = datetime.now(UTC) - timedelta(seconds=3_590)
    await _publish_context(
        session_factory, ticket.id, _context_evidence(reference), context_time, context_time
    )
    binding = await service.bind_pr(
        ticket.id,
        actor_project="brain-v42",
        deliverable_key="implementation",
        repository_id=_REPOSITORY_ID,
        pr_number=42,
        expected_revision=1,
        expected_workflow_version=2,
        idempotency_key=f"hydration-expiring-binding-{ticket.id}",
    )
    later = datetime.now(UTC)
    async with session_factory() as session:
        async with session.begin():
            await PgDeliveryEvidenceRepo(session_factory).publish_observation(
                session,
                binding_id=binding.id,
                expected_binding_version=1,
                evidence=_pr_evidence(collected_at=later),
                collection_started_at=later,
                collection_finished_at=later,
            )

    view = await service.get(ticket.id, actor_project="brain-v42")
    assert view.assessment.fresh_until is not None
    assert view.assessment.fresh_until == context_time + timedelta(seconds=3_600)


async def test_context_missing_and_stale_health_are_distinct_real_view_outcomes(
    session_factory,
) -> None:
    """Collapsing missing and stale pins would hide whether collection ever completed."""
    reference = RepositoryDocumentReference(
        kind="repository_document", repository_id=_REPOSITORY_ID, sha=_SHA_A, path="docs/context.md"
    )
    missing_ticket, missing_service = await _workflow(session_factory, refs=(reference,))
    missing = await missing_service.get(missing_ticket.id, actor_project="brain-v42")
    assert missing.assessment.observation_health == "never_observed"
    assert any(item.code == "context_missing" for item in missing.assessment.blockers)

    stale_ticket, stale_service = await _workflow(session_factory, refs=(reference,))
    stale_time = datetime.now(UTC) - timedelta(seconds=3_601)
    await _publish_context(
        session_factory, stale_ticket.id, _context_evidence(reference), stale_time, stale_time
    )
    stale = await stale_service.get(stale_ticket.id, actor_project="brain-v42")
    assert stale.assessment.observation_health == "stale"
    assert any(item.code == "observation_stale" for item in stale.assessment.blockers)


async def test_optional_long_repository_reference_and_amendment_do_not_reuse_old_proof(
    session_factory,
) -> None:
    """Optional pins must not block evaluation, and amendment must not hydrate prior evidence."""
    required = RepositoryDocumentReference(
        kind="repository_document",
        repository_id=_REPOSITORY_ID,
        sha=_SHA_A,
        path="docs/required.md",
    )
    optional = RepositoryDocumentReference(
        kind="repository_document",
        repository_id=_REPOSITORY_ID,
        sha=_SHA_B,
        path=("docs/" * 120) + "optional.md",
        required=False,
    )
    optional_brain = await _optional_brain_reference(session_factory)
    ticket, service = await _workflow(session_factory, refs=(required, optional, optional_brain))
    instant = datetime.now(UTC)
    await _publish_context(
        session_factory, ticket.id, _context_evidence(required), instant, instant
    )
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                sa.update(decisions)
                .where(decisions.c.id == optional_brain.entity_id)
                .values(description="changed after optional pinning")
            )
    before = await service.get(ticket.id, actor_project="brain-v42")
    assert all(item.status != "error" for item in before.contexts)
    assert any(item.reference_identity.endswith(optional.path) for item in before.contexts)
    assert any(item.reference_identity.startswith("brain_entity:") for item in before.contexts)
    assert not any(item.code.startswith("context_") for item in before.assessment.blockers)
    assert {item.kind for item in before.assessment.eligible_work} == {"implement"}

    await service.set_contract(
        ticket.id,
        actor_project="brain-v42",
        expected_revision=1,
        idempotency_key=f"hydration-amendment-{ticket.id}",
        contract=_contract(refs=(required,)),
    )
    after = await service.get(ticket.id, actor_project="brain-v42")
    assert after.contract.contract_revision == 2
    assert after.contexts[0].snapshot_id is None
    assert after.contexts[0].success_confirmation_id is None
