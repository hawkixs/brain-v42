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


def _receipt(inputs: Any, result: Any, milestone: Literal["integration", "fulfilled"]) -> Any:
    from brain_v42.models.delivery import MilestoneReceipt
    from tests.delivery_helpers import FIXED_NOW

    return MilestoneReceipt(
        id=UUID(
            "00000000-0000-0000-0000-000000000030"
            if milestone == "integration"
            else "00000000-0000-0000-0000-000000000031"
        ),
        ticket_id=inputs.contract.ticket_id,
        milestone=milestone,
        contract_revision=inputs.contract.contract_revision,
        attempt=inputs.attempt,
        contract_digest=inputs.contract.content_digest,
        delivery_digest=result.delivery_digest,
        issued_at=FIXED_NOW,
        acceptance_basis="automatic" if milestone == "fulfilled" else None,
    )


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
        provider_id=8001,
        kind="check_run",
        name="test-unit",
        app_slug="github-actions",
        head_sha="a" * 40,
        conclusion="success",
        run_attempt=1,
        completed_at=FIXED_NOW - timedelta(seconds=40),
    )
    rerun = CheckAttempt(
        provider_id=8002,
        kind="check_run",
        name="test-unit",
        app_slug="github-actions",
        head_sha="a" * 40,
        conclusion="pending",
        run_attempt=2,
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
        provider_id=9010,
        reviewer="executor-project",
        head_sha="a" * 40,
        decision="approved",
        submitted_at=FIXED_NOW - timedelta(seconds=20),
    )
    changed = ReviewEvidence(
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
    dependency_changed = delivery_inputs(dependencies=(dependency,))

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

    assert cross_resolve.completion_eligible_now is True
    assert self_pending.completion_eligible_now is True
    assert cross_confirm.completion_eligible_now is False
    assert self_resolve.completion_eligible_now is False


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
                original.active_bindings[0].model_copy(update={"last_attempt_outcome": "error"}),
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


def test_receipt_ticket_identity_must_match_its_own_workflow() -> None:
    from brain_v42.models.delivery import MilestoneReceipt
    from brain_v42.models.delivery_evaluator import evaluate_delivery
    from tests.delivery_helpers import FIXED_NOW, delivery_inputs

    inputs = delivery_inputs()
    initial = evaluate_delivery(inputs, now=FIXED_NOW)
    receipt = MilestoneReceipt(
        id=UUID("00000000-0000-0000-0000-000000000032"),
        ticket_id=inputs.contract.ticket_id,
        milestone="integration",
        contract_revision=inputs.contract.contract_revision,
        attempt=inputs.attempt,
        contract_digest=inputs.contract.content_digest,
        delivery_digest=initial.delivery_digest,
        issued_at=FIXED_NOW,
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
    from brain_v42.models.delivery import DependencyPredicate, MilestoneReceipt
    from brain_v42.models.delivery_evaluator import evaluate_delivery
    from tests.delivery_helpers import FIXED_NOW, delivery_inputs

    receipt = MilestoneReceipt(
        id=UUID("00000000-0000-0000-0000-000000000033"),
        ticket_id=UUID("00000000-0000-0000-0000-000000000040"),
        milestone="fulfilled",
        contract_revision=2,
        attempt=3,
        contract_digest="d" * 64,
        delivery_digest="e" * 64,
        issued_at=FIXED_NOW,
        acceptance_basis="automatic",
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

    result = evaluate_delivery(delivery_inputs(dependencies=(dependency,)), now=FIXED_NOW)

    assert "dependency_receipt_mismatch" in {item.code for item in result.blockers}
