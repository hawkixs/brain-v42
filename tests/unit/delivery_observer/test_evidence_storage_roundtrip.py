"""Real collector records retain typed timestamps through JSONB transport."""

import pytest
from pydantic import ValidationError

from brain_v42.models.delivery import PullRequestEvidence
from tests.unit.delivery_observer.github_cases import GitHubCase


@pytest.mark.asyncio
async def test_collector_check_and_review_timestamps_survive_jsonb_roundtrip():
    evidence = await GitHubCase(approvals=1).collect()
    restored = PullRequestEvidence.model_validate_json(evidence.model_dump_json())
    assert restored == evidence
    assert restored.checks[0].started_at.utcoffset() is not None
    assert restored.reviews[0].submitted_at.utcoffset() is not None


@pytest.mark.parametrize("field", ["started_at", "completed_at", "submitted_at"])
@pytest.mark.asyncio
async def test_stored_provider_timestamps_require_a_real_timezone(field):
    evidence = await GitHubCase(approvals=1).collect()
    payload = evidence.model_dump(mode="json")
    target = payload["reviews"][0] if field == "submitted_at" else payload["checks"][0]
    target[field] = "2026-09-07T12:00:00"
    import json

    with pytest.raises(ValidationError):
        PullRequestEvidence.model_validate_json(json.dumps(payload))
