"""Strict boundaries for artifact observation confirmations."""

from datetime import UTC, datetime, timedelta, tzinfo

import pytest
from pydantic import ValidationError

from brain_v42.models.delivery import ObservationConfirmation, PullRequestEvidence


class _TimezoneWithoutOffset(tzinfo):
    def utcoffset(self, value: datetime | None) -> None:
        return None

    def dst(self, value: datetime | None) -> None:
        return None

    def tzname(self, value: datetime | None) -> str:
        return "naive-with-tzinfo"


def _evidence(*, complete: bool = True) -> PullRequestEvidence:
    return PullRequestEvidence(
        provider_id=7001,
        repository_id=1_337_360_966,
        pr_number=42,
        author_id="executor",
        head_repository_id=9988,
        head_sha="a" * 40,
        base_sha="b" * 40,
        base_ref="main",
        state="open",
        draft=False,
        complete=complete,
        collected_at=datetime(2026, 9, 7, 12, tzinfo=UTC),
    )


@pytest.mark.parametrize(
    ("outcome", "evidence", "error_code"),
    [
        ("success", _evidence(), "provider_timeout"),
        ("error", _evidence(), "provider_timeout"),
        ("error", None, None),
        ("error", None, "caller_supplied_error"),
    ],
)
def test_observation_confirmation_requires_a_complete_and_unambiguous_outcome(
    outcome: str, evidence: PullRequestEvidence | None, error_code: str | None
) -> None:
    """Dropping outcome checks would let stored confirmations contradict their payload."""
    instant = datetime(2026, 9, 7, 12, tzinfo=UTC)

    with pytest.raises(ValidationError):
        ObservationConfirmation(
            evidence=evidence,
            collection_started_at=instant,
            collection_finished_at=instant,
            outcome=outcome,  # type: ignore[arg-type]
            error_code=error_code,
        )


def test_observation_confirmation_keeps_incomplete_provider_facts_representable() -> None:
    """The pure evaluator, rather than a publisher DTO, decides incomplete-proof refusal."""
    instant = datetime(2026, 9, 7, 12, tzinfo=UTC)

    confirmation = ObservationConfirmation(
        evidence=_evidence(complete=False),
        collection_started_at=instant,
        collection_finished_at=instant,
    )

    assert confirmation.evidence is not None
    assert confirmation.evidence.complete is False


@pytest.mark.parametrize("outcome", ["success", "error"])
def test_observation_confirmation_rejects_tzinfo_without_a_utc_offset(outcome: str) -> None:
    """A non-None tzinfo is still naive when it cannot supply a UTC offset."""
    instant = datetime(2026, 9, 7, 12, tzinfo=_TimezoneWithoutOffset())

    with pytest.raises(ValidationError):
        ObservationConfirmation(
            evidence=_evidence() if outcome == "success" else None,
            collection_started_at=instant,
            collection_finished_at=instant,
            outcome=outcome,  # type: ignore[arg-type]
            error_code=None if outcome == "success" else "provider_timeout",
        )


@pytest.mark.parametrize(
    ("outcome", "started", "finished"),
    [
        ("success", datetime(2026, 9, 7, 12), datetime(2026, 9, 7, 12)),
        ("error", datetime(2026, 9, 7, 12), datetime(2026, 9, 7, 12)),
        (
            "success",
            datetime(2026, 9, 7, 12, tzinfo=UTC),
            datetime(2026, 9, 7, 12),
        ),
        (
            "error",
            datetime(2026, 9, 7, 12, tzinfo=UTC),
            datetime(2026, 9, 7, 12),
        ),
        (
            "success",
            datetime(2026, 9, 7, 12, tzinfo=UTC) + timedelta(seconds=1),
            datetime(2026, 9, 7, 12, tzinfo=UTC),
        ),
        (
            "error",
            datetime(2026, 9, 7, 12, tzinfo=UTC) + timedelta(seconds=1),
            datetime(2026, 9, 7, 12, tzinfo=UTC),
        ),
    ],
)
def test_observation_confirmation_requires_aware_ordered_intervals(
    outcome: str, started: datetime, finished: datetime
) -> None:
    """Timezone or ordering regressions must fail before a database write is attempted."""
    with pytest.raises(ValidationError):
        ObservationConfirmation(
            evidence=_evidence() if outcome == "success" else None,
            collection_started_at=started,
            collection_finished_at=finished,
            outcome=outcome,  # type: ignore[arg-type]
            error_code=None if outcome == "success" else "provider_timeout",
        )
