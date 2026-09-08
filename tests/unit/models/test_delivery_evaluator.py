"""Literal behavioral matrix for pure observable-delivery assessment."""

from datetime import timedelta
from typing import Any, Literal
from uuid import UUID


def _replace_evidence(inputs: Any, **changes: Any) -> Any:
    """Return a new input with literal evidence changes, without evaluator helpers."""
    current = inputs.active_bindings[0]
    evidence = current.confirmation.evidence.model_copy(update=changes)
    confirmation = current.confirmation.model_copy(update={"evidence": evidence})
    return inputs.model_copy(
        update={"active_bindings": (current.model_copy(update={"confirmation": confirmation}),)}
    )


def _receipt(
    inputs: Any,
    result: Any,
    milestone: Literal["integration", "fulfilled"],
    *,
    ticket_id: UUID | None = None,
    contract_revision: int | None = None,
    attempt: int | None = None,
    contract_digest: str | None = None,
    delivery_digest: str | None = None,
) -> Any:
    from brain_v42.models.delivery import MilestoneReceipt
    from tests.delivery_helpers import FIXED_NOW

    binding_evidence = inputs.active_bindings[0]
    evidence = binding_evidence.confirmation.evidence
    assert evidence is not None
    actual_ticket_id = ticket_id or inputs.contract.ticket_id
    actual_contract_revision = contract_revision or inputs.contract.contract_revision
    actual_attempt = attempt or inputs.attempt
    actual_contract_digest = contract_digest or inputs.contract.content_digest
    actual_delivery_digest = delivery_digest or result.delivery_digest
    basis = "automatic" if milestone == "fulfilled" else None
    proof = {
        "ticket_id": actual_ticket_id,
        "contract_revision": actual_contract_revision,
        "attempt": actual_attempt,
        "workflow_version": inputs.workflow_version,
        "contract_digest": actual_contract_digest,
        "delivery_digest": actual_delivery_digest,
        "assessment_id": result.assessment_id,
        "decision_time": FIXED_NOW,
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
                "snapshot_id": UUID("00000000-0000-0000-0000-000000000021"),
                "snapshot_digest": "f" * 64,
                "success_confirmation_id": binding_evidence.confirmation.id,
                "latest_attempt_confirmation_id": binding_evidence.confirmation.id,
                "collection_started_at": binding_evidence.confirmation.collection_started_at,
                "collection_finished_at": binding_evidence.confirmation.collection_finished_at,
            },
        ),
        "brain_context_proofs": (
            {
                "reference_identity": "brain_entity:adr:00000000-0000-0000-0000-000000000060",
                "pinned_digest": "d" * 64,
                "current_digest": "d" * 64,
            },
        ),
        "repository_context_proofs": (),
        "upstream_receipt_ids": (),
        "issuer": {
            "issuer_project": "brain-v42",
            "issuer_identity": "delivery-observer",
            "issuer_kind": "observer",
        },
        "acceptance_basis": basis,
        "explicit_acceptance": None,
    }
    return MilestoneReceipt(
        id=UUID(
            "00000000-0000-0000-0000-000000000030"
            if milestone == "integration"
            else "00000000-0000-0000-0000-000000000031"
        ),
        ticket_id=actual_ticket_id,
        milestone=milestone,
        contract_revision=actual_contract_revision,
        attempt=actual_attempt,
        contract_digest=actual_contract_digest,
        delivery_digest=actual_delivery_digest,
        issued_at=FIXED_NOW,
        acceptance_basis=basis,
        proof=proof,
    )


def test_matching_explicit_integration_receipt_remains_pending() -> None:
    """Treating a matching integration receipt as superseded hides requester acceptance work."""
    from brain_v42.models.delivery_evaluator import evaluate_delivery
    from tests.delivery_helpers import FIXED_NOW, delivery_inputs

    inputs = delivery_inputs(acceptance_mode="explicit")
    assessment = evaluate_delivery(inputs, now=FIXED_NOW)
    matching = inputs.model_copy(
        update={"integration_receipt": _receipt(inputs, assessment, "integration")}
    )

    result = evaluate_delivery(matching, now=FIXED_NOW)

    assert result.acceptance_state == "pending"
    assert {item.kind for item in result.eligible_work} == {"accept"}


def test_a_bare_merge_does_not_satisfy_technical_delivery() -> None:
    """A failed required check must block a merged PR from receipt eligibility."""
    from brain_v42.models.delivery_evaluator import evaluate_delivery
    from tests.delivery_helpers import FIXED_NOW, delivery_inputs

    result = evaluate_delivery(
        delivery_inputs(merged=True, required_check="failure"), now=FIXED_NOW
    )

    assert result.delivery_stage == "integrated"
    assert result.integration_receipt_eligible is False
    assert result.completion_eligible_now is False
    assert result.contract_fulfilled is False
    assert "check_failed" in {item.code for item in result.blockers}


def test_absent_binding_leaves_executor_implementation_eligible() -> None:
    from brain_v42.models.delivery_evaluator import evaluate_delivery
    from tests.delivery_helpers import FIXED_NOW, delivery_inputs

    result = evaluate_delivery(delivery_inputs(active_bindings=()), now=FIXED_NOW)

    assert result.delivery_stage == "awaiting_artifact"
    assert {item.kind for item in result.eligible_work} == {"implement"}
    assert {item.role for item in result.eligible_work} == {"executor"}
    assert {item.code for item in result.blockers} == {"binding_missing"}


def test_draft_pr_does_not_advance_beyond_proposed() -> None:
    from brain_v42.models.delivery_evaluator import evaluate_delivery
    from tests.delivery_helpers import FIXED_NOW, delivery_inputs

    result = evaluate_delivery(delivery_inputs(merged=False, draft=True), now=FIXED_NOW)

    assert result.delivery_stage == "proposed"
    assert "pr_draft" in {item.code for item in result.blockers}
    assert {item.kind for item in result.eligible_work} == {"implement"}


def test_newer_pending_check_attempt_supersedes_old_success() -> None:
    from brain_v42.models.delivery import CheckAttempt
    from brain_v42.models.delivery_evaluator import evaluate_delivery
    from tests.delivery_helpers import FIXED_NOW, delivery_inputs

    old = CheckAttempt(
        record_id=8001,
        provider_id=8001,
        kind="check_run",
        name="test-unit",
        app_slug="github-actions",
        head_sha="a" * 40,
        conclusion="success",
        started_at=FIXED_NOW - timedelta(seconds=40),
        completed_at=FIXED_NOW - timedelta(seconds=40),
    )
    rerun = CheckAttempt(
        record_id=8002,
        provider_id=8002,
        kind="check_run",
        name="test-unit",
        app_slug="github-actions",
        head_sha="a" * 40,
        conclusion="pending",
        started_at=None,
        completed_at=None,
    )
    result = evaluate_delivery(delivery_inputs(checks=(old, rerun)), now=FIXED_NOW)

    assert "check_pending" in {item.code for item in result.blockers}
    assert result.integration_receipt_eligible is False


def test_effective_change_request_and_self_approval_do_not_count() -> None:
    from brain_v42.models.delivery import ReviewEvidence
    from brain_v42.models.delivery_evaluator import evaluate_delivery
    from tests.delivery_helpers import FIXED_NOW, delivery_inputs

    self_approval = ReviewEvidence(
        record_id=9010,
        provider_id=9010,
        reviewer="executor-project",
        head_sha="a" * 40,
        decision="approved",
        submitted_at=FIXED_NOW - timedelta(seconds=20),
    )
    changed = ReviewEvidence(
        record_id=9011,
        provider_id=9011,
        reviewer="reviewer-project",
        head_sha="a" * 40,
        decision="changes_requested",
        submitted_at=FIXED_NOW - timedelta(seconds=10),
    )
    result = evaluate_delivery(
        delivery_inputs(
            merged=False,
            required_approvals=1,
            allowed_reviewers=["reviewer-project"],
            reviews=(self_approval, changed),
        ),
        now=FIXED_NOW,
    )

    assert "review_changes_requested" in {item.code for item in result.blockers}
    assert "review_approval_missing" in {item.code for item in result.blockers}


def test_head_base_mismatch_and_incomplete_snapshot_block_technical_receipt() -> None:
    from brain_v42.models.delivery_evaluator import evaluate_delivery
    from tests.delivery_helpers import FIXED_NOW, delivery_inputs

    mismatch = evaluate_delivery(delivery_inputs(head_sha="e" * 40), now=FIXED_NOW)
    incomplete = evaluate_delivery(delivery_inputs(complete=False), now=FIXED_NOW)

    assert "head_mismatch" in {item.code for item in mismatch.blockers}
    assert "observation_incomplete" in {item.code for item in incomplete.blockers}
    assert mismatch.integration_receipt_eligible is False
    assert incomplete.integration_receipt_eligible is False


def test_stale_observation_blocks_new_completion_but_historical_fulfillment_survives() -> None:
    from brain_v42.models.delivery_evaluator import evaluate_delivery
    from tests.delivery_helpers import FIXED_NOW, delivery_inputs

    initial = delivery_inputs(acceptance_mode="automatic")
    initial_result = evaluate_delivery(initial, now=FIXED_NOW)
    fulfilled = _receipt(initial, initial_result, "fulfilled")
    stale = _replace_evidence(
        initial.model_copy(update={"fulfillment_receipt": fulfilled}),
        collected_at=FIXED_NOW - timedelta(seconds=601),
    )
    stale = stale.model_copy(
        update={
            "active_bindings": (
                stale.active_bindings[0].model_copy(
                    update={
                        "confirmation": stale.active_bindings[0].confirmation.model_copy(
                            update={
                                "collection_started_at": FIXED_NOW - timedelta(seconds=606),
                                "collection_finished_at": FIXED_NOW - timedelta(seconds=601),
                            }
                        )
                    }
                ),
            )
        }
    )
    result = evaluate_delivery(stale, now=FIXED_NOW)

    assert result.contract_fulfilled is True
    assert result.completion_eligible_now is False
    assert "observation_stale" in {item.code for item in result.blockers}


def test_changed_context_and_wrong_dependency_generation_block_delivery() -> None:
    from brain_v42.models.delivery import DependencyPredicate
    from brain_v42.models.delivery_evaluator import evaluate_delivery
    from tests.delivery_helpers import FIXED_NOW, delivery_inputs

    context_changed = delivery_inputs(context_status="changed")
    dependency = DependencyPredicate(
        ticket_id=UUID("00000000-0000-0000-0000-000000000040"),
        contract_revision=2,
        attempt=3,
        milestone="accepted",
        current_contract_revision=2,
        current_attempt=4,
        current_contract_digest="d" * 64,
        current_delivery_digest="e" * 64,
        current_disposition="active",
    )
    dependency_changed = delivery_inputs(
        dependencies=(dependency,),
        contract_dependencies=[
            {
                "ticket_id": str(dependency.ticket_id),
                "contract_revision": 2,
                "attempt": 3,
                "milestone": "accepted",
            }
        ],
    )

    context_result = evaluate_delivery(context_changed, now=FIXED_NOW)
    dependency_result = evaluate_delivery(dependency_changed, now=FIXED_NOW)

    assert "context_changed" in {item.code for item in context_result.blockers}
    assert "dependency_generation_mismatch" in {item.code for item in dependency_result.blockers}


def test_receipt_eligibility_is_independent_of_possessing_the_first_integration_receipt() -> None:
    from brain_v42.models.delivery_evaluator import evaluate_delivery
    from tests.delivery_helpers import FIXED_NOW, delivery_inputs

    result = evaluate_delivery(delivery_inputs(), now=FIXED_NOW)

    assert result.integration_receipt_eligible is True
    assert result.contract_fulfilled is False
    assert result.completion_eligible_now is False


def test_requested_completion_action_applies_the_correct_acceptance_gate() -> None:
    from brain_v42.models.delivery_evaluator import evaluate_delivery
    from tests.delivery_helpers import FIXED_NOW, delivery_inputs

    evidence = delivery_inputs(acceptance_mode="explicit")
    first = evaluate_delivery(evidence, now=FIXED_NOW)
    integrated = _receipt(evidence, first, "integration")
    base = evidence.model_copy(update={"integration_receipt": integrated})

    cross_resolve = evaluate_delivery(
        base.model_copy(update={"requested_completion_action": "cross_resolve"}), now=FIXED_NOW
    )
    cross_confirm = evaluate_delivery(
        base.model_copy(update={"requested_completion_action": "cross_confirm"}), now=FIXED_NOW
    )
    self_pending = evaluate_delivery(
        base.model_copy(
            update={"is_self_ticket": True, "requested_completion_action": "self_resolve_pending"}
        ),
        now=FIXED_NOW,
    )
    self_resolve = evaluate_delivery(
        base.model_copy(
            update={"is_self_ticket": True, "requested_completion_action": "self_resolve"}
        ),
        now=FIXED_NOW,
    )
    self_confirm = evaluate_delivery(
        base.model_copy(
            update={"is_self_ticket": True, "requested_completion_action": "self_confirm"}
        ),
        now=FIXED_NOW,
    )
    fulfilled = _receipt(base, first, "fulfilled")
    self_confirm_fulfilled = evaluate_delivery(
        base.model_copy(
            update={
                "is_self_ticket": True,
                "requested_completion_action": "self_confirm",
                "fulfillment_receipt": fulfilled,
            }
        ),
        now=FIXED_NOW,
    )

    assert cross_resolve.completion_eligible_now is True
    assert self_pending.completion_eligible_now is True
    assert cross_confirm.completion_eligible_now is False
    assert self_resolve.completion_eligible_now is False
    assert self_confirm.completion_eligible_now is False
    assert self_confirm_fulfilled.completion_eligible_now is True


def test_terminal_or_unsuccessful_disposition_exposes_no_claimable_work() -> None:
    from brain_v42.models.delivery_evaluator import evaluate_delivery
    from tests.delivery_helpers import FIXED_NOW, delivery_inputs

    result = evaluate_delivery(delivery_inputs(coordination_disposition="cancelled"), now=FIXED_NOW)

    assert result.eligible_work == ()
    assert "delivery_disposition_terminal" in {item.code for item in result.blockers}


def test_every_non_success_check_conclusion_is_a_distinct_blocker() -> None:
    from brain_v42.models.delivery_evaluator import evaluate_delivery
    from tests.delivery_helpers import FIXED_NOW, delivery_inputs

    expected = {
        "failure": "check_failed",
        "pending": "check_pending",
        "skipped": "check_skipped",
        "neutral": "check_neutral",
        "cancelled": "check_cancelled",
    }
    for conclusion, code in expected.items():
        result = evaluate_delivery(delivery_inputs(required_check=conclusion), now=FIXED_NOW)
        assert code in {item.code for item in result.blockers}


def test_stale_or_error_health_changes_current_eligibility_without_erasing_receipt() -> None:
    from brain_v42.models.delivery_evaluator import evaluate_delivery
    from tests.delivery_helpers import FIXED_NOW, delivery_inputs

    original = delivery_inputs(acceptance_mode="automatic")
    result = evaluate_delivery(original, now=FIXED_NOW)
    fulfilled = _receipt(original, result, "fulfilled")
    error = original.model_copy(
        update={
            "fulfillment_receipt": fulfilled,
            "active_bindings": (
                original.active_bindings[0].model_copy(
                    update={
                        "last_attempt_outcome": "error",
                        "latest_attempt_confirmation_id": UUID(
                            "00000000-0000-0000-0000-000000000206"
                        ),
                    }
                ),
            ),
        }
    )
    assessed = evaluate_delivery(error, now=FIXED_NOW)

    assert assessed.observation_health == "error"
    assert assessed.contract_fulfilled is True
    assert assessed.completion_eligible_now is False


def test_current_receipt_identity_is_superseded_by_a_new_evaluated_head() -> None:
    from brain_v42.models.delivery_evaluator import evaluate_delivery
    from tests.delivery_helpers import FIXED_NOW, delivery_inputs

    original = delivery_inputs(acceptance_mode="automatic")
    original_result = evaluate_delivery(original, now=FIXED_NOW)
    fulfilled = _receipt(original, original_result, "fulfilled")
    changed = _replace_evidence(
        original.model_copy(update={"fulfillment_receipt": fulfilled}), head_sha="f" * 40
    )
    assessed = evaluate_delivery(changed, now=FIXED_NOW)

    assert assessed.contract_fulfilled is False
    assert assessed.acceptance_state == "superseded"


def test_claimable_work_covers_repair_review_integration_and_accept_roles() -> None:
    from brain_v42.models.delivery_evaluator import evaluate_delivery
    from tests.delivery_helpers import FIXED_NOW, delivery_inputs

    repair = evaluate_delivery(delivery_inputs(required_check="failure"), now=FIXED_NOW)
    review = evaluate_delivery(
        delivery_inputs(
            merged=False,
            required_approvals=1,
            allowed_reviewers=["reviewer-project"],
            review="changes_requested",
        ),
        now=FIXED_NOW,
    )
    integration = evaluate_delivery(delivery_inputs(merged=False), now=FIXED_NOW)
    base = delivery_inputs(acceptance_mode="explicit")
    technical = evaluate_delivery(base, now=FIXED_NOW)
    accept = evaluate_delivery(
        base.model_copy(update={"integration_receipt": _receipt(base, technical, "integration")}),
        now=FIXED_NOW,
    )

    assert {(item.kind, item.role) for item in repair.eligible_work} == {("repair", "executor")}
    assert {(item.kind, item.role) for item in review.eligible_work} == {("review", "executor")}
    assert {(item.kind, item.role) for item in integration.eligible_work} == {
        ("integrate", "executor")
    }
    assert {(item.kind, item.role) for item in accept.eligible_work} == {("accept", "requester")}


def test_assessment_identity_is_stable_for_same_predicates_and_changes_at_freshness_boundary() -> (
    None
):
    from brain_v42.models.delivery_evaluator import evaluate_delivery
    from tests.delivery_helpers import FIXED_NOW, delivery_inputs

    inputs = delivery_inputs()
    first = evaluate_delivery(inputs, now=FIXED_NOW)
    same_predicates = evaluate_delivery(inputs, now=FIXED_NOW + timedelta(seconds=1))
    stale = evaluate_delivery(inputs, now=FIXED_NOW + timedelta(seconds=601))

    assert first.assessment_id == same_predicates.assessment_id
    assert stale.assessment_id != first.assessment_id
    assert stale.observation_health == "stale"
    assert first.observed_at == FIXED_NOW - timedelta(seconds=10)
    assert first.fresh_until == FIXED_NOW + timedelta(seconds=590)


def test_assessment_identity_is_stable_when_dependencies_arrive_in_a_different_order() -> None:
    from brain_v42.models.delivery import DependencyPredicate
    from brain_v42.models.delivery_evaluator import evaluate_delivery
    from tests.delivery_helpers import FIXED_NOW, delivery_inputs

    shared_ticket = UUID("00000000-0000-0000-0000-000000000040")
    base_inputs = delivery_inputs()
    inputs = delivery_inputs(
        contract_dependencies=[
            {
                "ticket_id": str(shared_ticket),
                "contract_revision": 1,
                "attempt": 1,
                "milestone": "integrated",
            },
            {
                "ticket_id": str(shared_ticket),
                "contract_revision": 1,
                "attempt": 1,
                "milestone": "accepted",
            },
        ]
    )
    upstream_assessment = evaluate_delivery(base_inputs, now=FIXED_NOW)
    contract_digest = base_inputs.contract.content_digest
    assert contract_digest is not None
    first_receipt = _receipt(
        base_inputs,
        upstream_assessment,
        "integration",
        ticket_id=shared_ticket,
        contract_revision=1,
        attempt=1,
        contract_digest=contract_digest,
    ).model_copy(update={"id": UUID("00000000-0000-0000-0000-000000000041")})
    second_receipt = _receipt(
        base_inputs,
        upstream_assessment,
        "fulfilled",
        ticket_id=shared_ticket,
        contract_revision=1,
        attempt=1,
        contract_digest=contract_digest,
    ).model_copy(update={"id": UUID("00000000-0000-0000-0000-000000000042")})
    first_dependency = DependencyPredicate(
        ticket_id=shared_ticket,
        contract_revision=1,
        attempt=1,
        milestone="integrated",
        current_contract_revision=1,
        current_attempt=1,
        current_contract_digest=contract_digest,
        current_delivery_digest=upstream_assessment.delivery_digest,
        current_disposition="active",
        receipt=first_receipt,
    )
    second_dependency = DependencyPredicate(
        ticket_id=shared_ticket,
        contract_revision=1,
        attempt=1,
        milestone="accepted",
        current_contract_revision=1,
        current_attempt=1,
        current_contract_digest=contract_digest,
        current_delivery_digest=upstream_assessment.delivery_digest,
        current_disposition="active",
        receipt=second_receipt,
    )
    inputs = inputs.model_copy(update={"dependencies": (first_dependency, second_dependency)})

    forward = evaluate_delivery(inputs, now=FIXED_NOW)
    reversed_order = evaluate_delivery(
        inputs.model_copy(update={"dependencies": (second_dependency, first_dependency)}),
        now=FIXED_NOW,
    )

    assert forward.requirements_satisfied is True
    assert forward.blockers == ()
    assert reversed_order.requirements_satisfied is True
    assert reversed_order.blockers == ()
    assert forward.assessment_id == reversed_order.assessment_id


def test_receipt_ticket_identity_must_match_its_own_workflow() -> None:
    from brain_v42.models.delivery_evaluator import evaluate_delivery
    from tests.delivery_helpers import FIXED_NOW, delivery_inputs

    inputs = delivery_inputs()
    initial = evaluate_delivery(inputs, now=FIXED_NOW)
    receipt = _receipt(
        inputs,
        initial,
        "integration",
    )

    matching = evaluate_delivery(
        inputs.model_copy(update={"integration_receipt": receipt}), now=FIXED_NOW
    )
    mismatched = evaluate_delivery(
        inputs.model_copy(
            update={"integration_receipt": receipt.model_copy(update={"ticket_id": UUID(int=99)})}
        ),
        now=FIXED_NOW,
    )

    assert matching.completion_eligible_now is False
    assert mismatched.acceptance_state == "superseded"


def test_dependency_receipt_is_superseded_when_current_delivery_digest_changes() -> None:
    from brain_v42.models.delivery import DependencyPredicate
    from brain_v42.models.delivery_evaluator import evaluate_delivery
    from tests.delivery_helpers import FIXED_NOW, delivery_inputs

    source_inputs = delivery_inputs()
    source_result = evaluate_delivery(source_inputs, now=FIXED_NOW)
    receipt = _receipt(
        source_inputs,
        source_result,
        "fulfilled",
        ticket_id=UUID("00000000-0000-0000-0000-000000000040"),
        contract_revision=2,
        attempt=3,
        contract_digest="d" * 64,
        delivery_digest="e" * 64,
    )
    dependency = DependencyPredicate(
        ticket_id=receipt.ticket_id,
        contract_revision=2,
        attempt=3,
        milestone="accepted",
        current_contract_revision=2,
        current_attempt=3,
        current_contract_digest="d" * 64,
        current_delivery_digest="f" * 64,
        current_disposition="active",
        receipt=receipt,
    )

    result = evaluate_delivery(
        delivery_inputs(
            dependencies=(dependency,),
            contract_dependencies=[
                {
                    "ticket_id": str(dependency.ticket_id),
                    "contract_revision": 2,
                    "attempt": 3,
                    "milestone": "accepted",
                }
            ],
        ),
        now=FIXED_NOW,
    )

    assert "dependency_receipt_mismatch" in {item.code for item in result.blockers}


def test_contract_required_context_and_dependency_cannot_be_omitted_from_predicates() -> None:
    from brain_v42.models.delivery import ContractRevision
    from brain_v42.models.delivery_evaluator import evaluate_delivery
    from tests.delivery_helpers import FIXED_NOW, delivery_inputs, stored_contract_payload

    stored = stored_contract_payload()
    stored["context_refs"] = [
        {
            "kind": "brain_entity",
            "entity_type": "adr",
            "entity_id": "00000000-0000-0000-0000-000000000050",
            "content_snapshot": "Pinned architecture",
            "content_digest": "d" * 64,
        }
    ]
    stored["dependencies"] = [
        {
            "ticket_id": "00000000-0000-0000-0000-000000000051",
            "contract_revision": 2,
            "attempt": 3,
            "milestone": "integrated",
        }
    ]
    result = evaluate_delivery(
        delivery_inputs(
            contract=ContractRevision.model_validate(stored), contexts=(), dependencies=()
        ),
        now=FIXED_NOW,
    )

    assert result.integration_receipt_eligible is False
    assert {item.code for item in result.blockers} >= {
        "context_predicate_missing",
        "dependency_predicate_missing",
    }


def test_pr_author_approval_and_commented_review_cannot_bypass_effective_review() -> None:
    from brain_v42.models.delivery import ReviewEvidence
    from brain_v42.models.delivery_evaluator import evaluate_delivery
    from tests.delivery_helpers import FIXED_NOW, delivery_inputs

    author = ReviewEvidence(
        record_id=9010,
        provider_id=91,
        reviewer="github-author",
        head_sha="a" * 40,
        decision="approved",
        submitted_at=FIXED_NOW - timedelta(seconds=30),
    )
    approved = ReviewEvidence(
        record_id=9011,
        provider_id=92,
        reviewer="reviewer-project",
        head_sha="a" * 40,
        decision="approved",
        submitted_at=FIXED_NOW - timedelta(seconds=20),
    )
    comment = ReviewEvidence(
        record_id=9012,
        provider_id=92,
        reviewer="reviewer-project",
        head_sha="a" * 40,
        decision="commented",
        submitted_at=FIXED_NOW - timedelta(seconds=10),
    )
    result = evaluate_delivery(
        delivery_inputs(
            merged=False,
            author_id="github-author",
            required_approvals=2,
            allowed_reviewers=["github-author", "reviewer-project"],
            reviews=(author, approved, comment),
        ),
        now=FIXED_NOW,
    )

    assert "review_approval_missing" in {item.code for item in result.blockers}


def test_confirmation_refresh_keeps_snapshot_fresh_and_delivery_digest_stable() -> None:
    from brain_v42.models.delivery_evaluator import evaluate_delivery
    from tests.delivery_helpers import FIXED_NOW, delivery_inputs

    first_input = delivery_inputs(acceptance_mode="automatic")
    first = evaluate_delivery(first_input, now=FIXED_NOW)
    fulfilled = _receipt(first_input, first, "fulfilled")
    binding = first_input.active_bindings[0].binding.model_copy(update={"binding_version": 2})
    confirmation = first_input.active_bindings[0].confirmation.model_copy(
        update={
            "id": UUID("00000000-0000-0000-0000-000000000021"),
            "collection_finished_at": FIXED_NOW,
        }
    )
    refreshed = first_input.model_copy(
        update={
            "fulfillment_receipt": fulfilled,
            "active_bindings": (
                first_input.active_bindings[0].model_copy(
                    update={"binding": binding, "confirmation": confirmation}
                ),
            ),
        }
    )
    result = evaluate_delivery(refreshed, now=FIXED_NOW)

    assert result.observation_health == "fresh"
    assert result.delivery_digest == first.delivery_digest
    assert result.contract_fulfilled is True
    assert result.assessment_id != first.assessment_id


def test_newest_real_provider_record_and_proven_synthetic_merge_are_selected() -> None:
    from brain_v42.models.delivery import CheckAttempt, SyntheticMergeAssociation
    from brain_v42.models.delivery_evaluator import evaluate_delivery
    from tests.delivery_helpers import FIXED_NOW, delivery_inputs

    old = CheckAttempt(
        record_id=100,
        provider_id=77,
        kind="check_run",
        name="test-unit",
        app_slug="github-actions",
        head_sha="a" * 40,
        conclusion="success",
        started_at=FIXED_NOW - timedelta(seconds=30),
        completed_at=FIXED_NOW - timedelta(seconds=20),
    )
    pending = CheckAttempt(
        record_id=101,
        provider_id=77,
        kind="check_run",
        name="test-unit",
        app_slug="github-actions",
        head_sha="a" * 40,
        conclusion="pending",
        started_at=None,
        completed_at=None,
    )
    synthetic = CheckAttempt(
        record_id=102,
        provider_id=77,
        kind="check_run",
        name="test-unit",
        app_slug="github-actions",
        head_sha="c" * 40,
        conclusion="success",
        started_at=FIXED_NOW - timedelta(seconds=5),
        completed_at=FIXED_NOW,
    )
    association = SyntheticMergeAssociation(
        synthetic_sha="c" * 40, head_sha="a" * 40, base_sha="b" * 40
    )

    pending_result = evaluate_delivery(delivery_inputs(checks=(old, pending)), now=FIXED_NOW)
    synthetic_result = evaluate_delivery(
        delivery_inputs(checks=(synthetic,), synthetic_merges=(association,)), now=FIXED_NOW
    )

    assert "check_pending" in {item.code for item in pending_result.blockers}
    assert "check_missing" not in {item.code for item in synthetic_result.blockers}


def test_invalid_action_terminal_status_claim_and_global_blockers_prevent_acquisition() -> None:
    from brain_v42.models.delivery import ClaimState
    from brain_v42.models.delivery_evaluator import evaluate_delivery
    from tests.delivery_helpers import FIXED_NOW, delivery_inputs

    terminal = evaluate_delivery(delivery_inputs(coordination_status="closed"), now=FIXED_NOW)
    action = evaluate_delivery(
        delivery_inputs(is_self_ticket=True, requested_completion_action="cross_resolve"),
        now=FIXED_NOW,
    )
    claimed = evaluate_delivery(
        delivery_inputs(
            active_bindings=(),
            claim=ClaimState(epoch=1, owner="owner", expires_at=FIXED_NOW + timedelta(seconds=30)),
        ),
        now=FIXED_NOW,
    )
    blocked = evaluate_delivery(
        delivery_inputs(active_bindings=(), context_status="changed"), now=FIXED_NOW
    )

    assert terminal.integration_receipt_eligible is False
    assert terminal.eligible_work == ()
    assert "completion_action_invalid" in {item.code for item in action.blockers}
    assert claimed.eligible_work == ()
    assert blocked.eligible_work == ()


def test_claim_expiry_and_integration_sha_change_assessment_or_delivery_identity() -> None:
    from brain_v42.models.delivery import ClaimState
    from brain_v42.models.delivery_evaluator import evaluate_delivery
    from tests.delivery_helpers import FIXED_NOW, delivery_inputs

    inputs = delivery_inputs()
    short = evaluate_delivery(
        inputs.model_copy(
            update={
                "claim": ClaimState(
                    epoch=1, owner="owner", expires_at=FIXED_NOW + timedelta(seconds=10)
                )
            }
        ),
        now=FIXED_NOW,
    )
    long = evaluate_delivery(
        inputs.model_copy(
            update={
                "claim": ClaimState(
                    epoch=1, owner="owner", expires_at=FIXED_NOW + timedelta(seconds=500)
                )
            }
        ),
        now=FIXED_NOW,
    )
    changed = evaluate_delivery(
        _replace_evidence(inputs, integration_sha="f" * 40, integration_revision=None),
        now=FIXED_NOW,
    )

    assert short.assessment_id != long.assessment_id
    assert changed.delivery_digest != evaluate_delivery(inputs, now=FIXED_NOW).delivery_digest


def test_binding_from_an_old_attempt_cannot_satisfy_current_contract() -> None:
    from brain_v42.models.delivery_evaluator import evaluate_delivery
    from tests.delivery_helpers import FIXED_NOW, delivery_inputs

    inputs = delivery_inputs()
    old_binding = inputs.active_bindings[0].binding.model_copy(update={"attempt": 2})
    result = evaluate_delivery(
        inputs.model_copy(
            update={
                "active_bindings": (
                    inputs.active_bindings[0].model_copy(update={"binding": old_binding}),
                )
            }
        ),
        now=FIXED_NOW,
    )

    assert result.integration_receipt_eligible is False
    assert "binding_identity_invalid" in {item.code for item in result.blockers}


def test_provider_collections_allow_two_thousand_records_but_reject_more() -> None:
    import pytest

    from brain_v42.models.delivery import PullRequestEvidence
    from tests.delivery_helpers import delivery_inputs

    evidence = delivery_inputs().active_bindings[0].confirmation.evidence
    accepted = PullRequestEvidence.model_validate(
        {**evidence.model_dump(), "checks": [evidence.checks[0].model_dump()] * 2000}
    )

    assert len(accepted.checks) == 2000
    with pytest.raises(ValueError):
        PullRequestEvidence.model_validate(
            {**evidence.model_dump(), "checks": [evidence.checks[0].model_dump()] * 2001}
        )


def test_equal_review_timestamps_use_record_id_independent_of_input_order() -> None:
    from brain_v42.models.delivery import ReviewEvidence
    from brain_v42.models.delivery_evaluator import evaluate_delivery
    from tests.delivery_helpers import FIXED_NOW, delivery_inputs

    changed = ReviewEvidence(
        record_id=100,
        provider_id=99,
        reviewer="reviewer-project",
        head_sha="a" * 40,
        decision="changes_requested",
        submitted_at=FIXED_NOW,
    )
    approved = ReviewEvidence(
        record_id=101,
        provider_id=1,
        reviewer="reviewer-project",
        head_sha="a" * 40,
        decision="approved",
        submitted_at=FIXED_NOW,
    )

    forward = evaluate_delivery(
        delivery_inputs(
            merged=False,
            required_approvals=1,
            allowed_reviewers=["reviewer-project"],
            reviews=(changed, approved),
        ),
        now=FIXED_NOW,
    )
    reverse = evaluate_delivery(
        delivery_inputs(
            merged=False,
            required_approvals=1,
            allowed_reviewers=["reviewer-project"],
            reviews=(approved, changed),
        ),
        now=FIXED_NOW,
    )

    assert "review_changes_requested" not in {item.code for item in forward.blockers}
    assert "review_changes_requested" not in {item.code for item in reverse.blockers}
    assert "review_approval_missing" not in {item.code for item in forward.blockers}
    assert "review_approval_missing" not in {item.code for item in reverse.blockers}
