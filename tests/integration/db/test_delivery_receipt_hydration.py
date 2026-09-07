"""Real PostgreSQL contracts for current delivery receipt hydration."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa

from brain_v42.db.tables import delivery_contract_revisions, delivery_receipts, delivery_workflows
from brain_v42.delivery_config import DeliverySettings
from brain_v42.models.delivery import (
    ContractInput,
    ContractRevision,
    Deliverable,
    DeliveryDependency,
    MilestoneReceipt,
    PullRequestEvidence,
    ReviewPolicy,
)
from brain_v42.models.delivery_evaluator import evaluate_delivery
from brain_v42.models.delivery_hashes import canonical_digest
from brain_v42.models.ticket import TicketCreate, TicketKind
from brain_v42.repositories.pg_delivery import PgDeliveryRepo, _context_set_digest
from brain_v42.repositories.pg_delivery_evidence import PgDeliveryEvidenceRepo
from brain_v42.repositories.pg_ticket import PgTicketRepo
from brain_v42.services.delivery_service import DeliveryService

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

_REPOSITORY_ID = 1_337_360_966
_SHA_A = "a" * 40
_SHA_B = "b" * 40
_SHA_C = "c" * 40


def _service(session_factory) -> DeliveryService:
    return DeliveryService(
        PgDeliveryRepo(session_factory),
        settings=DeliverySettings(enabled=True, freshness_seconds=3_600),
    )


def _contract(
    *,
    acceptance_mode: str = "automatic",
    dependencies: tuple[DeliveryDependency, ...] = (),
    objective: str = "hydrate only receipts that match current delivery identity",
) -> ContractInput:
    return ContractInput(
        objective=objective,
        priority=1,
        acceptance_mode=acceptance_mode,
        dependencies=dependencies,
        deliverables=(
            Deliverable(
                key="implementation",
                repository="hawkixs/brain-v42",
                target_branch="main",
                required_checks=(),
                no_checks_reason="receipt hydration uses retained observations",
                review=ReviewPolicy(required_approvals=0, allowed_reviewers=()),
            ),
        ),
    )


async def _ticket_with_contract(
    session_factory,
    *,
    acceptance_mode: str = "automatic",
    dependencies: tuple[DeliveryDependency, ...] = (),
    objective: str = "hydrate only receipts that match current delivery identity",
):
    ticket = await PgTicketRepo(session_factory).create(
        TicketCreate(
            kind=TicketKind.REQUEST,
            title=f"receipt hydration {uuid4()}",
            body="immutable receipts are read from the current workflow identity",
            from_project="brain-v42",
            to_project="brain-v42",
        )
    )
    service = _service(session_factory)
    await service.set_contract(
        ticket.id,
        actor_project="brain-v42",
        expected_revision=0,
        idempotency_key=f"receipt-contract-{ticket.id}",
        contract=_contract(
            acceptance_mode=acceptance_mode, dependencies=dependencies, objective=objective
        ),
    )
    return ticket, service


def _evidence(*, collected_at: datetime) -> PullRequestEvidence:
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


async def _observed_workflow(session_factory, *, acceptance_mode: str = "automatic"):
    ticket, service = await _ticket_with_contract(session_factory, acceptance_mode=acceptance_mode)
    binding = await service.bind_pr(
        ticket.id,
        actor_project="brain-v42",
        deliverable_key="implementation",
        repository_id=_REPOSITORY_ID,
        pr_number=42,
        expected_revision=1,
        expected_workflow_version=1,
        idempotency_key=f"receipt-binding-{ticket.id}",
    )
    instant = datetime.now(UTC)
    async with session_factory() as session:
        async with session.begin():
            await PgDeliveryEvidenceRepo(session_factory).publish_observation(
                session,
                binding_id=binding.id,
                expected_binding_version=1,
                evidence=_evidence(collected_at=instant),
                collection_started_at=instant,
                collection_finished_at=instant,
            )
    inputs = await PgDeliveryRepo(session_factory).load_inputs(
        ticket.id, feature_enabled=True, freshness_seconds=3_600
    )
    assert inputs is not None
    return ticket, service, binding, inputs


def _receipt(inputs, *, milestone: str, **changes: object) -> MilestoneReceipt:
    issued_at = datetime.now(UTC)
    assessment = evaluate_delivery(inputs, now=issued_at)
    payload: dict[str, object] = {
        "id": uuid4(),
        "ticket_id": inputs.contract.ticket_id,
        "milestone": milestone,
        "contract_revision": inputs.contract.contract_revision,
        "attempt": inputs.attempt,
        "contract_digest": inputs.contract.content_digest,
        "delivery_digest": assessment.delivery_digest,
        "issued_at": issued_at,
        "acceptance_basis": "automatic" if milestone == "fulfilled" else None,
    }
    payload.update(changes)
    binding_evidence = inputs.active_bindings[0]
    confirmation = binding_evidence.confirmation
    evidence = confirmation.evidence if confirmation is not None else None
    assert confirmation is not None
    assert evidence is not None
    assert binding_evidence.snapshot_id is not None
    assert binding_evidence.success_confirmation_id is not None
    assert binding_evidence.latest_attempt_confirmation_id is not None
    snapshot_payload = evidence.model_dump(mode="json")
    semantic_payload = dict(snapshot_payload)
    semantic_payload.pop("collected_at")
    basis = payload["acceptance_basis"]
    explicit_acceptance = (
        {"requester_project": "brain-v42", "rationale": "hydration fixture acceptance"}
        if basis == "explicit"
        else None
    )
    issuer = (
        {
            "issuer_project": "brain-v42",
            "issuer_identity": "brain-v42",
            "issuer_kind": "requester",
        }
        if basis == "explicit"
        else {
            "issuer_project": "brain-v42",
            "issuer_identity": "receipt-hydration-test",
            "issuer_kind": "observer",
        }
    )
    payload["explicit_acceptance"] = explicit_acceptance
    payload["proof"] = {
        "ticket_id": payload["ticket_id"],
        "contract_revision": payload["contract_revision"],
        "attempt": payload["attempt"],
        "workflow_version": inputs.workflow_version,
        "contract_digest": payload["contract_digest"],
        "delivery_digest": payload["delivery_digest"],
        "assessment_id": assessment.assessment_id,
        "decision_time": payload["issued_at"],
        "artifact_proofs": (
            {
                "binding_id": binding_evidence.binding.id,
                "binding_version": binding_evidence.binding.binding_version,
                "deliverable_key": binding_evidence.binding.deliverable_key,
                "repository_id": binding_evidence.binding.repository_id,
                "pr_number": binding_evidence.binding.pr_number,
                "head_sha": evidence.head_sha,
                "base_sha": evidence.base_sha,
                "integration_sha": evidence.integration_sha,
                "integration_revision": evidence.integration_revision,
                "snapshot_id": binding_evidence.snapshot_id,
                "snapshot_digest": canonical_digest(semantic_payload, domain="result"),
                "success_confirmation_id": binding_evidence.success_confirmation_id,
                "latest_attempt_confirmation_id": binding_evidence.latest_attempt_confirmation_id,
                "collection_started_at": confirmation.collection_started_at,
                "collection_finished_at": confirmation.collection_finished_at,
            },
        ),
        "brain_context_proofs": (),
        "repository_context_proofs": (),
        "upstream_receipt_ids": (),
        "issuer": issuer,
        "acceptance_basis": basis,
        "explicit_acceptance": explicit_acceptance,
    }
    return MilestoneReceipt.model_validate(payload)


async def _insert_receipt(session_factory, receipt: MilestoneReceipt) -> None:
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                delivery_receipts.insert().values(
                    id=receipt.id,
                    ticket_id=receipt.ticket_id,
                    contract_revision=receipt.contract_revision,
                    attempt=receipt.attempt,
                    milestone=receipt.milestone,
                    delivery_digest=receipt.delivery_digest,
                    payload=receipt.model_dump(mode="json"),
                    issuer=receipt.proof.issuer.issuer_identity,
                    basis=receipt.acceptance_basis,
                    issued_at=receipt.issued_at,
                )
            )


async def _insert_historical_contract_revision(session_factory, current: ContractRevision) -> None:
    """Add the valid, non-current revision required by the receipt foreign key."""
    historical = ContractRevision.model_validate(
        {
            **current.model_dump(mode="python"),
            "contract_revision": 2,
            "amendment_reason": "historical receipt fixture",
            "content_digest": None,
        }
    )
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                delivery_contract_revisions.insert().values(
                    ticket_id=historical.ticket_id,
                    contract_revision=historical.contract_revision,
                    normalized_contract=historical.model_dump(mode="json"),
                    content_digest=historical.content_digest,
                    context_set_digest=_context_set_digest(historical),
                    author_project=historical.author_project,
                    amendment_reason=historical.amendment_reason,
                    created_at=historical.created_at,
                )
            )


async def _dependent_inputs(session_factory, *, milestone: str = "integrated"):
    upstream, _service, _binding, upstream_inputs = await _observed_workflow(session_factory)
    downstream, _ = await _ticket_with_contract(
        session_factory,
        dependencies=(
            DeliveryDependency(
                ticket_id=upstream.id,
                contract_revision=1,
                attempt=1,
                milestone=milestone,
            ),
        ),
    )
    inputs = await PgDeliveryRepo(session_factory).load_inputs(
        downstream.id, feature_enabled=True, freshness_seconds=3_600
    )
    assert inputs is not None
    return upstream, downstream, upstream_inputs, inputs


async def test_get_view_hydrates_matching_receipts_with_strict_stored_types(
    session_factory,
) -> None:
    """Omitting receipt hydration makes a fulfilled explicit workflow appear merely pending."""
    ticket, service, _binding, inputs = await _observed_workflow(
        session_factory, acceptance_mode="explicit"
    )
    integration = _receipt(inputs, milestone="integration")
    fulfillment = _receipt(inputs, milestone="fulfilled", acceptance_basis="explicit")
    await _insert_receipt(session_factory, integration)

    pending = await service.get(ticket.id, actor_project="brain-v42")

    assert pending.integration_receipt is not None
    assert pending.integration_receipt.id == integration.id
    assert isinstance(pending.integration_receipt.id, UUID)
    assert isinstance(pending.integration_receipt.issued_at, datetime)
    assert pending.fulfillment_receipt is None
    assert pending.assessment.acceptance_state == "pending"
    assert {item.kind for item in pending.assessment.eligible_work} == {"accept"}
    await _insert_receipt(session_factory, fulfillment)

    accepted = await service.get(ticket.id, actor_project="brain-v42")

    assert accepted.fulfillment_receipt is not None
    assert accepted.fulfillment_receipt.id == fulfillment.id
    assert isinstance(accepted.fulfillment_receipt.id, UUID)
    assert isinstance(accepted.fulfillment_receipt.issued_at, datetime)
    assert accepted.assessment.acceptance_state == "accepted"
    await service.bind_pr(
        ticket.id,
        actor_project="brain-v42",
        deliverable_key="implementation",
        repository_id=_REPOSITORY_ID,
        pr_number=43,
        expected_revision=1,
        expected_workflow_version=inputs.workflow_version,
        idempotency_key=f"receipt-view-replacement-{ticket.id}",
    )

    superseded = await service.get(ticket.id, actor_project="brain-v42")

    assert superseded.integration_receipt is None
    assert superseded.fulfillment_receipt is None
    assert superseded.assessment.acceptance_state == "pending"


async def test_load_inputs_rejects_receipt_when_row_issuer_differs_from_frozen_proof(
    session_factory,
) -> None:
    """A mutable index column must not authenticate a different proof issuer."""
    ticket, _service, _binding, inputs = await _observed_workflow(session_factory)
    receipt = _receipt(inputs, milestone="integration")
    await _insert_receipt(session_factory, receipt)

    coherent = await PgDeliveryRepo(session_factory).load_inputs(
        ticket.id, feature_enabled=True, freshness_seconds=3_600
    )

    assert coherent is not None
    assert coherent.integration_receipt is not None
    assert coherent.integration_receipt.id == receipt.id
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                sa.update(delivery_receipts)
                .where(delivery_receipts.c.id == receipt.id)
                .values(issuer="different-row-issuer")
            )

    rejected = await PgDeliveryRepo(session_factory).load_inputs(
        ticket.id, feature_enabled=True, freshness_seconds=3_600
    )

    assert rejected is not None
    assert rejected.integration_receipt is None


async def test_matching_dependency_receipt_hydrates_using_the_upstream_delivery_identity(
    session_factory,
) -> None:
    """The placeholder dependency hash must not hide a real matching upstream receipt."""
    upstream, _downstream, upstream_inputs, inputs = await _dependent_inputs(session_factory)
    receipt = _receipt(upstream_inputs, milestone="integration")
    await _insert_receipt(session_factory, receipt)

    hydrated = await PgDeliveryRepo(session_factory).load_inputs(
        inputs.contract.ticket_id, feature_enabled=True, freshness_seconds=3_600
    )

    assert hydrated is not None
    dependency = hydrated.dependencies[0]
    assert dependency.ticket_id == upstream.id
    assert dependency.receipt is not None
    assert dependency.receipt.id == receipt.id
    assert not any(
        finding.code.startswith("dependency_")
        for finding in evaluate_delivery(hydrated, now=datetime.now(UTC)).blockers
    )


@pytest.mark.parametrize(
    ("case", "dependency_milestone", "receipt_changes"),
    (
        ("absent", "integrated", None),
        (
            "wrong_milestone_for_integrated",
            "integrated",
            {"milestone": "fulfilled", "acceptance_basis": "automatic"},
        ),
        ("wrong_attempt", "integrated", {"attempt": 2}),
        ("wrong_contract_revision", "integrated", {"contract_revision": 2}),
        ("wrong_contract_digest", "integrated", {"contract_digest": "d" * 64}),
        ("wrong_delivery_digest", "integrated", {"delivery_digest": "e" * 64}),
        ("wrong_milestone_for_accepted", "accepted", {"milestone": "integration"}),
    ),
)
async def test_dependency_hydration_ignores_receipts_that_do_not_match_the_pin_or_identity(
    session_factory,
    case: str,
    dependency_milestone: str,
    receipt_changes: dict[str, object] | None,
) -> None:
    """Selecting a receipt by ticket alone would satisfy an unrelated upstream delivery."""
    _upstream, _downstream, upstream_inputs, inputs = await _dependent_inputs(
        session_factory, milestone=dependency_milestone
    )
    if receipt_changes is not None:
        changes = dict(receipt_changes)
        milestone = str(changes.pop("milestone", "integration"))
        if changes.get("contract_revision") == 2:
            await _insert_historical_contract_revision(session_factory, upstream_inputs.contract)
        receipt = _receipt(upstream_inputs, milestone=milestone, **changes)
        await _insert_receipt(session_factory, receipt)

    hydrated = await PgDeliveryRepo(session_factory).load_inputs(
        inputs.contract.ticket_id, feature_enabled=True, freshness_seconds=3_600
    )

    assert hydrated is not None, case
    assert hydrated.dependencies[0].receipt is None, case
    assert "dependency_receipt_missing" in {
        finding.code for finding in evaluate_delivery(hydrated, now=datetime.now(UTC)).blockers
    }


async def test_accepted_dependency_hydrates_a_matching_fulfillment_receipt(session_factory) -> None:
    """An accepted dependency needs fulfillment evidence, not merely an integration receipt."""
    upstream, _downstream, upstream_inputs, inputs = await _dependent_inputs(
        session_factory, milestone="accepted"
    )
    receipt = _receipt(upstream_inputs, milestone="fulfilled")
    await _insert_receipt(session_factory, receipt)

    hydrated = await PgDeliveryRepo(session_factory).load_inputs(
        inputs.contract.ticket_id, feature_enabled=True, freshness_seconds=3_600
    )

    assert hydrated is not None
    assert hydrated.dependencies[0].ticket_id == upstream.id
    assert hydrated.dependencies[0].receipt is not None
    assert hydrated.dependencies[0].receipt.id == receipt.id


async def test_replaced_binding_supersedes_a_previously_matching_dependency_receipt(
    session_factory,
) -> None:
    """A receipt for the old active binding cannot authorize its replacement."""
    upstream, _downstream, upstream_inputs, inputs = await _dependent_inputs(session_factory)
    receipt = _receipt(upstream_inputs, milestone="integration")
    await _insert_receipt(session_factory, receipt)
    before = await PgDeliveryRepo(session_factory).load_inputs(
        inputs.contract.ticket_id, feature_enabled=True, freshness_seconds=3_600
    )
    assert before is not None
    assert before.dependencies[0].receipt is not None
    upstream_service = _service(session_factory)
    await upstream_service.bind_pr(
        upstream.id,
        actor_project="brain-v42",
        deliverable_key="implementation",
        repository_id=_REPOSITORY_ID,
        pr_number=43,
        expected_revision=1,
        expected_workflow_version=upstream_inputs.workflow_version,
        idempotency_key=f"receipt-replacement-{upstream.id}",
    )

    after = await PgDeliveryRepo(session_factory).load_inputs(
        inputs.contract.ticket_id, feature_enabled=True, freshness_seconds=3_600
    )

    assert after is not None
    assert after.dependencies[0].receipt is None


@pytest.mark.parametrize(
    ("dependency_milestone", "receipt_milestone"),
    (("integrated", "integration"), ("accepted", "fulfilled")),
)
async def test_retained_success_keeps_matching_dependency_receipt_through_provider_outage(
    session_factory, dependency_milestone: str, receipt_milestone: str
) -> None:
    """Replacing retained success proof with a failed poll invalidates a sound receipt."""
    upstream, _downstream, upstream_inputs, inputs = await _dependent_inputs(
        session_factory, milestone=dependency_milestone
    )
    receipt = _receipt(upstream_inputs, milestone=receipt_milestone)
    await _insert_receipt(session_factory, receipt)
    binding = upstream_inputs.active_bindings[0].binding
    instant = datetime.now(UTC)
    async with session_factory() as session:
        async with session.begin():
            await PgDeliveryEvidenceRepo(session_factory).record_observation_error(
                session,
                binding_id=binding.id,
                expected_binding_version=2,
                code="provider_timeout",
                collection_started_at=instant,
                collection_finished_at=instant + timedelta(seconds=1),
            )

    hydrated = await PgDeliveryRepo(session_factory).load_inputs(
        inputs.contract.ticket_id, feature_enabled=True, freshness_seconds=3_600
    )

    assert hydrated is not None
    assert hydrated.dependencies[0].receipt is not None
    assert hydrated.dependencies[0].receipt.id == receipt.id


async def test_upstream_amendment_rejects_a_dependency_receipt_from_the_pinned_generation(
    session_factory,
) -> None:
    """A receipt for a prior contract revision cannot satisfy the downstream pin."""
    upstream, _downstream, upstream_inputs, inputs = await _dependent_inputs(session_factory)
    receipt = _receipt(upstream_inputs, milestone="integration")
    await _insert_receipt(session_factory, receipt)
    before = await PgDeliveryRepo(session_factory).load_inputs(
        inputs.contract.ticket_id, feature_enabled=True, freshness_seconds=3_600
    )
    assert before is not None
    assert before.dependencies[0].receipt is not None
    upstream_service = _service(session_factory)
    await upstream_service.set_contract(
        upstream.id,
        actor_project="brain-v42",
        expected_revision=1,
        idempotency_key=f"receipt-amendment-{upstream.id}",
        contract=_contract(objective="amended upstream contract changes its receipt identity"),
    )

    after = await PgDeliveryRepo(session_factory).load_inputs(
        inputs.contract.ticket_id, feature_enabled=True, freshness_seconds=3_600
    )

    assert after is not None
    assert after.dependencies[0].current_contract_revision == 2
    assert after.dependencies[0].receipt is None
    assert "dependency_generation_mismatch" in {
        finding.code for finding in evaluate_delivery(after, now=datetime.now(UTC)).blockers
    }


async def test_upstream_attempt_change_rejects_a_dependency_receipt_from_the_pinned_attempt(
    session_factory,
) -> None:
    """A receipt from attempt one cannot satisfy a workflow that has advanced to attempt two."""
    upstream, _downstream, upstream_inputs, inputs = await _dependent_inputs(session_factory)
    receipt = _receipt(upstream_inputs, milestone="integration")
    await _insert_receipt(session_factory, receipt)
    before = await PgDeliveryRepo(session_factory).load_inputs(
        inputs.contract.ticket_id, feature_enabled=True, freshness_seconds=3_600
    )
    assert before is not None
    assert before.dependencies[0].receipt is not None
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                sa.update(delivery_workflows)
                .where(delivery_workflows.c.ticket_id == upstream.id)
                .values(attempt=2)
            )

    after = await PgDeliveryRepo(session_factory).load_inputs(
        inputs.contract.ticket_id, feature_enabled=True, freshness_seconds=3_600
    )

    assert after is not None
    assert after.dependencies[0].current_attempt == 2
    assert after.dependencies[0].receipt is None
    assert "dependency_generation_mismatch" in {
        finding.code for finding in evaluate_delivery(after, now=datetime.now(UTC)).blockers
    }


@pytest.mark.parametrize("disposition", ("cancelled", "wontfix"))
async def test_unsuccessful_upstream_disposition_rejects_historical_dependency_receipt(
    session_factory, disposition: str
) -> None:
    """A terminal unsuccessful upstream cannot satisfy a dependency through prior acceptance."""
    upstream, _downstream, upstream_inputs, inputs = await _dependent_inputs(session_factory)
    receipt = _receipt(upstream_inputs, milestone="integration")
    await _insert_receipt(session_factory, receipt)
    before = await PgDeliveryRepo(session_factory).load_inputs(
        inputs.contract.ticket_id, feature_enabled=True, freshness_seconds=3_600
    )
    assert before is not None
    assert before.dependencies[0].receipt is not None
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                sa.update(delivery_workflows)
                .where(delivery_workflows.c.ticket_id == upstream.id)
                .values(disposition=disposition)
            )

    after = await PgDeliveryRepo(session_factory).load_inputs(
        inputs.contract.ticket_id, feature_enabled=True, freshness_seconds=3_600
    )

    assert after is not None
    assert "dependency_unsuccessful" in {
        finding.code for finding in evaluate_delivery(after, now=datetime.now(UTC)).blockers
    }
