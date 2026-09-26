"""Current claim validity is derived from immutable observation times."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from brain_v42.models.claim_read import ClaimRead, VerdictRead, evaluate_claim

NOW = datetime(2026, 9, 26, tzinfo=UTC)


def _claim(
    latest: VerdictRead | None = None,
    conclusive: VerdictRead | None = None,
    *,
    retired: bool = False,
) -> ClaimRead:
    return ClaimRead(
        id=uuid4(),
        seq=1,
        entry_id=uuid4(),
        entity_type="learning",
        project_key="project-a",
        claim_key="a" * 64,
        statement="Lag is bounded",
        fact_name="lag",
        definition_version=1,
        target="production",
        expected={"path": "/lag", "op": "lte", "value": 5},
        expected_resolved={"path": "/lag", "op": "lte", "value": 5},
        validity_seconds=60,
        provenance="declared",
        declared_by="tester",
        declared_at=NOW,
        recorded_at=NOW,
        retired_at=NOW if retired else None,
        replaces_id=None,
        latest=latest,
        conclusive=conclusive,
    )


def _verdict(kind: str, seq: int, emitted: datetime, *, reason: str | None = None) -> VerdictRead:
    return VerdictRead(
        id=uuid4(),
        seq=seq,
        verdict=kind,
        reason=reason,
        emitted_at=emitted,
        recorded_at=NOW,
        observation_id=uuid4(),
        measurement={"value": {"lag": 3}},
    )


def test_no_verdict_is_unverified() -> None:
    state = evaluate_claim(_claim(), NOW)
    assert state.status == "unverified"
    assert state.valid_until is None


def test_unreadable_without_conclusive_is_unreadable() -> None:
    latest = _verdict("unreadable", 1, NOW, reason="probe:timeout")
    state = evaluate_claim(_claim(latest), NOW)
    assert state.status == "unreadable"
    assert state.latest == latest


@pytest.mark.parametrize("kind", ["holds", "falsified"])
def test_conclusive_expires_at_exact_observation_boundary(kind: str) -> None:
    verdict = _verdict(kind, 1, NOW - timedelta(seconds=60))
    claim = _claim(verdict, verdict)
    assert evaluate_claim(claim, NOW - timedelta(microseconds=1)).status == kind
    state = evaluate_claim(claim, NOW)
    assert state.status == "stale"
    assert state.previous_conclusive_kind == kind
    assert state.valid_until == NOW


def test_recording_time_does_not_refresh_old_observation() -> None:
    verdict = _verdict("holds", 1, NOW - timedelta(hours=1))
    state = evaluate_claim(_claim(verdict, verdict), NOW)
    assert state.status == "stale"
    assert state.age_seconds == 3600


def test_later_unreadable_keeps_fresh_conclusive_and_reason() -> None:
    conclusive = _verdict("holds", 2, NOW - timedelta(seconds=10))
    latest = _verdict("unreadable", 3, NOW, reason="probe:timeout")
    state = evaluate_claim(_claim(latest, conclusive), NOW)
    assert state.status == "holds"
    assert state.latest == latest
    assert state.conclusive == conclusive


def test_later_unreadable_keeps_stale_conclusive_and_reason() -> None:
    conclusive = _verdict("falsified", 2, NOW - timedelta(seconds=61))
    latest = _verdict("unreadable", 3, NOW, reason="target_mismatch")
    state = evaluate_claim(_claim(latest, conclusive), NOW)
    assert state.status == "stale"
    assert state.previous_conclusive_kind == "falsified"
    assert state.latest.reason == "target_mismatch"


def test_future_observation_has_zero_rendered_age_and_does_not_mutate_record() -> None:
    verdict = _verdict("holds", 1, NOW + timedelta(seconds=30))
    claim = _claim(verdict, verdict, retired=True)
    state = evaluate_claim(claim, NOW)
    assert state.age_seconds == 0
    assert state.retired_at == NOW
    assert claim.latest == verdict
    assert verdict.emitted_at == NOW + timedelta(seconds=30)
