"""Strict, frozen boundary contracts for Task 4B1a receipt proofs."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from pydantic import ValidationError

_DECISION_TIME = datetime(2026, 9, 7, 12, 0, 2, tzinfo=UTC)


def _proof_payload() -> dict[str, object]:
    started = _DECISION_TIME - timedelta(seconds=2)
    finished = _DECISION_TIME - timedelta(seconds=1)
    return {
        "ticket_id": UUID("00000000-0000-0000-0000-000000000110"),
        "contract_revision": 1,
        "contract_digest": "c" * 64,
        "attempt": 1,
        "workflow_version": 3,
        "delivery_digest": "d" * 64,
        "assessment_id": "e" * 64,
        "decision_time": _DECISION_TIME,
        "artifact_proofs": (
            {
                "binding_id": UUID("00000000-0000-0000-0000-000000000101"),
                "binding_version": 2,
                "deliverable_key": "implementation",
                "repository_id": 1337360966,
                "pr_number": 42,
                "head_sha": "a" * 40,
                "base_sha": "b" * 40,
                "integration_sha": "c" * 40,
                "integration_revision": None,
                "snapshot_id": UUID("00000000-0000-0000-0000-000000000102"),
                "snapshot_digest": "f" * 64,
                "success_confirmation_id": UUID("00000000-0000-0000-0000-000000000103"),
                "latest_attempt_confirmation_id": UUID("00000000-0000-0000-0000-000000000103"),
                "collection_started_at": started,
                "collection_finished_at": finished,
            },
        ),
        "brain_context_proofs": (
            {
                "reference_identity": "brain_entity:decision:00000000-0000-0000-0000-000000000111",
                "pinned_digest": "1" * 64,
                "current_digest": "1" * 64,
            },
        ),
        "repository_context_proofs": (
            {
                "reference_identity": "repository_document:1337360966:" + "a" * 40 + ":README.md",
                "required": False,
                "expected_digest": "2" * 64,
                "current_digest": "2" * 64,
                "snapshot_id": UUID("00000000-0000-0000-0000-000000000105"),
                "snapshot_digest": "3" * 64,
                "success_confirmation_id": UUID("00000000-0000-0000-0000-000000000106"),
                "latest_attempt_confirmation_id": UUID("00000000-0000-0000-0000-000000000106"),
                "collection_started_at": started,
                "collection_finished_at": finished,
            },
        ),
        "upstream_receipt_ids": (UUID("00000000-0000-0000-0000-000000000108"),),
        "issuer": {
            "issuer_project": "brain-v42",
            "issuer_identity": "delivery-observer",
            "issuer_kind": "observer",
        },
        "acceptance_basis": None,
        "explicit_acceptance": None,
    }


def _receipt_payload() -> dict[str, object]:
    proof = _proof_payload()
    return {
        "id": UUID("00000000-0000-0000-0000-000000000109"),
        "ticket_id": proof["ticket_id"],
        "milestone": "integration",
        "contract_revision": proof["contract_revision"],
        "attempt": proof["attempt"],
        "contract_digest": proof["contract_digest"],
        "delivery_digest": proof["delivery_digest"],
        "issued_at": proof["decision_time"],
        "acceptance_basis": None,
        "explicit_acceptance": None,
        "proof": proof,
    }


def _delivery_module():
    from brain_v42.models import delivery

    assert hasattr(delivery, "FrozenReceiptProof"), "Task 4B1a must expose FrozenReceiptProof"
    return delivery


def test_receipt_requires_complete_exact_frozen_audit_proof() -> None:
    delivery = _delivery_module()

    receipt = delivery.MilestoneReceipt.model_validate(_receipt_payload())

    assert receipt.issued_at == receipt.proof.decision_time
    assert receipt.ticket_id == receipt.proof.ticket_id
    assert receipt.proof.artifact_proofs[0].integration_revision is None
    assert receipt.proof.artifact_proofs[0].success_confirmation_id == (
        receipt.proof.artifact_proofs[0].latest_attempt_confirmation_id
    )
    assert receipt.proof.repository_context_proofs[0].required is False
    assert receipt.proof.brain_context_proofs[0].current_digest == "1" * 64


def test_receipt_round_trips_immutable_proof_through_json_storage() -> None:
    delivery = _delivery_module()
    receipt = delivery.MilestoneReceipt.model_validate(_receipt_payload())

    restored = delivery.MilestoneReceipt.model_validate_json(receipt.model_dump_json())

    assert restored == receipt


def test_receipt_preserves_integration_revision_when_sha_is_not_observed() -> None:
    delivery = _delivery_module()
    payload = _receipt_payload()
    proof = dict(payload["proof"])
    artifact = dict(proof["artifact_proofs"][0])
    artifact.update(integration_sha=None, integration_revision="c" * 40)
    proof["artifact_proofs"] = (artifact,)
    payload["proof"] = proof

    receipt = delivery.MilestoneReceipt.model_validate(payload)

    assert receipt.proof.artifact_proofs[0].integration_sha is None
    assert receipt.proof.artifact_proofs[0].integration_revision == "c" * 40


def test_receipt_rejects_artifact_without_integration_sha_or_revision() -> None:
    delivery = _delivery_module()
    payload = _receipt_payload()
    proof = dict(payload["proof"])
    artifact = dict(proof["artifact_proofs"][0])
    artifact.update(integration_sha=None, integration_revision=None)
    proof["artifact_proofs"] = (artifact,)
    payload["proof"] = proof

    with pytest.raises(ValidationError):
        delivery.MilestoneReceipt.model_validate(payload)


@pytest.mark.parametrize("field", ("integration_sha", "integration_revision"))
def test_receipt_requires_each_nullable_integration_key(field: str) -> None:
    delivery = _delivery_module()
    payload = _receipt_payload()
    proof = dict(payload["proof"])
    artifact = dict(proof["artifact_proofs"][0])
    artifact["integration_sha"] = "c" * 40
    artifact["integration_revision"] = "d" * 40
    artifact.pop(field)
    proof["artifact_proofs"] = (artifact,)
    payload["proof"] = proof

    with pytest.raises(ValidationError):
        delivery.MilestoneReceipt.model_validate(payload)


@pytest.mark.parametrize(
    ("part", "field"),
    (
        ("old_receipt", "proof"),
        ("proof", "ticket_id"),
        ("artifact", "snapshot_id"),
        ("repository_context", "success_confirmation_id"),
        ("brain_context", "pinned_digest"),
        ("proof", "issuer"),
    ),
)
def test_receipt_rejects_old_minimal_or_missing_mandatory_proof_part(part: str, field: str) -> None:
    delivery = _delivery_module()
    if part == "old_receipt":
        payload = {
            key: value
            for key, value in _receipt_payload().items()
            if key
            in {
                "id",
                "ticket_id",
                "milestone",
                "contract_revision",
                "attempt",
                "contract_digest",
                "delivery_digest",
                "issued_at",
                "acceptance_basis",
            }
        }
        with pytest.raises(ValidationError):
            delivery.MilestoneReceipt.model_validate(payload)
        return
    payload = _receipt_payload()
    if part != "old_receipt":
        proof = dict(payload["proof"])
        if part == "proof":
            proof.pop(field)
        else:
            field_name = f"{part}_proofs"
            item = dict(proof[field_name][0])
            item.pop(field)
            proof[field_name] = (item,)
        payload["proof"] = proof

    with pytest.raises(ValidationError):
        delivery.MilestoneReceipt.model_validate(payload)


@pytest.mark.parametrize("case", ("naive", "reversed", "future"))
def test_receipt_rejects_naive_reversed_or_post_decision_collection_interval(
    case: str,
) -> None:
    delivery = _delivery_module()
    payload = _receipt_payload()
    proof = dict(payload["proof"])
    artifact = dict(proof["artifact_proofs"][0])
    if case == "naive":
        artifact["collection_started_at"] = artifact["collection_started_at"].replace(tzinfo=None)
    elif case == "reversed":
        artifact["collection_finished_at"] = artifact["collection_started_at"] - timedelta(
            seconds=1
        )
    else:
        artifact["collection_finished_at"] = _DECISION_TIME + timedelta(seconds=1)
    proof["artifact_proofs"] = (artifact,)
    payload["proof"] = proof

    with pytest.raises(ValidationError):
        delivery.MilestoneReceipt.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "replacement"),
    (
        ("ticket_id", UUID("00000000-0000-0000-0000-000000000199")),
        ("contract_revision", 2),
        ("attempt", 2),
        ("contract_digest", "8" * 64),
        ("delivery_digest", "9" * 64),
        ("issued_at", _DECISION_TIME + timedelta(seconds=1)),
    ),
)
def test_receipt_rejects_identity_or_decision_time_mismatch(
    field: str, replacement: object
) -> None:
    delivery = _delivery_module()
    payload = _receipt_payload()
    if field == "issued_at":
        payload[field] = replacement
    else:
        proof = dict(payload["proof"])
        proof[field] = replacement
        payload["proof"] = proof

    with pytest.raises(ValidationError):
        delivery.MilestoneReceipt.model_validate(payload)


def test_receipt_rejects_duplicate_frozen_binding_identity() -> None:
    delivery = _delivery_module()
    payload = _receipt_payload()
    proof = dict(payload["proof"])
    proof["artifact_proofs"] = proof["artifact_proofs"] * 2
    payload["proof"] = proof

    with pytest.raises(ValidationError):
        delivery.MilestoneReceipt.model_validate(payload)


def test_receipt_rejects_duplicate_frozen_upstream_receipt_identity() -> None:
    delivery = _delivery_module()
    payload = _receipt_payload()
    proof = dict(payload["proof"])
    proof["upstream_receipt_ids"] = proof["upstream_receipt_ids"] * 2
    payload["proof"] = proof

    with pytest.raises(ValidationError):
        delivery.MilestoneReceipt.model_validate(payload)


def test_receipt_is_immutable() -> None:
    delivery = _delivery_module()
    receipt = delivery.MilestoneReceipt.model_validate(_receipt_payload())

    with pytest.raises(ValidationError):
        receipt.delivery_digest = "9" * 64


def test_nested_receipt_proof_is_immutable() -> None:
    delivery = _delivery_module()
    receipt = delivery.MilestoneReceipt.model_validate(_receipt_payload())

    with pytest.raises(ValidationError):
        receipt.proof.contract_digest = "9" * 64


def test_nested_artifact_receipt_proof_is_immutable() -> None:
    delivery = _delivery_module()
    receipt = delivery.MilestoneReceipt.model_validate(_receipt_payload())

    with pytest.raises(ValidationError):
        receipt.proof.artifact_proofs[0].head_sha = "9" * 40


def test_explicit_fulfillment_requires_requester_and_nonblank_rationale() -> None:
    delivery = _delivery_module()
    payload = _receipt_payload()
    proof = dict(payload["proof"])
    proof["acceptance_basis"] = "explicit"
    proof["issuer"] = {
        "issuer_project": "requester-project",
        "issuer_identity": "requester-project",
        "issuer_kind": "requester",
    }
    proof["explicit_acceptance"] = {
        "requester_project": "requester-project",
        "rationale": "accepted",
    }
    payload.update(
        milestone="fulfilled",
        acceptance_basis="explicit",
        explicit_acceptance=proof["explicit_acceptance"],
        proof=proof,
    )

    explicit = delivery.MilestoneReceipt.model_validate(payload)
    assert explicit.proof.explicit_acceptance.requester_project == "requester-project"

    proof["explicit_acceptance"] = {
        "requester_project": "requester-project",
        "rationale": "   ",
    }
    payload["explicit_acceptance"] = proof["explicit_acceptance"]
    payload["proof"] = proof
    with pytest.raises(ValidationError):
        delivery.MilestoneReceipt.model_validate(payload)


def test_explicit_fulfillment_rejects_observer_or_mismatched_requester_provenance() -> None:
    delivery = _delivery_module()
    payload = _receipt_payload()
    proof = dict(payload["proof"])
    proof.update(
        acceptance_basis="explicit",
        explicit_acceptance={"requester_project": "requester-project", "rationale": "accepted"},
    )
    payload.update(
        milestone="fulfilled",
        acceptance_basis="explicit",
        explicit_acceptance=proof["explicit_acceptance"],
        proof=proof,
    )
    with pytest.raises(ValidationError):
        delivery.MilestoneReceipt.model_validate(payload)

    proof["issuer"] = {
        "issuer_project": "another-requester",
        "issuer_identity": "another-requester",
        "issuer_kind": "requester",
    }
    payload["proof"] = proof
    with pytest.raises(ValidationError):
        delivery.MilestoneReceipt.model_validate(payload)


def test_automatic_fulfillment_rejects_explicit_decision_or_nonobserver_issuer() -> None:
    delivery = _delivery_module()
    payload = _receipt_payload()
    proof = dict(payload["proof"])
    proof["acceptance_basis"] = "automatic"
    proof["explicit_acceptance"] = {"requester_project": "brain-v42", "rationale": "forbidden"}
    payload.update(
        milestone="fulfilled",
        acceptance_basis="automatic",
        explicit_acceptance=proof["explicit_acceptance"],
        proof=proof,
    )
    with pytest.raises(ValidationError):
        delivery.MilestoneReceipt.model_validate(payload)

    proof["explicit_acceptance"] = None
    proof["issuer"] = {
        "issuer_project": "requester-project",
        "issuer_identity": "requester-project",
        "issuer_kind": "requester",
    }
    payload.update(explicit_acceptance=None, proof=proof)
    with pytest.raises(ValidationError):
        delivery.MilestoneReceipt.model_validate(payload)
