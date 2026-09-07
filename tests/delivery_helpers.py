"""Literal delivery-contract fixtures used by delivery domain tests."""

from copy import deepcopy
from typing import Any

NORMALIZED_CONTRACT_DIGEST = "c12ee4ad61554c2e625307165385d65bd7dfa058b5dc15e71eba405a0350d9b1"


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
