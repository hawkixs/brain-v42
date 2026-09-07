"""Literal delivery-contract fixtures used by delivery domain tests."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

NORMALIZED_CONTRACT_DIGEST = "c12ee4ad61554c2e625307165385d65bd7dfa058b5dc15e71eba405a0350d9b1"
FIXED_NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
_HEAD_SHA = "a" * 40
_BASE_SHA = "b" * 40
_INTEGRATION_SHA = "c" * 40


def contract_payload() -> dict[str, Any]:
    """Return an independent valid v1 contract input payload."""
    return {
        "schema_version": 1,
        "objective": "Expose delivery progress from GitHub evidence",
        "constraints": ["Brain never launches execution agents"],
        "acceptance_criteria": [
            "A new commit requires evidence for that revision",
            "The session briefing shows missing proofs without an agent update",
        ],
        "priority": 20,
        "context_refs": [],
        "dependencies": [],
        "deliverables": [
            {
                "key": "implementation",
                "repository": "hawkixs/brain-v42",
                "target_branch": "main",
                "required_checks": [
                    {"kind": "check_run", "name": "test-unit", "app_slug": "github-actions"}
                ],
                "review": {"required_approvals": 0, "allowed_reviewers": []},
            }
        ],
        "acceptance_mode": "explicit",
    }


def contract_payload_copy() -> dict[str, Any]:
    """Return a deep copy when a test needs to modify the fixture."""
    return deepcopy(contract_payload())


def stored_contract_payload() -> dict[str, Any]:
    """Return the independently pinned normalized golden contract payload."""
    payload = contract_payload_copy()
    deliverable = payload["deliverables"][0]
    deliverable["repository_id"] = 42
    return {
        **payload,
        "ticket_id": "00000000-0000-0000-0000-000000000001",
        "contract_revision": 1,
        "author_project": "brain-v42",
    }


def delivery_inputs(**overrides: Any) -> Any:
    """Return literal, complete evidence facts for evaluator behavior tests."""
    from brain_v42.models.delivery import (
        ArtifactBinding,
        BindingEvidence,
        CheckAttempt,
        ContextPredicate,
        ContractRevision,
        EvaluationInput,
        ObservationConfirmation,
        PullRequestEvidence,
        ReviewEvidence,
    )

    stored = stored_contract_payload()
    stored["acceptance_mode"] = overrides.pop("acceptance_mode", stored["acceptance_mode"])
    stored["deliverables"][0]["review"] = {
        "required_approvals": overrides.pop("required_approvals", 0),
        "allowed_reviewers": overrides.pop("allowed_reviewers", []),
    }
    contract = ContractRevision.model_validate(stored)
    binding = ArtifactBinding(
        id=UUID("00000000-0000-0000-0000-000000000010"),
        ticket_id=contract.ticket_id,
        contract_revision=1,
        attempt=1,
        deliverable_key="implementation",
        repository_id=42,
        pr_number=73,
        state="observed",
        head_sha=_HEAD_SHA,
        base_sha=_BASE_SHA,
        integration_sha=_INTEGRATION_SHA,
    )
    check = CheckAttempt(
        provider_id=8001,
        kind="check_run",
        name="test-unit",
        app_slug="github-actions",
        head_sha=_HEAD_SHA,
        conclusion=overrides.pop("required_check", "success"),
        run_attempt=1,
        completed_at=FIXED_NOW - timedelta(seconds=30),
    )
    review = ReviewEvidence(
        provider_id=9001,
        reviewer="reviewer-project",
        head_sha=_HEAD_SHA,
        decision=overrides.pop("review", "approved"),
        submitted_at=FIXED_NOW - timedelta(seconds=20),
    )
    evidence = PullRequestEvidence(
        provider_id=7001,
        repository_id=42,
        pr_number=73,
        head_sha=overrides.pop("head_sha", _HEAD_SHA),
        base_sha=overrides.pop("base_sha", _BASE_SHA),
        integration_sha=_INTEGRATION_SHA,
        state="merged" if overrides.pop("merged", True) else "open",
        draft=overrides.pop("draft", False),
        mergeable=overrides.pop("mergeable", True),
        complete=overrides.pop("complete", True),
        checks=tuple(overrides.pop("checks", (check,))),
        reviews=tuple(overrides.pop("reviews", (review,))),
        integration_revision=_INTEGRATION_SHA,
        collected_at=FIXED_NOW - timedelta(seconds=10),
    )
    confirmation = ObservationConfirmation(
        id=UUID("00000000-0000-0000-0000-000000000020"),
        evidence=evidence,
        collection_started_at=FIXED_NOW - timedelta(seconds=15),
        collection_finished_at=FIXED_NOW - timedelta(seconds=10),
    )
    context_status = overrides.pop("context_status", "available")
    defaults: dict[str, Any] = {
        "contract": contract,
        "attempt": 1,
        "workflow_version": 1,
        "coordination_status": "in_progress",
        "coordination_disposition": "active",
        "is_self_ticket": False,
        "active_bindings": (BindingEvidence(binding=binding, confirmation=confirmation),),
        "contexts": (
            ContextPredicate(
                key="architecture",
                required=True,
                expected_digest="d" * 64,
                current_digest=("e" * 64 if context_status == "changed" else "d" * 64),
                status=context_status,
            ),
        ),
        "dependencies": (),
        "feature_enabled": True,
        "freshness_seconds": 600,
    }
    defaults.update(overrides)
    return EvaluationInput(**defaults)
