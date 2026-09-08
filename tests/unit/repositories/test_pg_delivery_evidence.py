"""Unit behavior for immutable delivery-receipt proof construction."""

from uuid import UUID


class _SnapshotSession:
    async def scalar(self, _statement: object) -> str:
        return "f" * 64


async def test_frozen_receipt_proof_is_stable_when_dependencies_are_reversed() -> None:
    from brain_v42.models.delivery import (
        DependencyPredicate,
        ReceiptIssuerProvenance,
    )
    from brain_v42.models.delivery_evaluator import evaluate_delivery
    from brain_v42.repositories.pg_delivery_evidence import PgDeliveryEvidenceRepo
    from tests.delivery_helpers import FIXED_NOW, delivery_inputs
    from tests.unit.models.test_delivery_evaluator import _receipt

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
    assessment = evaluate_delivery(base_inputs, now=FIXED_NOW)
    first_receipt = _receipt(
        base_inputs,
        assessment,
        "integration",
        ticket_id=shared_ticket,
        contract_revision=1,
        attempt=1,
        contract_digest=base_inputs.contract.content_digest,
        delivery_digest=assessment.delivery_digest,
    ).model_copy(update={"id": UUID("00000000-0000-0000-0000-000000000041")})
    second_receipt = _receipt(
        base_inputs,
        assessment,
        "fulfilled",
        ticket_id=shared_ticket,
        contract_revision=1,
        attempt=1,
        contract_digest=base_inputs.contract.content_digest,
        delivery_digest=assessment.delivery_digest,
    ).model_copy(update={"id": UUID("00000000-0000-0000-0000-000000000042")})
    first_dependency = DependencyPredicate(
        ticket_id=shared_ticket,
        contract_revision=1,
        attempt=1,
        milestone="integrated",
        current_contract_revision=1,
        current_attempt=1,
        current_contract_digest=base_inputs.contract.content_digest,
        current_delivery_digest=assessment.delivery_digest,
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
        current_contract_digest=base_inputs.contract.content_digest,
        current_delivery_digest=assessment.delivery_digest,
        current_disposition="active",
        receipt=second_receipt,
    )
    confirmation = inputs.active_bindings[0].confirmation
    assert confirmation is not None
    binding = inputs.active_bindings[0].model_copy(
        update={
            "snapshot_id": UUID("00000000-0000-0000-0000-000000000021"),
            "success_confirmation_id": confirmation.id,
            "latest_attempt_confirmation_id": confirmation.id,
        }
    )
    inputs = inputs.model_copy(
        update={
            "active_bindings": (binding,),
            "dependencies": (first_dependency, second_dependency),
        }
    )
    repository = PgDeliveryEvidenceRepo()
    issuer = ReceiptIssuerProvenance(
        issuer_project="brain-v42",
        issuer_identity="delivery-observer",
        issuer_kind="observer",
    )
    evaluated = evaluate_delivery(inputs, now=FIXED_NOW)

    assert evaluated.requirements_satisfied is True
    assert evaluated.blockers == ()

    forward = await repository._frozen_receipt_proof(
        _SnapshotSession(),
        inputs=inputs,
        assessment_id="a" * 64,
        delivery_digest="b" * 64,
        decision_time=FIXED_NOW,
        acceptance_basis=None,
        issuer=issuer,
    )
    reversed_order = await repository._frozen_receipt_proof(
        _SnapshotSession(),
        inputs=inputs.model_copy(update={"dependencies": (second_dependency, first_dependency)}),
        assessment_id="a" * 64,
        delivery_digest="b" * 64,
        decision_time=FIXED_NOW,
        acceptance_basis=None,
        issuer=issuer,
    )

    assert forward.upstream_receipt_ids == reversed_order.upstream_receipt_ids
