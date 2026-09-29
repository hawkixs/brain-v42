"""Dry mode: classify, measure once per distinct fact, never verify (spec §3.4, T1.7)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from uuid import UUID

from brain_v42.facts.model import FactTarget, Measured, SourceIdentity, Unreadable
from brain_v42.facts.nightly import NightlyVerifier, ReleaseDecision

_IDENTITY = SourceIdentity("1", "brain_test", "127.0.0.1", 5432)


@dataclass(frozen=True, slots=True)
class _Claim:
    id: UUID
    project_key: str
    fact_name: str
    target: str
    definition_version: int


def _claim(index: int, *, fact: str, version: int = 1, target: str = "production") -> _Claim:
    return _Claim(
        id=UUID(int=index + 1),
        project_key="project-a",
        fact_name=fact,
        target=target,
        definition_version=version,
    )


@dataclass(frozen=True, slots=True)
class _Descriptor:
    definition_version: int
    target: FactTarget


class _FakeRegistry:
    def __init__(
        self, descriptors: dict[str, _Descriptor], measurements: dict[str, object]
    ) -> None:
        self._descriptors = descriptors
        self._measurements = measurements
        self.measure_calls: list[tuple[str, object]] = []

    def names(self) -> tuple[str, ...]:
        return tuple(self._descriptors)

    def describe(self, name: str) -> _Descriptor:
        return self._descriptors[name]

    def refusals(self) -> dict[str, str]:
        return {}

    def disabled(self) -> dict[str, str]:
        return {}

    def expected_identity(self, target: FactTarget) -> object:
        return _IDENTITY

    async def measure(self, name: str, *, max_age: object = None) -> object:
        self.measure_calls.append((name, max_age))
        return self._measurements[name]


class _FakeReleaseCheckDry:
    def __init__(self, action: str) -> None:
        self.action = action
        self.calls = 0

    async def decide(self) -> ReleaseDecision:
        self.calls += 1
        return ReleaseDecision(self.action, "a" * 40, "b" * 40, None)


def _measured(fact: str, *, wrong_identity: bool = False) -> Measured:
    return Measured.from_value(
        fact=fact,
        definition_version=1,
        target=FactTarget.PRODUCTION,
        source=SourceIdentity("2", "other", "10.0.0.1", 5432) if wrong_identity else _IDENTITY,
        value={"x": 1},
        observation_id=UUID(int=1),
        measured_at=datetime(2026, 9, 27, tzinfo=UTC),
        duration_ms=1,
        ttl_seconds=30,
    )


def _unreadable(fact: str, error_code: str = "timeout") -> Unreadable:
    return Unreadable(
        fact=fact,
        definition_version=1,
        target=FactTarget.PRODUCTION,
        error_code=error_code,
        where="probe",
        observation_id=UUID(int=2),
        measured_at=datetime(2026, 9, 27, tzinfo=UTC),
        duration_ms=1,
        ttl_seconds=30,
        source_kind="probe",
    )


async def test_measures_exactly_once_per_distinct_measurable_fact() -> None:
    registry = _FakeRegistry(
        descriptors={"fact_a": _Descriptor(1, FactTarget.PRODUCTION)},
        measurements={"fact_a": _measured("fact_a")},
    )
    claims = (_claim(0, fact="fact_a"), _claim(1, fact="fact_a"), _claim(2, fact="fact_a"))
    verifier = NightlyVerifier(service=None, release_check=_FakeReleaseCheckDry("verify"))

    report = await verifier.run_dry(claims, registry=registry, run_id=1, run_date=date(2026, 9, 27))

    assert registry.measure_calls == [("fact_a", None)]
    assert report.dry_facts == {
        "fact_a": {"status": "measured", "error_code": None, "identity_ok": True}
    }
    assert report.status == "done"


async def test_measurable_counts_claims_while_measuring_each_fact_once() -> None:
    registry = _FakeRegistry(
        descriptors={"fact_a": _Descriptor(1, FactTarget.PRODUCTION)},
        measurements={"fact_a": _measured("fact_a")},
    )
    claims = tuple(_claim(index, fact="fact_a") for index in range(3))
    verifier = NightlyVerifier(service=None, release_check=_FakeReleaseCheckDry("verify"))

    report = await verifier.run_dry(claims, registry=registry, run_id=1, run_date=date(2026, 9, 27))

    assert report.dry_claims["measurable"] == 3
    assert registry.measure_calls == [("fact_a", None)]


async def test_dry_release_decision_is_reused_for_every_live_release_claim() -> None:
    registry = _FakeRegistry(
        descriptors={"fact_a": _Descriptor(1, FactTarget.LIVE_RELEASE)}, measurements={}
    )
    claims = tuple(_claim(index, fact="fact_a", target="live_release") for index in range(20))
    release_check = _FakeReleaseCheckDry("skip_release_mismatch")
    verifier = NightlyVerifier(service=None, release_check=release_check)

    report = await verifier.run_dry(claims, registry=registry, run_id=1, run_date=date(2026, 9, 27))

    assert release_check.calls == 1
    assert report.dry_claims["release_skip"] == len(claims)


async def test_a_fact_absent_from_the_catalogue_is_historical_and_not_measured() -> None:
    registry = _FakeRegistry(descriptors={}, measurements={})
    claims = (_claim(0, fact="fact_gone", version=1),)
    verifier = NightlyVerifier(service=None, release_check=_FakeReleaseCheckDry("verify"))

    report = await verifier.run_dry(claims, registry=registry, run_id=1, run_date=date(2026, 9, 27))

    assert registry.measure_calls == []
    assert report.dry_claims == {
        "historical_definition": [
            {"fact": "fact_gone", "stored_version": 1, "current_version": None}
        ],
        "release_skip": 0,
        "measurable": 0,
    }


async def test_a_claim_whose_version_differs_is_historical_and_not_measured() -> None:
    registry = _FakeRegistry(
        descriptors={"fact_a": _Descriptor(2, FactTarget.PRODUCTION)}, measurements={}
    )
    claims = (_claim(0, fact="fact_a", version=1),)
    verifier = NightlyVerifier(service=None, release_check=_FakeReleaseCheckDry("verify"))

    report = await verifier.run_dry(claims, registry=registry, run_id=1, run_date=date(2026, 9, 27))

    assert registry.measure_calls == []
    assert report.dry_claims["historical_definition"] == [
        {"fact": "fact_a", "stored_version": 1, "current_version": 2}
    ]


async def test_a_claim_whose_target_differs_is_historical_and_not_measured() -> None:
    registry = _FakeRegistry(
        descriptors={"fact_a": _Descriptor(1, FactTarget.HOST)}, measurements={}
    )
    claims = (_claim(0, fact="fact_a", version=1, target="production"),)
    verifier = NightlyVerifier(service=None, release_check=_FakeReleaseCheckDry("verify"))

    report = await verifier.run_dry(claims, registry=registry, run_id=1, run_date=date(2026, 9, 27))

    assert registry.measure_calls == []
    assert report.dry_claims["historical_definition"] == [
        {"fact": "fact_a", "stored_version": 1, "current_version": 1}
    ]


async def test_a_live_release_claim_that_would_skip_is_counted_as_release_skip() -> None:
    registry = _FakeRegistry(
        descriptors={"fact_a": _Descriptor(1, FactTarget.LIVE_RELEASE)}, measurements={}
    )
    claims = (_claim(0, fact="fact_a", target="live_release"),)
    release_check = _FakeReleaseCheckDry("skip_release_mismatch")
    verifier = NightlyVerifier(service=None, release_check=release_check)

    report = await verifier.run_dry(claims, registry=registry, run_id=1, run_date=date(2026, 9, 27))

    assert release_check.calls == 1
    assert registry.measure_calls == []
    assert report.dry_claims["release_skip"] == 1
    assert report.dry_claims["measurable"] == 0


async def test_dry_facts_records_an_unreadable_measurement() -> None:
    registry = _FakeRegistry(
        descriptors={"fact_a": _Descriptor(1, FactTarget.PRODUCTION)},
        measurements={"fact_a": _unreadable("fact_a", "timeout")},
    )
    claims = (_claim(0, fact="fact_a"),)
    verifier = NightlyVerifier(service=None, release_check=_FakeReleaseCheckDry("verify"))

    report = await verifier.run_dry(claims, registry=registry, run_id=1, run_date=date(2026, 9, 27))

    assert report.dry_facts == {
        "fact_a": {"status": "unreadable", "error_code": "timeout", "identity_ok": None}
    }


async def test_dry_facts_records_an_identity_mismatch() -> None:
    registry = _FakeRegistry(
        descriptors={"fact_a": _Descriptor(1, FactTarget.PRODUCTION)},
        measurements={"fact_a": _measured("fact_a", wrong_identity=True)},
    )
    claims = (_claim(0, fact="fact_a"),)
    verifier = NightlyVerifier(service=None, release_check=_FakeReleaseCheckDry("verify"))

    report = await verifier.run_dry(claims, registry=registry, run_id=1, run_date=date(2026, 9, 27))

    assert report.dry_facts == {
        "fact_a": {"status": "measured", "error_code": None, "identity_ok": False}
    }


async def test_dry_mode_writes_no_verdict_and_takes_no_lock() -> None:
    """Structural proof at this layer: `service` is never even passed a callable."""
    registry = _FakeRegistry(
        descriptors={"fact_a": _Descriptor(1, FactTarget.PRODUCTION)},
        measurements={"fact_a": _measured("fact_a")},
    )
    claims = (_claim(0, fact="fact_a"),)
    verifier = NightlyVerifier(service=None, release_check=_FakeReleaseCheckDry("verify"))

    report = await verifier.run_dry(claims, registry=registry, run_id=1, run_date=date(2026, 9, 27))

    assert report.mode == "dry"
    assert report.holds == 0 and report.falsified == 0
