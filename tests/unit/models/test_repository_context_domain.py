"""Failure-first contracts for repository-context facts and pure delivery freshness."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from tests.delivery_helpers import FIXED_NOW, delivery_inputs, stored_contract_payload


def _repository_reference(
    *, path: str = "docs/guide.md", required: bool = True
) -> dict[str, object]:
    return {
        "kind": "repository_document",
        "repository_id": 42,
        "sha": "a" * 40,
        "path": path,
        "required": required,
    }


def _repository_inputs(*, path: str = "docs/guide.md", required: bool = True):
    from brain_v42.models.delivery import ContractRevision

    payload = stored_contract_payload()
    payload["context_refs"] = [_repository_reference(path=path, required=required)]
    return delivery_inputs(contract=ContractRevision.model_validate(payload), active_bindings=())


def _predicate(*, path: str = "docs/guide.md", finished_at: datetime = FIXED_NOW):
    from brain_v42.models.delivery import (
        ContextPredicate,
        RepositoryContextEvidence,
        RepositoryDocumentFact,
        RepositoryDocumentReference,
        context_reference_digest,
    )

    fact = RepositoryDocumentFact(
        repository_id=42,
        commit_sha="a" * 40,
        path=path,
        tree_sha="b" * 40,
        blob_sha="c" * 40,
        mode="100644",
        status="available",
    )
    evidence = RepositoryContextEvidence(facts=(fact,), complete=True)
    return ContextPredicate(
        reference_identity=f"repository_document:42:{'a' * 40}:{path}",
        current_digest=context_reference_digest(
            RepositoryDocumentReference(**_repository_reference(path=path))
        ),
        status="available",
        snapshot_id=UUID("00000000-0000-0000-0000-000000000101"),
        success_confirmation_id=UUID("00000000-0000-0000-0000-000000000102"),
        latest_attempt_confirmation_id=UUID("00000000-0000-0000-0000-000000000102"),
        collection_started_at=finished_at - timedelta(seconds=1),
        collection_finished_at=finished_at,
        evidence=evidence,
    )


@pytest.mark.parametrize(
    "fact",
    [
        {"mode": "100664"},
        {"tree_sha": None},
        {"blob_sha": None},
    ],
)
def test_available_repository_fact_requires_complete_regular_file_proof(
    fact: dict[str, object],
) -> None:
    from brain_v42.models.delivery import RepositoryDocumentFact

    payload: dict[str, object] = {
        "repository_id": 42,
        "commit_sha": "a" * 40,
        "path": "docs/guide.md",
        "tree_sha": "b" * 40,
        "blob_sha": "c" * 40,
        "mode": "100644",
        "status": "available",
    }
    payload.update(fact)
    with pytest.raises(ValueError):
        RepositoryDocumentFact.model_validate(payload)


def test_context_bundle_rejects_duplicates_but_represents_incomplete_collection() -> None:
    from brain_v42.models.delivery import RepositoryContextEvidence, RepositoryDocumentFact

    fact = RepositoryDocumentFact(
        repository_id=42,
        commit_sha="a" * 40,
        path="docs/guide.md",
        tree_sha="b" * 40,
        blob_sha="c" * 40,
        mode="100755",
        status="available",
    )
    with pytest.raises(ValueError, match="duplicate"):
        RepositoryContextEvidence(facts=(fact, fact), complete=True)
    incomplete = RepositoryContextEvidence(facts=(fact,), complete=False)
    missing = RepositoryDocumentFact(
        repository_id=42,
        commit_sha="a" * 40,
        path="docs/missing.md",
        status="missing",
    )
    incomplete_missing = RepositoryContextEvidence(facts=(missing,), complete=False)

    assert incomplete.complete is False
    assert incomplete_missing.facts[0].status == "missing"


def test_required_repository_context_before_pr_is_fresh_and_enables_implementation() -> None:
    from brain_v42.models.delivery_evaluator import evaluate_delivery

    inputs = _repository_inputs().model_copy(update={"contexts": (_predicate(),)})
    result = evaluate_delivery(inputs, now=FIXED_NOW)

    assert result.observation_health == "fresh"
    assert result.observed_at == FIXED_NOW
    assert result.fresh_until == FIXED_NOW + timedelta(seconds=600)
    assert {(item.kind, item.role) for item in result.eligible_work} == {("implement", "executor")}


@pytest.mark.parametrize("status", ["missing", "error"])
def test_missing_or_error_required_repository_context_blocks_work(status: str) -> None:
    from brain_v42.models.delivery_evaluator import evaluate_delivery

    predicate = _predicate().model_copy(update={"status": status})
    result = evaluate_delivery(
        _repository_inputs().model_copy(update={"contexts": (predicate,)}), now=FIXED_NOW
    )

    assert result.eligible_work == ()
    assert f"context_{status}" in {item.code for item in result.blockers}


@pytest.mark.parametrize(
    "finished_at",
    [FIXED_NOW - timedelta(seconds=601), FIXED_NOW + timedelta(seconds=1)],
)
def test_stale_or_future_required_context_fails_closed(finished_at: datetime) -> None:
    from brain_v42.models.delivery_evaluator import evaluate_delivery

    result = evaluate_delivery(
        _repository_inputs().model_copy(
            update={"contexts": (_predicate(finished_at=finished_at),)}
        ),
        now=FIXED_NOW,
    )

    assert result.eligible_work == ()
    assert result.observation_health in {"stale", "error"}


def test_context_expiry_bounds_freshness_before_a_newer_pr_confirmation() -> None:
    from brain_v42.models.delivery_evaluator import evaluate_delivery

    inputs = _repository_inputs().model_copy(
        update={
            "active_bindings": delivery_inputs().active_bindings,
            "contexts": (_predicate(finished_at=FIXED_NOW - timedelta(seconds=300)),),
        }
    )
    result = evaluate_delivery(inputs, now=FIXED_NOW)

    assert result.fresh_until == FIXED_NOW + timedelta(seconds=300)


def test_optional_old_repository_context_does_not_shorten_required_pr_freshness() -> None:
    from brain_v42.models.delivery import ContractRevision
    from brain_v42.models.delivery_evaluator import evaluate_delivery

    payload = stored_contract_payload()
    payload["context_refs"] = [_repository_reference(required=False)]
    contract = ContractRevision.model_validate(payload)
    result = evaluate_delivery(
        delivery_inputs(
            contract=contract, contexts=(_predicate(finished_at=FIXED_NOW - timedelta(hours=1)),)
        ),
        now=FIXED_NOW,
    )

    assert result.observation_health == "fresh"
    assert result.fresh_until == FIXED_NOW + timedelta(seconds=590)


def test_repeated_context_errors_change_assessment_identity() -> None:
    from brain_v42.models.delivery_evaluator import evaluate_delivery

    first = _predicate().model_copy(
        update={
            "status": "error",
            "latest_attempt_confirmation_id": UUID("00000000-0000-0000-0000-000000000103"),
        }
    )
    second = first.model_copy(
        update={"latest_attempt_confirmation_id": UUID("00000000-0000-0000-0000-000000000104")}
    )
    one = evaluate_delivery(
        _repository_inputs().model_copy(update={"contexts": (first,)}), now=FIXED_NOW
    )
    two = evaluate_delivery(
        _repository_inputs().model_copy(update={"contexts": (second,)}), now=FIXED_NOW
    )

    assert one.assessment_id != two.assessment_id


@pytest.mark.parametrize(
    "started,finished",
    [
        (datetime(2026, 9, 7, 12), datetime(2026, 9, 7, 12, tzinfo=UTC)),
        (datetime(2026, 9, 7, 12, tzinfo=UTC), datetime(2026, 9, 7, 12)),
        (FIXED_NOW, FIXED_NOW - timedelta(seconds=1)),
    ],
)
def test_available_repository_context_rejects_naive_mixed_or_reversed_intervals(
    started: datetime, finished: datetime
) -> None:
    from brain_v42.models.delivery import (
        ContextPredicate,
        RepositoryContextEvidence,
        RepositoryDocumentFact,
    )

    fact = RepositoryDocumentFact(
        repository_id=42,
        commit_sha="a" * 40,
        path="docs/guide.md",
        tree_sha="b" * 40,
        blob_sha="c" * 40,
        mode="100644",
        status="available",
    )
    with pytest.raises(ValueError):
        ContextPredicate(
            reference_identity=f"repository_document:42:{'a' * 40}:docs/guide.md",
            current_digest="d" * 64,
            status="available",
            snapshot_id=UUID("00000000-0000-0000-0000-000000000111"),
            success_confirmation_id=UUID("00000000-0000-0000-0000-000000000112"),
            latest_attempt_confirmation_id=UUID("00000000-0000-0000-0000-000000000112"),
            collection_started_at=started,
            collection_finished_at=finished,
            evidence=RepositoryContextEvidence(facts=(fact,), complete=True),
        )


def test_unobserved_binding_cannot_be_hidden_by_another_fresh_binding() -> None:
    from brain_v42.models.delivery import BindingEvidence
    from brain_v42.models.delivery_evaluator import evaluate_delivery

    inputs = delivery_inputs()
    unobserved = BindingEvidence(binding=inputs.active_bindings[0].binding)
    result = evaluate_delivery(
        inputs.model_copy(update={"active_bindings": (*inputs.active_bindings, unobserved)}),
        now=FIXED_NOW,
    )

    assert result.observation_health == "never_observed"


def test_optional_declared_brain_and_repository_contexts_never_block_or_truncate_paths() -> None:
    from brain_v42.models.delivery import ContextPredicate, ContractRevision
    from brain_v42.models.delivery_evaluator import evaluate_delivery

    long_path = "docs/" * 817 + "guide.md"
    payload = stored_contract_payload()
    payload["context_refs"] = [
        {
            "kind": "brain_entity",
            "entity_type": "adr",
            "entity_id": "00000000-0000-0000-0000-000000000060",
            "content_snapshot": "optional architecture",
            "content_digest": "d" * 64,
            "required": False,
        },
        _repository_reference(path=long_path, required=False),
    ]
    contract = ContractRevision.model_validate(payload)
    contexts = (
        ContextPredicate(
            reference_identity="brain_entity:adr:00000000-0000-0000-0000-000000000060",
            status="missing",
        ),
        ContextPredicate(
            reference_identity=f"repository_document:42:{'a' * 40}:{long_path}", status="missing"
        ),
    )
    result = evaluate_delivery(delivery_inputs(contract=contract, contexts=contexts), now=FIXED_NOW)

    assert len(contexts[1].reference_identity) > 4096
    assert "context_predicate_unexpected" not in {item.code for item in result.blockers}


@pytest.mark.parametrize("path_length", [608, 4096])
def test_required_maximum_path_retains_machine_identity_and_bounds_human_detail(
    path_length: int,
) -> None:
    from brain_v42.models.delivery import ContextPredicate
    from brain_v42.models.delivery_evaluator import evaluate_delivery

    long_path = "d" * path_length
    identity = f"repository_document:42:{'a' * 40}:{long_path}"
    result = evaluate_delivery(
        _repository_inputs(path=long_path).model_copy(
            update={"contexts": (ContextPredicate(reference_identity=identity, status="missing"),)}
        ),
        now=FIXED_NOW,
    )

    assert len(long_path) == path_length
    assert len(identity) > 608
    assert "context_missing" in {item.code for item in result.blockers}
    assert all(len(item.detail) <= 1000 for item in result.blockers)
