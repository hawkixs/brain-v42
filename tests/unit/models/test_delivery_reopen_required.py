"""Resolved delivery amendments require the canonical requester reopen path."""

from __future__ import annotations

from brain_v42.models.delivery_evaluator import evaluate_delivery
from tests.delivery_helpers import FIXED_NOW, delivery_inputs
from tests.unit.models.test_delivery_evaluator import _receipt


def test_resolved_amendment_requires_reopen_before_executor_work() -> None:
    """A superseded resolved delivery cannot offer new executor work until reopened."""
    original = delivery_inputs()
    integrated = _receipt(original, evaluate_delivery(original, now=FIXED_NOW), "integration")
    amended = original.model_copy(
        update={
            "contract": original.contract.model_copy(
                update={"contract_revision": 2, "amendment_reason": "new implementation"}
            ),
            "workflow_version": 2,
            "coordination_status": "resolved",
            "active_bindings": (),
            "integration_receipt": integrated,
        }
    )

    assessment = evaluate_delivery(amended, now=FIXED_NOW)

    assert "reopen_required" in {finding.code for finding in assessment.blockers}
    assert assessment.eligible_work == ()


def test_resolved_current_integration_still_offers_explicit_acceptance() -> None:
    """A valid resolved integration remains actionable by the requester, not reopened."""
    inputs = delivery_inputs()
    integrated = _receipt(inputs, evaluate_delivery(inputs, now=FIXED_NOW), "integration")

    assessment = evaluate_delivery(
        inputs.model_copy(
            update={"coordination_status": "resolved", "integration_receipt": integrated}
        ),
        now=FIXED_NOW,
    )

    assert "reopen_required" not in {finding.code for finding in assessment.blockers}
    assert {work.kind for work in assessment.eligible_work} == {"accept"}
