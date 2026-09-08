"""Acceptance boundaries for observable delivery contracts."""

from copy import deepcopy
from uuid import uuid4

import pytest
from pydantic import ValidationError

from tests.delivery_helpers import (
    NORMALIZED_CONTRACT_DIGEST,
    contract_payload,
    stored_contract_payload,
)


def test_contract_digest_preserves_the_published_unicode_vector() -> None:
    from brain_v42.models.delivery_hashes import canonical_digest

    assert canonical_digest({"z": None, "a": ["é", 1, True]}, domain="contract") == (
        "3a9f5e4221a8f912fdb0af7ba4087dbd02fbcb3c82713f6676cb69bcbf3d87af"
    )


def test_empty_check_policy_needs_an_explicit_reason() -> None:
    from brain_v42.models.delivery import ContractInput

    payload = contract_payload()
    payload["deliverables"][0]["required_checks"] = []
    with pytest.raises(ValidationError, match="no_checks_reason"):
        ContractInput.model_validate(payload)


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda payload: payload.update(unexpected=True), "unexpected"),
        (lambda payload: payload.update(priority="20"), "priority"),
        (lambda payload: payload.update(objective="\ud800"), "unicode"),
        (lambda payload: payload["constraints"].append(1.5), "constraints"),
        (
            lambda payload: payload["deliverables"].append(deepcopy(payload["deliverables"][0])),
            "duplicate",
        ),
        (
            lambda payload: payload["deliverables"][0]["required_checks"].append(
                deepcopy(payload["deliverables"][0]["required_checks"][0])
            ),
            "duplicate",
        ),
        (lambda payload: payload["deliverables"][0].update(repository="hawkixs"), "repository"),
        (
            lambda payload: payload["deliverables"][0].update(target_branch="../../main"),
            "target_branch",
        ),
        (lambda payload: payload["deliverables"][0].update(repository_id=0), "repository_id"),
    ],
)
def test_contract_rejects_invalid_input_boundaries(mutate, match: str) -> None:  # type: ignore[no-untyped-def]
    from brain_v42.models.delivery import ContractInput

    payload = contract_payload()
    mutate(payload)
    with pytest.raises(ValidationError, match=match):
        ContractInput.model_validate(payload)


def test_review_policy_requires_explicit_approvals_even_when_zero_is_valid() -> None:
    from brain_v42.models.delivery import ContractInput

    valid = ContractInput.model_validate(contract_payload())
    assert valid.deliverables[0].review.required_approvals == 0
    payload = contract_payload()
    del payload["deliverables"][0]["review"]["required_approvals"]
    with pytest.raises(ValidationError, match="required_approvals"):
        ContractInput.model_validate(payload)


def test_context_references_validate_identity_and_proof_semantics() -> None:
    from brain_v42.models.delivery import (
        ContractInput,
        ContractRevision,
        PinnedBrainEntityReference,
    )

    payload = contract_payload()
    payload["context_refs"] = [
        {
            "kind": "repository_document",
            "repository_id": 42,
            "sha": "a" * 40,
            "path": "docs/contract.md",
            "required": True,
        },
        {
            "kind": "brain_entity",
            "entity_type": "learning",
            "entity_id": str(uuid4()),
            "required": True,
        },
    ]
    assert len(ContractInput.model_validate(payload).context_refs) == 2

    payload["context_refs"][1]["content_snapshot"] = "caller supplied"
    with pytest.raises(ValidationError, match="content_snapshot"):
        ContractInput.model_validate(payload)

    payload = contract_payload()
    payload["context_refs"] = [
        {
            "kind": "brain_entity",
            "entity_type": "knowledge",
            "entity_id": str(uuid4()),
            "required": True,
        }
    ]
    with pytest.raises(ValidationError, match="entity_type"):
        ContractInput.model_validate(payload)

    stored = stored_contract_payload()
    stored["context_refs"] = [
        {
            "kind": "brain_entity",
            "entity_type": "learning",
            "entity_id": str(uuid4()),
            "content_snapshot": '{"confidence":"high"}',
            "content_digest": "b" * 64,
            "required": True,
        }
    ]
    revision = ContractRevision.model_validate(stored)
    assert isinstance(revision.context_refs[0], PinnedBrainEntityReference)
    with pytest.raises(ValidationError, match="frozen"):
        revision.context_refs[0].content_snapshot = "rewritten"  # type: ignore[misc]

    payload = contract_payload()
    payload["context_refs"] = [
        {"kind": "url", "url": "https://example.test/proof", "required": True}
    ]
    with pytest.raises(ValidationError, match="reference-only"):
        ContractInput.model_validate(payload)


def test_dependency_rejects_duplicates_and_stored_revision_rejects_self_reference() -> None:
    from brain_v42.models.delivery import ContractInput, ContractRevision

    ticket_id = uuid4()
    dependency = {
        "ticket_id": str(ticket_id),
        "contract_revision": 1,
        "attempt": 1,
        "milestone": "integrated",
    }
    payload = contract_payload()
    payload["dependencies"] = [dependency, deepcopy(dependency)]
    with pytest.raises(ValidationError, match="duplicate"):
        ContractInput.model_validate(payload)

    payload["dependencies"] = [dependency]
    payload["deliverables"][0]["repository_id"] = 42
    with pytest.raises(ValidationError, match="self_dependency"):
        ContractRevision.model_validate(
            {
                **payload,
                "ticket_id": str(ticket_id),
                "contract_revision": 1,
                "author_project": "brain-v42",
            }
        )


def test_stored_contract_revision_requires_normalized_repository_identity_and_is_immutable() -> (
    None
):
    from brain_v42.models.delivery import ContractRevision

    payload = contract_payload()
    with pytest.raises(ValidationError, match="repository_id"):
        ContractRevision.model_validate(
            {
                **payload,
                "ticket_id": str(uuid4()),
                "contract_revision": 1,
                "author_project": "brain-v42",
            }
        )

    revision = ContractRevision.model_validate(stored_contract_payload())
    with pytest.raises(ValidationError, match="frozen"):
        revision.priority = 10  # type: ignore[misc]
    with pytest.raises(AttributeError):
        revision.constraints.append("rewritten")
    with pytest.raises(ValidationError, match="frozen"):
        revision.deliverables[0].target_branch = "rewritten"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        revision.deliverables[0].required_checks.append(revision.deliverables[0].required_checks[0])
    with pytest.raises(ValidationError, match="frozen"):
        revision.deliverables[0].required_checks[0].name = "rewritten"  # type: ignore[misc]
    with pytest.raises(ValidationError, match="frozen"):
        revision.deliverables[0].review.required_approvals = 1  # type: ignore[misc]


@pytest.mark.parametrize("value", [True, 1.0, "1"])
def test_schema_version_requires_the_integer_one(value: object) -> None:
    from brain_v42.models.delivery import ContractInput

    payload = contract_payload()
    payload["schema_version"] = value
    with pytest.raises(ValidationError, match="schema_version"):
        ContractInput.model_validate(payload)


def test_canonical_digest_rejects_invalid_recursive_values_and_preserves_list_order() -> None:
    from brain_v42.models.delivery_hashes import canonical_digest

    with pytest.raises(ValueError, match="float"):
        canonical_digest({"nested": [1.5]}, domain="contract")
    with pytest.raises(ValueError, match="surrogate"):
        canonical_digest({"text": "\ud800"}, domain="contract")
    with pytest.raises(ValueError, match="string keys"):
        canonical_digest({1: "not allowed"}, domain="contract")  # type: ignore[dict-item]
    assert canonical_digest({"checks": ["a", "b"]}, domain="contract") != canonical_digest(
        {"checks": ["b", "a"]}, domain="contract"
    )


def test_contract_digest_excludes_revision_metadata_and_keeps_contract_content() -> None:
    from brain_v42.models.delivery import ContractInput, ContractRevision
    from brain_v42.models.delivery_hashes import contract_digest

    first = ContractRevision.model_validate(stored_contract_payload())
    second_input = stored_contract_payload()
    second_input["ticket_id"] = str(uuid4())
    second_input["contract_revision"] = 2
    second_input["amendment_reason"] = "Acceptance criteria clarified"
    second_input["deliverables"][0]["repository"] = "renamed/brain-v42"
    second = ContractRevision.model_validate(second_input)
    assert first.content_digest == NORMALIZED_CONTRACT_DIGEST
    assert contract_digest(first) == NORMALIZED_CONTRACT_DIGEST
    assert contract_digest(first) == contract_digest(second)
    with pytest.raises(ValueError, match="normalized ContractRevision"):
        contract_digest(ContractInput.model_validate(contract_payload()))


def test_stored_contract_digest_is_computed_or_must_match_content() -> None:
    from brain_v42.models.delivery import ContractRevision

    computed = ContractRevision.model_validate(stored_contract_payload())
    assert computed.content_digest == NORMALIZED_CONTRACT_DIGEST
    with pytest.raises(ValidationError, match="content_digest"):
        ContractRevision.model_validate({**stored_contract_payload(), "content_digest": "c" * 64})


def test_artifact_binding_uses_pinned_positive_identities_and_sha_values() -> None:
    from brain_v42.models.delivery import ArtifactBinding

    binding = ArtifactBinding.model_validate(
        {
            "id": str(uuid4()),
            "ticket_id": str(uuid4()),
            "contract_revision": 1,
            "attempt": 1,
            "deliverable_key": "implementation",
            "repository_id": 42,
            "pr_number": 7,
            "state": "observed",
            "head_sha": "a" * 40,
            "base_sha": "b" * 64,
        }
    )
    assert binding.repository_id == 42
    with pytest.raises(ValidationError, match="sha"):
        ArtifactBinding.model_validate({**binding.model_dump(), "head_sha": "ABC"})


def test_proposed_artifact_binding_has_no_revision_proof() -> None:
    from brain_v42.models.delivery import ArtifactBinding

    proposed = ArtifactBinding.model_validate(
        {
            "id": str(uuid4()),
            "ticket_id": str(uuid4()),
            "contract_revision": 1,
            "attempt": 1,
            "deliverable_key": "implementation",
            "repository_id": 42,
            "pr_number": 7,
            "state": "proposed",
        }
    )
    assert not proposed.is_observed
    with pytest.raises(ValidationError, match="head_sha and base_sha"):
        ArtifactBinding.model_validate({**proposed.model_dump(), "state": "observed"})
    with pytest.raises(ValidationError, match="proposed"):
        ArtifactBinding.model_validate({**proposed.model_dump(), "head_sha": "a" * 40})
