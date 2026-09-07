"""Real PostgreSQL RED contracts for direct immutable receipt issuance."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import datetime, timedelta
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa

from brain_v42.db.tables import (
    decisions,
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
    DeliveryError,
    PinnedBrainEntityReference,
    PullRequestEvidence,
    ReceiptIssuerProvenance,
    RepositoryContextEvidence,
    RepositoryDocumentFact,
    RepositoryDocumentReference,
    RequiredCheck,
    ReviewPolicy,
    context_reference_digest,
)
from brain_v42.models.delivery_evaluator import evaluate_delivery
from brain_v42.models.ticket import TicketCreate, TicketKind
from brain_v42.repositories.pg_delivery import PgDeliveryRepo
from brain_v42.repositories.pg_delivery_evidence import PgDeliveryEvidenceRepo
from brain_v42.repositories.pg_ticket import PgTicketRepo
from brain_v42.services.delivery_service import DeliveryService

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]
RID = 1_337_360_966
A, B, C, D = "a" * 40, "b" * 40, "c" * 40, "d" * 40


def _contract(
    *,
    mode="automatic",
    refs=(),
    dependencies=(),
    checks=(),
    approvals=0,
    source: UUID | None = None,
):
    context_refs = tuple(refs) + (
        ()
        if source is None
        else (BrainEntityReference(kind="brain_entity", entity_type="decision", entity_id=source),)
    )
    return ContractInput(
        objective="freeze receipt proof from current PostgreSQL facts",
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
                no_checks_reason=None if checks else "issuer test",
                review=ReviewPolicy(required_approvals=approvals, allowed_reviewers=()),
            ),
        ),
    )


async def _workflow(factory, **options):
    ticket = await PgTicketRepo(factory).create(
        TicketCreate(
            kind=TicketKind.REQUEST,
            title=f"receipt {uuid4()}",
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
        idempotency_key=f"contract-{ticket.id}",
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
        idempotency_key=f"binding-{ticket.id}",
    )
    return ticket, binding, service


def _proof(
    now: datetime,
    *,
    state="merged",
    checks=(),
    pr_number=42,
    integration_sha: str | None = C,
    integration_revision: str | None = None,
):
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
        integration_sha=integration_sha,
        integration_revision=integration_revision,
        checks=tuple(checks),
        collected_at=now,
    )


def _repository_ref():
    return RepositoryDocumentReference(
        kind="repository_document", repository_id=RID, sha=A, path="docs/receipt.md"
    )


def _context(*, changed=False):
    return RepositoryContextEvidence(
        complete=True,
        facts=(
            RepositoryDocumentFact(
                repository_id=RID,
                commit_sha=A,
                path="docs/other.md" if changed else "docs/receipt.md",
                tree_sha=C,
                blob_sha=D,
                mode="100644",
                status="available",
            ),
        ),
    )


def _issuer(factory, *, freshness=3600, provenance=None):
    """Actual 4B1b1 constructor: absent at RED base, deliberately no private bypass."""
    return PgDeliveryEvidenceRepo(
        factory,
        settings=DeliverySettings(enabled=True, freshness_seconds=freshness),
        observer_provenance=provenance
        or ReceiptIssuerProvenance(
            issuer_project="brain-v42",
            issuer_identity="receipt-issuer-test",
            issuer_kind="observer",
        ),
    )


async def _now(session):
    value = await session.scalar(sa.select(sa.func.clock_timestamp()))
    assert isinstance(value, datetime)
    return value


async def _publish(factory, session, binding, evidence, at):
    return await PgDeliveryEvidenceRepo(factory).publish_observation(
        session, binding.id, binding.binding_version, evidence, at, at
    )


async def _publish_context(factory, session, ticket_id, at, evidence=None):
    row = (
        (
            await session.execute(
                sa.select(delivery_workflows).where(delivery_workflows.c.ticket_id == ticket_id)
            )
        )
        .mappings()
        .one()
    )
    return await PgDeliveryEvidenceRepo(factory).publish_repository_context(
        session,
        ticket_id,
        row["current_revision"],
        row["attempt"],
        row["context_set_digest"],
        row["context_row_version"],
        evidence or _context(),
        at,
        at,
    )


async def _issue(repo, session, ticket_id):
    return await repo.issue_integration_receipt(session, ticket_id)


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


@pytest.mark.parametrize(("integration_sha", "integration_revision"), ((C, None), (None, D)))
async def test_valid_direct_issuance_freezes_exact_full_proof_and_automatic_fulfillment(
    session_factory, integration_sha, integration_revision
):
    """Removing a frozen ID/digest/issuer or automatic sibling must fail this real receipt boundary."""
    async with session_factory() as session:
        async with session.begin():
            source_id = (
                await session.execute(
                    decisions.insert()
                    .values(title="receipt source", description="pinned", reasoning="test")
                    .returning(decisions.c.id)
                )
            ).scalar_one()
    ticket, binding, _ = await _workflow(
        session_factory, refs=(_repository_ref(),), source=source_id
    )
    async with session_factory() as session:
        async with session.begin():
            now = await _now(session)
            await _publish_context(session_factory, session, ticket.id, now)
            await _publish(
                session_factory,
                session,
                binding,
                _proof(
                    now,
                    integration_sha=integration_sha,
                    integration_revision=integration_revision,
                ),
                now,
            )
    inputs = await PgDeliveryRepo(session_factory).load_inputs(
        ticket.id, feature_enabled=True, freshness_seconds=3600
    )
    assert inputs is not None
    artifact_input = inputs.active_bindings[0]
    artifact_confirmation = artifact_input.confirmation
    repository_input = next(
        item
        for item in inputs.contexts
        if item.reference_identity.startswith("repository_document:")
    )
    repository_reference = next(
        reference
        for reference in inputs.contract.context_refs
        if isinstance(reference, RepositoryDocumentReference)
    )
    expected_context_digest = context_reference_digest(repository_reference)
    assert expected_context_digest is not None
    brain_input = next(
        item for item in inputs.contexts if item.reference_identity.startswith("brain_entity:")
    )
    assert artifact_confirmation is not None
    assert artifact_input.snapshot_id is not None
    assert artifact_input.success_confirmation_id is not None
    assert artifact_input.latest_attempt_confirmation_id is not None
    assert repository_input.snapshot_id is not None
    assert repository_input.success_confirmation_id is not None
    assert repository_input.latest_attempt_confirmation_id is not None
    expected_assessment = evaluate_delivery(inputs, now=now)
    async with session_factory() as session:
        artifact_digest = await session.scalar(
            sa.select(delivery_snapshots.c.semantic_digest).where(
                delivery_snapshots.c.id == artifact_input.snapshot_id
            )
        )
        repository_digest = await session.scalar(
            sa.select(delivery_snapshots.c.semantic_digest).where(
                delivery_snapshots.c.id == repository_input.snapshot_id
            )
        )
    async with session_factory() as session:
        async with session.begin():
            before_decision = await _now(session)
            receipt = await _issue(_issuer(session_factory), session, ticket.id)
            after_decision = await _now(session)
            assert receipt is not None and receipt.milestone == "integration"
            assert before_decision <= receipt.issued_at <= after_decision
            assert receipt.ticket_id == ticket.id
            assert (
                receipt.contract_revision,
                receipt.attempt,
                receipt.contract_digest,
                receipt.delivery_digest,
                receipt.proof.contract_revision,
                receipt.proof.attempt,
                receipt.proof.contract_digest,
                receipt.proof.delivery_digest,
                receipt.proof.workflow_version,
                receipt.proof.assessment_id,
            ) == (
                inputs.contract.contract_revision,
                inputs.attempt,
                inputs.contract.content_digest,
                expected_assessment.delivery_digest,
                inputs.contract.contract_revision,
                inputs.attempt,
                inputs.contract.content_digest,
                expected_assessment.delivery_digest,
                inputs.workflow_version,
                expected_assessment.assessment_id,
            )
            artifact = receipt.proof.artifact_proofs[0]
            context = receipt.proof.repository_context_proofs[0]
            brain = receipt.proof.brain_context_proofs[0]
            assert (
                artifact.binding_id,
                artifact.binding_version,
                artifact.head_sha,
                artifact.base_sha,
                artifact.integration_sha,
                artifact.integration_revision,
                artifact.snapshot_id,
                artifact.snapshot_digest,
                artifact.success_confirmation_id,
                artifact.latest_attempt_confirmation_id,
                artifact.collection_started_at,
                artifact.collection_finished_at,
            ) == (
                binding.id,
                artifact_input.binding.binding_version,
                A,
                B,
                integration_sha,
                integration_revision,
                artifact_input.snapshot_id,
                artifact_digest,
                artifact_input.success_confirmation_id,
                artifact_input.latest_attempt_confirmation_id,
                artifact_confirmation.collection_started_at,
                artifact_confirmation.collection_finished_at,
            )
            assert (
                context.reference_identity,
                context.required,
                context.expected_digest,
                context.current_digest,
                context.snapshot_id,
                context.snapshot_digest,
                context.success_confirmation_id,
                context.latest_attempt_confirmation_id,
                context.collection_started_at,
                context.collection_finished_at,
            ) == (
                repository_input.reference_identity,
                repository_reference.required,
                expected_context_digest,
                repository_input.current_digest,
                repository_input.snapshot_id,
                repository_digest,
                repository_input.success_confirmation_id,
                repository_input.latest_attempt_confirmation_id,
                repository_input.collection_started_at,
                repository_input.collection_finished_at,
            )
            assert brain.reference_identity == brain_input.reference_identity
            assert brain.current_digest == brain_input.current_digest
            pinned_brain = next(
                reference
                for reference in inputs.contract.context_refs
                if isinstance(reference, PinnedBrainEntityReference)
            )
            assert brain.pinned_digest == pinned_brain.content_digest
            assert receipt.proof.issuer.issuer_identity == "receipt-issuer-test"
    rows = await _rows(session_factory, ticket.id)
    assert [row["milestone"] for row in rows] == ["fulfilled", "integration"]
    assert {row["issuer"] for row in rows} == {"receipt-issuer-test"}
    assert {row["basis"] for row in rows} == {None, "automatic"}


async def test_explicit_mode_returns_integration_only(session_factory):
    """Fabricating a requester decision for explicit mode must be impossible in 4B1b1."""
    ticket, binding, _ = await _workflow(session_factory, mode="explicit")
    async with session_factory() as session:
        async with session.begin():
            now = await _now(session)
            await _publish(session_factory, session, binding, _proof(now), now)
            receipt = await _issue(_issuer(session_factory), session, ticket.id)
            assert receipt is not None and receipt.acceptance_basis is None
    assert [row["milestone"] for row in await _rows(session_factory, ticket.id)] == ["integration"]


async def test_optional_repository_reference_needs_no_collection_to_issue(session_factory):
    """Optional repository context must not create a collector prerequisite or block issuance."""
    optional = _repository_ref().model_copy(update={"required": False})
    ticket, binding, _ = await _workflow(session_factory, refs=(optional,))
    async with session_factory() as session:
        async with session.begin():
            now = await _now(session)
            await _publish(session_factory, session, binding, _proof(now), now)
            receipt = await _issue(_issuer(session_factory), session, ticket.id)
            assert receipt is not None
            assert receipt.proof.repository_context_proofs == ()


async def test_disabled_and_invalid_composition_stay_safe(session_factory, monkeypatch):
    """An ambient enabled setting or requester provenance must not activate side effects."""
    monkeypatch.setenv("BRAIN_DELIVERY_ENABLED", "true")
    ticket, binding, _ = await _workflow(session_factory, refs=(_repository_ref(),))
    async with session_factory() as session:
        async with session.begin():
            now = await _now(session)
            await _publish_context(session_factory, session, ticket.id, now)
            await _publish(session_factory, session, binding, _proof(now), now)
            assert await _issue(PgDeliveryEvidenceRepo(session_factory), session, ticket.id) is None
    with pytest.raises(DeliveryError):
        PgDeliveryEvidenceRepo(session_factory, settings=DeliverySettings(enabled=True))
    with pytest.raises(DeliveryError):
        _issuer(
            session_factory,
            provenance=ReceiptIssuerProvenance(
                issuer_project="brain-v42", issuer_identity="requester", issuer_kind="requester"
            ),
        )
    assert await _rows(session_factory, ticket.id) == []


@pytest.mark.parametrize("disposition", ["cancelled", "wontfix", "fulfilled"])
async def test_terminal_workflow_disposition_blocks_valid_active_ticket(
    session_factory, disposition
):
    """A terminal workflow disposition cannot be bypassed by fresh complete ticket evidence."""
    ticket, binding, _ = await _workflow(session_factory, refs=(_repository_ref(),))
    async with session_factory() as session:
        async with session.begin():
            now = await _now(session)
            await _publish_context(session_factory, session, ticket.id, now)
            await _publish(session_factory, session, binding, _proof(now), now)
            assert (
                await session.scalar(sa.select(tickets.c.status).where(tickets.c.id == ticket.id))
                == "open"
            )
            await session.execute(
                sa.update(delivery_workflows)
                .where(delivery_workflows.c.ticket_id == ticket.id)
                .values(disposition=disposition)
            )
            assert await _issue(_issuer(session_factory), session, ticket.id) is None
    assert await _rows(session_factory, ticket.id) == []


@pytest.mark.parametrize(
    "case",
    ["missing", "pending", "failure", "wrong-head", "review-missing", "open", "bare", "stale"],
)
async def test_technical_or_freshness_ineligibility_returns_none_without_rows(
    session_factory, case
):
    """Merge alone must not bypass missing/pending/failed/wrong-head checks or freshness."""
    required = (
        (RequiredCheck(kind="check_run", name="unit", provider_id=7),)
        if case in {"missing", "pending", "failure", "wrong-head"}
        else ()
    )
    ticket, binding, _ = await _workflow(
        session_factory, checks=required, approvals=1 if case == "review-missing" else 0
    )
    checks = (
        ()
        if case == "missing"
        else (
            CheckAttempt(
                record_id=1,
                provider_id=7,
                kind="check_run",
                name="unit",
                head_sha=B if case == "wrong-head" else A,
                conclusion="pending"
                if case == "pending"
                else "failure"
                if case == "failure"
                else "success",
            ),
        )
    )
    async with session_factory() as session:
        async with session.begin():
            now = await _now(session)
            observed = now - timedelta(seconds=2) if case == "stale" else now
            await _publish(
                session_factory,
                session,
                binding,
                _proof(
                    observed,
                    state="open" if case == "open" else "merged",
                    checks=checks,
                    integration_sha=None if case == "bare" else C,
                ),
                observed,
            )
            assert (
                await _issue(
                    _issuer(session_factory, freshness=1 if case == "stale" else 3600),
                    session,
                    ticket.id,
                )
                is None
            )
    assert await _rows(session_factory, ticket.id) == []


async def test_required_context_missing_or_error_do_not_issue(session_factory):
    """Required context needs current success; a missing or error attempt cannot mint a receipt."""
    ticket, binding, _ = await _workflow(session_factory, refs=(_repository_ref(),))
    async with session_factory() as session:
        async with session.begin():
            now = await _now(session)
            await _publish(session_factory, session, binding, _proof(now), now)
            assert await _issue(_issuer(session_factory), session, ticket.id) is None
            row = (
                (
                    await session.execute(
                        sa.select(delivery_workflows).where(
                            delivery_workflows.c.ticket_id == ticket.id
                        )
                    )
                )
                .mappings()
                .one()
            )
            await PgDeliveryEvidenceRepo(session_factory).record_repository_context_error(
                session,
                ticket.id,
                row["current_revision"],
                row["attempt"],
                row["context_set_digest"],
                row["context_row_version"],
                "provider_unavailable",
                now,
                now,
            )
            assert await _issue(_issuer(session_factory), session, ticket.id) is None
    assert await _rows(session_factory, ticket.id) == []


async def test_real_brain_source_drift_blocks_direct_issue(session_factory):
    """A changed required Brain source must invalidate its pinned proof without fabricated snapshots."""
    async with session_factory() as session:
        async with session.begin():
            source_id = (
                await session.execute(
                    decisions.insert()
                    .values(title="drift source", description="before", reasoning="test")
                    .returning(decisions.c.id)
                )
            ).scalar_one()
    ticket, binding, _ = await _workflow(session_factory, source=source_id)
    async with session_factory() as session:
        async with session.begin():
            now = await _now(session)
            await _publish(session_factory, session, binding, _proof(now), now)
            await session.execute(
                sa.update(decisions).where(decisions.c.id == source_id).values(description="after")
            )
            assert await _issue(_issuer(session_factory), session, ticket.id) is None
    assert await _rows(session_factory, ticket.id) == []


@pytest.mark.parametrize("status", ["wontfix", "closed", "acked"])
async def test_each_terminal_ticket_status_blocks_a_valid_previously_persisted_observation(
    session_factory, status
):
    """The issuer shares the publisher terminal boundary until later ticket guards exist."""
    ticket, binding, _ = await _workflow(session_factory, refs=(_repository_ref(),))
    async with session_factory() as session:
        async with session.begin():
            now = await _now(session)
            await _publish_context(session_factory, session, ticket.id, now)
            await _publish(session_factory, session, binding, _proof(now), now)
            await session.execute(
                sa.update(tickets).where(tickets.c.id == ticket.id).values(status=status)
            )
            assert await _issue(_issuer(session_factory), session, ticket.id) is None
    assert await _rows(session_factory, ticket.id) == []


async def test_incomplete_evidence_is_rejected_by_existing_publisher_and_never_issued(
    session_factory,
):
    """A bypassed incomplete provider DTO cannot become a receipt through a later call."""
    ticket, binding, _ = await _workflow(session_factory)
    async with session_factory() as session:
        async with session.begin():
            now = await _now(session)
            with pytest.raises(DeliveryError):
                await _publish(
                    session_factory,
                    session,
                    binding,
                    _proof(now).model_copy(update={"complete": False}),
                    now,
                )
            assert await _issue(_issuer(session_factory), session, ticket.id) is None
    assert await _rows(session_factory, ticket.id) == []


async def test_provider_outage_blocks_new_issue_without_destroying_prior_receipt(session_factory):
    """A failed later poll cannot mint a new receipt or erase immutable historical rows."""
    ticket, binding, _ = await _workflow(session_factory)
    async with session_factory() as session:
        async with session.begin():
            now = await _now(session)
            await _publish(session_factory, session, binding, _proof(now), now)
            first = await _issue(_issuer(session_factory), session, ticket.id)
            assert first is not None
    historical = await _rows(session_factory, ticket.id)
    async with session_factory() as session:
        async with session.begin():
            now = await _now(session)
            await PgDeliveryEvidenceRepo(session_factory).record_observation_error(
                session, binding.id, 2, "provider_unavailable", now, now
            )
            assert await _issue(_issuer(session_factory), session, ticket.id) is None
    assert [(row["id"], row["payload"]) for row in await _rows(session_factory, ticket.id)] == [
        (row["id"], row["payload"]) for row in historical
    ]
    view = await PgDeliveryRepo(session_factory).get_view(
        ticket.id, feature_enabled=True, freshness_seconds=3600
    )
    assert view is not None
    assert view.assessment.contract_fulfilled is True
    assert view.assessment.completion_eligible_now is False
    assert view.integration_receipt is not None
    assert view.fulfillment_receipt is not None
    assert {view.integration_receipt.id, view.fulfillment_receipt.id} == {
        row["id"] for row in historical
    }


async def test_identical_reissue_preserves_receipt_identity_payload_and_time(session_factory):
    """New confirmations may append but immutable receipts may not be retimestamped or rewritten."""
    ticket, binding, _ = await _workflow(session_factory, refs=(_repository_ref(),))
    async with session_factory() as session:
        async with session.begin():
            now = await _now(session)
            await _publish_context(session_factory, session, ticket.id, now)
            await _publish(session_factory, session, binding, _proof(now), now)
            first = await _issue(_issuer(session_factory), session, ticket.id)
            assert first is not None
    before = await _rows(session_factory, ticket.id)
    async with session_factory() as session:
        async with session.begin():
            now = await _now(session)
            await _publish_context(session_factory, session, ticket.id, now)
            await _publish(
                session_factory,
                session,
                binding.model_copy(update={"binding_version": 2}),
                _proof(now),
                now,
            )
            second = await _issue(_issuer(session_factory), session, ticket.id)
            assert second is not None
    after = await _rows(session_factory, ticket.id)
    assert [(row["id"], row["issued_at"], row["payload"]) for row in after] == [
        (row["id"], row["issued_at"], row["payload"]) for row in before
    ]
    async with session_factory() as session:
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
    assert (artifact_snapshots, context_snapshots) == (1, 1)
    assert (artifact_confirmations, context_confirmations) == (2, 2)


async def test_replacement_preserves_history_and_issues_new_current_digest(session_factory):
    """A superseded binding receipt must remain history and never satisfy its replacement."""
    ticket, binding, service = await _workflow(session_factory)
    async with session_factory() as session:
        async with session.begin():
            now = await _now(session)
            await _publish(session_factory, session, binding, _proof(now), now)
            assert await _issue(_issuer(session_factory), session, ticket.id) is not None
    old = await _rows(session_factory, ticket.id)
    old_history = [(row["id"], row["issued_at"], row["payload"]) for row in old]
    before_replacement = await PgDeliveryRepo(session_factory).load_inputs(
        ticket.id, feature_enabled=True, freshness_seconds=3600
    )
    assert before_replacement is not None
    replacement = await service.bind_pr(
        ticket.id,
        actor_project="brain-v42",
        deliverable_key="implementation",
        repository_id=RID,
        pr_number=43,
        expected_revision=1,
        expected_workflow_version=before_replacement.workflow_version,
        idempotency_key=f"replace-{ticket.id}",
    )
    after_replacement = await PgDeliveryRepo(session_factory).load_inputs(
        ticket.id, feature_enabled=True, freshness_seconds=3600
    )
    assert after_replacement is not None
    assert after_replacement.integration_receipt is None
    async with session_factory() as session:
        async with session.begin():
            now = await _now(session)
            await _publish(
                session_factory,
                session,
                replacement,
                _proof(now).model_copy(update={"pr_number": 43}),
                now,
            )
            replacement_receipt = await _issue(_issuer(session_factory), session, ticket.id)
            assert replacement_receipt is not None
    current = await _rows(session_factory, ticket.id)
    current_history = [(row["id"], row["issued_at"], row["payload"]) for row in current]
    assert all(row in current_history for row in old_history)
    assert len(current) == len(old) + 2
    assert replacement_receipt.delivery_digest != old[0]["delivery_digest"]


async def test_dependency_freezes_matching_real_upstream_receipt_id(session_factory):
    """An upstream merge label cannot substitute for the exact pinned immutable receipt."""
    upstream, upstream_binding, _ = await _workflow(session_factory)
    async with session_factory() as session:
        async with session.begin():
            now = await _now(session)
            await _publish(session_factory, session, upstream_binding, _proof(now), now)
            upstream_receipt = await _issue(_issuer(session_factory), session, upstream.id)
            assert upstream_receipt is not None
    downstream, downstream_binding, _ = await _workflow(
        session_factory,
        dependencies=(
            DeliveryDependency(
                ticket_id=upstream.id, contract_revision=1, attempt=1, milestone="integrated"
            ),
        ),
    )
    async with session_factory() as session:
        async with session.begin():
            now = await _now(session)
            await _publish(session_factory, session, downstream_binding, _proof(now), now)
            receipt = await _issue(_issuer(session_factory), session, downstream.id)
            assert receipt is not None and receipt.proof.upstream_receipt_ids == (
                upstream_receipt.id,
            )


async def test_missing_or_obsolete_upstream_receipt_never_satisfies_dependency(session_factory):
    """A merged upstream without its pinned receipt, or a newer generation, is ineligible."""
    upstream, upstream_binding, upstream_service = await _workflow(session_factory)
    async with session_factory() as session:
        async with session.begin():
            now = await _now(session)
            await _publish(session_factory, session, upstream_binding, _proof(now), now)
    downstream, downstream_binding, _ = await _workflow(
        session_factory,
        dependencies=(
            DeliveryDependency(
                ticket_id=upstream.id, contract_revision=1, attempt=1, milestone="integrated"
            ),
        ),
    )
    async with session_factory() as session:
        async with session.begin():
            now = await _now(session)
            await _publish(session_factory, session, downstream_binding, _proof(now), now)
            assert await _issue(_issuer(session_factory), session, downstream.id) is None
    async with session_factory() as session:
        async with session.begin():
            upstream_receipt = await _issue(_issuer(session_factory), session, upstream.id)
            assert upstream_receipt is not None
    await upstream_service.set_contract(
        upstream.id,
        actor_project="brain-v42",
        expected_revision=1,
        idempotency_key=f"obsolete-upstream-{upstream.id}",
        contract=_contract(),
    )
    async with session_factory() as session:
        async with session.begin():
            assert await _issue(_issuer(session_factory), session, downstream.id) is None
    assert await _rows(session_factory, downstream.id) == []


async def test_caller_rollback_removes_receipts_but_retains_prior_observation(session_factory):
    """The issuer must never commit a caller write or receipts outside its caller transaction."""
    ticket, binding, _ = await _workflow(session_factory)
    async with session_factory() as session:
        async with session.begin():
            now = await _now(session)
            await _publish(session_factory, session, binding, _proof(now), now)
    async with session_factory() as session:
        transaction = await session.begin()
        try:
            marker = (
                await session.execute(
                    decisions.insert()
                    .values(title="marker", description="rollback", reasoning="test")
                    .returning(decisions.c.id)
                )
            ).scalar_one()
            assert await _issue(_issuer(session_factory), session, ticket.id) is not None
        finally:
            await transaction.rollback()
    assert await _rows(session_factory, ticket.id) == []
    async with session_factory() as session:
        assert (
            await session.scalar(sa.select(decisions.c.id).where(decisions.c.id == marker)) is None
        )
        assert (
            await session.scalar(
                sa.select(sa.func.count())
                .select_from(delivery_confirmations)
                .where(delivery_confirmations.c.binding_id == binding.id)
            )
            == 1
        )


async def _wait(observer, waiter, blocker, task):
    for _ in range(100):
        if blocker in await observer.scalar(
            sa.text("SELECT pg_blocking_pids(:pid)"), {"pid": waiter}
        ):
            assert not task.done()
            return
        await asyncio.sleep(0.01)
    raise AssertionError("expected PostgreSQL wait edge")


async def _cancel_waiter(task: asyncio.Task[object] | None) -> None:
    """Cancel and await a blocked issuer before its session rolls back."""
    if task is not None and not task.done():
        task.cancel()
    if task is not None:
        with suppress(asyncio.CancelledError):
            await task


async def test_direct_issuer_waits_on_lower_upstream_before_locking_current_ticket(session_factory):
    """Locking the current ticket before its lower dependency would retain a deadlock edge."""
    ticket_repo = PgTicketRepo(session_factory)
    candidates = []
    for title in ("upstream receipt lock", "current receipt lock"):
        candidates.append(
            await ticket_repo.create(
                TicketCreate(
                    kind=TicketKind.REQUEST,
                    title=f"{title} {uuid4()}",
                    body="direct issuer lock order",
                    from_project="brain-v42",
                    to_project="brain-v42",
                )
            )
        )
    upstream, current = sorted(candidates, key=lambda ticket: str(ticket.id))
    assert str(upstream.id) < str(current.id)
    service = DeliveryService(
        PgDeliveryRepo(session_factory),
        settings=DeliverySettings(enabled=True, freshness_seconds=3600),
    )
    await service.set_contract(
        upstream.id,
        actor_project="brain-v42",
        expected_revision=0,
        idempotency_key=f"upstream-lock-contract-{upstream.id}",
        contract=_contract(),
    )
    await service.set_contract(
        current.id,
        actor_project="brain-v42",
        expected_revision=0,
        idempotency_key=f"current-lock-contract-{current.id}",
        contract=_contract(
            dependencies=(
                DeliveryDependency(
                    ticket_id=upstream.id,
                    contract_revision=1,
                    attempt=1,
                    milestone="integrated",
                ),
            )
        ),
    )
    upstream_binding = await service.bind_pr(
        upstream.id,
        actor_project="brain-v42",
        deliverable_key="implementation",
        repository_id=RID,
        pr_number=41,
        expected_revision=1,
        expected_workflow_version=1,
        idempotency_key=f"upstream-lock-binding-{upstream.id}",
    )
    current_binding = await service.bind_pr(
        current.id,
        actor_project="brain-v42",
        deliverable_key="implementation",
        repository_id=RID,
        pr_number=42,
        expected_revision=1,
        expected_workflow_version=1,
        idempotency_key=f"current-lock-binding-{current.id}",
    )
    async with session_factory() as session:
        async with session.begin():
            now = await _now(session)
            await _publish(
                session_factory, session, upstream_binding, _proof(now, pr_number=41), now
            )
            upstream_receipt = await _issue(_issuer(session_factory), session, upstream.id)
            assert upstream_receipt is not None
            await _publish(session_factory, session, current_binding, _proof(now), now)
    async with (
        session_factory() as blocker,
        session_factory() as issuer,
        session_factory() as probe,
        session_factory() as observer,
    ):
        blocker_transaction = await blocker.begin()
        issuer_transaction = await issuer.begin()
        probe_transaction = await probe.begin()
        task: asyncio.Task[object] | None = None
        try:
            blocker_pid = await blocker.scalar(sa.text("SELECT pg_backend_pid()"))
            await blocker.execute(
                sa.select(tickets.c.id).where(tickets.c.id == upstream.id).with_for_update()
            )
            issuer_pid = await issuer.scalar(sa.text("SELECT pg_backend_pid()"))
            task = asyncio.create_task(_issue(_issuer(session_factory), issuer, current.id))
            await _wait(observer, issuer_pid, blocker_pid, task)
            assert (
                await probe.scalar(
                    sa.select(tickets.c.id)
                    .where(tickets.c.id == current.id)
                    .with_for_update(nowait=True)
                )
                == current.id
            )
            await probe_transaction.commit()
            await blocker_transaction.commit()
            receipt = await asyncio.wait_for(task, timeout=5)
            assert receipt is not None
            await issuer_transaction.commit()
        finally:
            await _cancel_waiter(task)
            if blocker.in_transaction():
                await blocker.rollback()
            if issuer.in_transaction():
                await issuer.rollback()
            if probe.in_transaction():
                await probe.rollback()


async def test_required_source_wait_crosses_freshness_before_direct_issue(session_factory):
    """Reading the decision clock before a source wait would turn stale evidence into a receipt."""
    async with session_factory() as seed:
        async with seed.begin():
            source = (
                await seed.execute(
                    decisions.insert()
                    .values(title="source", description="before", reasoning="test")
                    .returning(decisions.c.id)
                )
            ).scalar_one()
    ticket, binding, _ = await _workflow(session_factory, source=source)
    async with session_factory() as session:
        async with session.begin():
            confirmation_finished = await _now(session)
            await _publish(
                session_factory,
                session,
                binding,
                _proof(confirmation_finished),
                confirmation_finished,
            )
    repo = _issuer(session_factory, freshness=1)
    async with (
        session_factory() as blocker,
        session_factory() as issuer,
        session_factory() as observer,
    ):
        first, second, task = await blocker.begin(), await issuer.begin(), None
        try:
            await blocker.execute(
                sa.select(decisions.c.id).where(decisions.c.id == source).with_for_update()
            )
            blocker_pid, issuer_pid = (
                await blocker.scalar(sa.text("SELECT pg_backend_pid()")),
                await issuer.scalar(sa.text("SELECT pg_backend_pid()")),
            )
            transaction_time = await issuer.scalar(sa.select(sa.func.transaction_timestamp()))
            assert isinstance(transaction_time, datetime)
            task = asyncio.create_task(_issue(repo, issuer, ticket.id))
            await _wait(observer, issuer_pid, blocker_pid, task)
            await asyncio.sleep(1.1)
            released = await blocker.scalar(sa.select(sa.func.clock_timestamp()))
            assert transaction_time < confirmation_finished + timedelta(seconds=1) <= released
            await first.commit()
            assert (
                await asyncio.wait_for(task, 5) is None
                and await issuer.scalar(sa.select(sa.func.clock_timestamp())) >= released
            )
            await second.commit()
        finally:
            await _cancel_waiter(task)
            if blocker.in_transaction():
                await blocker.rollback()
            if issuer.in_transaction():
                await issuer.rollback()
    assert await _rows(session_factory, ticket.id) == []
