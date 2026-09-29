"""`NightlyVerifier` orchestrates the wet loop: outcomes, errors, deadline, rc.

All collaborators are fakes (spec ADR 27 lot C, T1.6): the service
(`verify_outcome`), the ownership fence and the release check. Selection is a
plain tuple of `NightlyClaim` built by the test, not a callable dependency.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from uuid import UUID, uuid4

import pytest

from brain_v42.facts.nightly import (
    STATUS_TO_RC,
    NightlyReport,
    NightlyVerifier,
    issuer_for,
    key_for,
)
from brain_v42.models.claim_verdict import ClaimVerificationError
from brain_v42.repositories.pg_claim_nightly import NightlyClaim, VerifyRunOwnershipLost
from brain_v42.repositories.pg_claim_verdicts import VerdictRow


def _claim(index: int, *, fact: str | None = None, target: str = "production") -> NightlyClaim:
    return NightlyClaim(
        id=UUID(int=index + 1),
        project_key=f"project-{index % 3}",
        fact_name=fact or f"fact_{index}",
        target=target,
        definition_version=1,
        validity_seconds=600,
        seq=index,
        age_key=datetime(2026, 9, 27, tzinfo=UTC),
    )


def _row(claim: NightlyClaim, verdict: str, *, reason: str | None = None) -> VerdictRow:
    return VerdictRow(
        id=uuid4(),
        seq=1,
        claim_id=claim.id,
        verdict=verdict,
        reason=reason,
        measurement={},
        measurement_digest=None,
        observation_id=uuid4(),
        issuer_identity="dream:verify:1",
        issuer_kind="robot",
        request_fingerprint="a" * 64,
        outcome_fingerprint="b" * 64,
        idempotency_key="dream-verify:v1:2026-09-27",
        emitted_at=datetime(2026, 9, 27, tzinfo=UTC),
        recorded_at=datetime(2026, 9, 27, tzinfo=UTC),
    )


class _Outcome:
    def __init__(self, row: VerdictRow, replayed: bool) -> None:
        self.row = row
        self.replayed = replayed


class _FakeService:
    """Routes by claim id: an outcome, an error to raise, or a plain exception."""

    def __init__(self) -> None:
        self.calls: list[UUID] = []
        self._outcomes: dict[UUID, object] = {}
        self.max_concurrent = 0
        self._active = 0
        self._hold = asyncio.Event()
        self._hold.set()

    def respond(self, claim: NightlyClaim, result: object) -> None:
        self._outcomes[claim.id] = result

    async def verify_outcome(self, claim_id, issuer_identity, issuer_kind, idempotency_key, **kw):
        self.calls.append(claim_id)
        self._active += 1
        self.max_concurrent = max(self.max_concurrent, self._active)
        try:
            await self._hold.wait()
            result = self._outcomes[claim_id]
            if isinstance(result, BaseException):
                raise result
            return result
        finally:
            self._active -= 1


class _FakeReleaseCheck:
    def __init__(self, action: str = "verify") -> None:
        self.action = action
        self.calls = 0

    async def decide(self):
        from brain_v42.facts.nightly import ReleaseDecision

        self.calls += 1
        return ReleaseDecision(self.action, "a" * 40, "a" * 40, None)


class _FakeOwnership:
    def __init__(self, *, lose_after: int | None = None) -> None:
        self.checks = 0
        self._lose_after = lose_after

    async def check(self) -> None:
        self.checks += 1
        if self._lose_after is not None and self.checks > self._lose_after:
            raise VerifyRunOwnershipLost()


def _verifier(
    service, *, ownership=None, release_check=None, clock=None, **kwargs
) -> NightlyVerifier:
    return NightlyVerifier(
        service=service,
        release_check=release_check or _FakeReleaseCheck(),
        ownership=ownership,
        clock=clock or (lambda: 0.0),
        **kwargs,
    )


async def test_outcomes_are_counted_per_verdict_and_replay_comes_from_the_outcome() -> None:
    service = _FakeService()
    c1, c2, c3 = _claim(0), _claim(1), _claim(2)
    service.respond(c1, _Outcome(_row(c1, "holds"), False))
    service.respond(c2, _Outcome(_row(c2, "falsified"), True))
    service.respond(c3, _Outcome(_row(c3, "unreadable", reason="probe:timeout"), False))

    report = await _verifier(service).run(
        (c1, c2, c3), run_id=1, run_date=date(2026, 9, 27), wet=True
    )

    assert report.holds == 1
    assert report.falsified == 1
    assert report.unreadable == {"probe:timeout": 1}
    assert report.replayed == 1
    assert report.status == "done"
    assert STATUS_TO_RC[report.status] == 0


async def test_wet_run_skips_dream_last_night_without_writing_a_verdict() -> None:
    service = _FakeService()
    self_referential = _claim(0, fact="dream_last_night")
    other = _claim(1)
    service.respond(other, _Outcome(_row(other, "holds"), False))

    report = await _verifier(service, max_concurrency=1).run(
        (self_referential, other), run_id=1, run_date=date(2026, 9, 27), wet=True
    )

    assert service.calls == [other.id]
    assert report.skipped_self_referential == 1
    assert report.as_dict()["skipped_self_referential"] == 1
    assert report.holds == 1
    assert report.status == "done"


async def test_refresh_budget_exhausted_stops_only_its_own_fact() -> None:
    service = _FakeService()
    a1 = _claim(0, fact="fact_a")
    a2 = _claim(1, fact="fact_a")
    b1 = _claim(2, fact="fact_b")
    service.respond(a1, ClaimVerificationError("refresh_budget_exhausted"))
    service.respond(b1, _Outcome(_row(b1, "holds"), False))
    # a2 must be skipped WITHOUT a call: no response registered for it.

    report = await _verifier(service, max_concurrency=1).run(
        (a1, a2, b1), run_id=1, run_date=date(2026, 9, 27), wet=True
    )

    assert a2.id not in service.calls
    assert b1.id in service.calls
    assert report.stopped_facts == ["fact_a"]
    assert report.skipped_budget == 2
    assert report.holds == 1
    assert report.status == "done"


async def test_claim_retired_is_counted_and_the_loop_continues() -> None:
    service = _FakeService()
    c1, c2 = _claim(0), _claim(1)
    service.respond(c1, ClaimVerificationError("claim_retired"))
    service.respond(c2, _Outcome(_row(c2, "holds"), False))

    report = await _verifier(service).run((c1, c2), run_id=1, run_date=date(2026, 9, 27), wet=True)

    assert report.retired_mid_run == 1
    assert report.holds == 1
    assert report.status == "done"


async def test_observation_already_verified_is_counted_with_no_status_effect() -> None:
    service = _FakeService()
    c1 = _claim(0)
    service.respond(c1, ClaimVerificationError("observation_already_verified"))

    report = await _verifier(service).run((c1,), run_id=1, run_date=date(2026, 9, 27), wet=True)

    assert report.errors == {"observation_already_verified": 1}
    assert report.status == "done"


@pytest.mark.parametrize("code", ["claim_not_found", "invalid_emitted_at", "idempotency_conflict"])
async def test_each_partial_error_code_makes_the_run_partial(code: str) -> None:
    service = _FakeService()
    c1, c2 = _claim(0), _claim(1)
    service.respond(c1, ClaimVerificationError(code))
    service.respond(c2, _Outcome(_row(c2, "holds"), False))

    report = await _verifier(service).run((c1, c2), run_id=1, run_date=date(2026, 9, 27), wet=True)

    assert report.errors == {code: 1}
    assert report.holds == 1
    assert report.status == "partial"
    assert STATUS_TO_RC[report.status] == 5


@pytest.mark.parametrize("code", ["invalid_argument", "unknown_actor"])
async def test_each_abort_error_code_stops_the_run_as_fail(code: str) -> None:
    service = _FakeService()
    claims = tuple(_claim(index) for index in range(5))
    service.respond(claims[0], ClaimVerificationError(code))
    for claim in claims[1:]:
        service.respond(claim, _Outcome(_row(claim, "holds"), False))

    report = await _verifier(service, max_concurrency=1).run(
        claims, run_id=1, run_date=date(2026, 9, 27), wet=True
    )

    assert report.status == "fail"
    assert STATUS_TO_RC[report.status] == 1
    assert report.errors == {code: 1}


async def test_an_unexpected_exception_aborts_with_fail_and_logs_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _FakeService()
    c1 = _claim(0)
    service.respond(c1, RuntimeError("boom"))
    logged: list[tuple[str, dict[str, str]]] = []

    class _Logger:
        def exception(self, event: str, **fields: str) -> None:
            logged.append((event, fields))

    monkeypatch.setattr("brain_v42.facts.nightly._LOG", _Logger())

    report = await _verifier(service).run((c1,), run_id=1, run_date=date(2026, 9, 27), wet=True)

    assert report.status == "fail"
    assert report.error_message == "RuntimeError: boom"
    assert logged == [
        (
            "claim_verify_lane_failed",
            {"error_type": "RuntimeError", "error_message": "RuntimeError: boom"},
        )
    ]


async def test_an_unexpected_exception_diagnostic_is_bounded() -> None:
    service = _FakeService()
    claim = _claim(0)
    service.respond(claim, RuntimeError("x" * 3000))

    report = await _verifier(service).run((claim,), run_id=1, run_date=date(2026, 9, 27), wet=True)

    assert len(report.error_message) == 2000


def test_report_serializes_exact_spec_fields() -> None:
    report = NightlyReport(run_date="2026-09-27", run_id=812, mode="wet")
    assert set(report.as_dict()) == {
        "run_date",
        "run_id",
        "mode",
        "started_at",
        "finished_at",
        "status",
        "rc",
        "max_claims",
        "eligible_at_start",
        "selected",
        "holds",
        "falsified",
        "unreadable",
        "replayed",
        "skipped_budget",
        "stopped_facts",
        "skipped_deadline",
        "skipped_release_mismatch",
        "skipped_release_unknown",
        "skipped_self_referential",
        "release",
        "retired_mid_run",
        "errors",
        "dry_claims",
        "dry_facts",
    }


async def test_cancelled_error_propagates_out_of_run() -> None:
    service = _FakeService()
    c1 = _claim(0)
    service.respond(c1, asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        await _verifier(service).run((c1,), run_id=1, run_date=date(2026, 9, 27), wet=True)


async def test_concurrency_never_exceeds_the_configured_bound() -> None:
    service = _FakeService()
    service._hold.clear()
    claims = tuple(_claim(index) for index in range(10))
    for claim in claims:
        service.respond(claim, _Outcome(_row(claim, "holds"), False))

    async def release_soon() -> None:
        await asyncio.sleep(0.05)
        service._hold.set()

    releaser = asyncio.create_task(release_soon())
    report = await _verifier(service, max_concurrency=4).run(
        claims, run_id=1, run_date=date(2026, 9, 27), wet=True
    )
    await releaser

    assert service.max_concurrent <= 4
    assert report.holds == 10


async def test_the_deadline_stops_dispatch_and_in_flight_tasks_finish() -> None:
    service = _FakeService()
    claims = tuple(_claim(index) for index in range(6))
    for claim in claims:
        service.respond(claim, _Outcome(_row(claim, "holds"), False))

    calls_before_deadline = {"n": 0}

    def clock() -> float:
        # The deadline (0.0) is already exceeded from the very first check:
        # nothing should ever dispatch.
        return 1.0

    report = await _verifier(service, clock=clock, deadline_seconds=0.0).run(
        claims, run_id=1, run_date=date(2026, 9, 27), wet=True
    )
    del calls_before_deadline

    assert report.holds == 0
    assert report.skipped_deadline == 6
    assert report.status == "timeout"
    assert STATUS_TO_RC[report.status] == 3


async def test_a_partial_error_plus_the_deadline_still_reports_partial() -> None:
    service = _FakeService()
    c1, c2 = _claim(0), _claim(1)
    service.respond(c1, ClaimVerificationError("claim_not_found"))
    service.respond(c2, _Outcome(_row(c2, "holds"), False))

    ticks = iter([0.0, 0.0, 100.0, 100.0, 100.0, 100.0])

    def clock() -> float:
        return next(ticks, 100.0)

    report = await _verifier(service, clock=clock, deadline_seconds=50.0, max_concurrency=1).run(
        (c1, c2), run_id=1, run_date=date(2026, 9, 27), wet=True
    )

    assert report.status == "partial"
    assert STATUS_TO_RC[report.status] == 5


async def test_ownership_loss_stops_dispatch_cancels_in_flight_and_reports_it() -> None:
    service = _FakeService()
    claims = tuple(_claim(index) for index in range(4))
    for claim in claims:
        service.respond(claim, _Outcome(_row(claim, "holds"), False))
    ownership = _FakeOwnership(lose_after=1)

    report = await _verifier(service, ownership=ownership, max_concurrency=1).run(
        claims, run_id=1, run_date=date(2026, 9, 27), wet=True
    )

    assert report.status == "ownership_lost"
    assert STATUS_TO_RC[report.status] == 7
    assert len(service.calls) < len(claims)


async def test_ownership_is_never_checked_in_dry_mode() -> None:
    service = _FakeService()
    c1 = _claim(0)
    service.respond(c1, _Outcome(_row(c1, "holds"), False))
    ownership = _FakeOwnership(lose_after=0)

    report = await _verifier(service, ownership=ownership).run(
        (c1,), run_id=1, run_date=date(2026, 9, 27), wet=False
    )

    assert ownership.checks == 0
    assert report.status == "done"


async def test_live_release_claims_go_through_the_release_check() -> None:
    service = _FakeService()
    c1 = _claim(0, target="live_release")
    service.respond(c1, _Outcome(_row(c1, "holds"), False))
    release_check = _FakeReleaseCheck("skip_release_mismatch")

    report = await _verifier(service, release_check=release_check).run(
        (c1,), run_id=1, run_date=date(2026, 9, 27), wet=True
    )
    assert report.release == {
        "cli_sha": "a" * 40,
        "server_sha": "a" * 40,
        "unknown_reason": None,
    }

    assert release_check.calls == 1
    assert c1.id not in service.calls
    assert report.skipped_release_mismatch == 1
    assert report.status == "done"


async def test_wet_release_decision_is_reused_for_every_live_release_claim() -> None:
    service = _FakeService()
    claims = tuple(_claim(index, target="live_release") for index in range(20))
    release_check = _FakeReleaseCheck("skip_release_mismatch")

    report = await _verifier(service, release_check=release_check).run(
        claims, run_id=1, run_date=date(2026, 9, 27), wet=True
    )

    assert release_check.calls == 1
    assert report.skipped_release_mismatch == len(claims)
    assert service.calls == []


async def test_a_non_live_release_claim_never_calls_the_release_check() -> None:
    service = _FakeService()
    c1 = _claim(0, target="production")
    service.respond(c1, _Outcome(_row(c1, "holds"), False))
    release_check = _FakeReleaseCheck("verify")

    await _verifier(service, release_check=release_check).run(
        (c1,), run_id=1, run_date=date(2026, 9, 27), wet=True
    )

    assert release_check.calls == 0


async def test_the_issuer_and_key_passed_to_the_service_use_the_shared_constants() -> None:
    service = _FakeService()
    c1 = _claim(0)
    service.respond(c1, _Outcome(_row(c1, "holds"), False))
    captured: dict[str, object] = {}

    original = service.verify_outcome

    async def spy(claim_id, issuer_identity, issuer_kind, idempotency_key, **kw):
        captured["issuer"] = issuer_identity
        captured["kind"] = issuer_kind
        captured["key"] = idempotency_key
        captured["project_key"] = kw.get("project_key")
        return await original(claim_id, issuer_identity, issuer_kind, idempotency_key, **kw)

    service.verify_outcome = spy
    run_date = date(2026, 9, 27)

    await _verifier(service).run((c1,), run_id=812, run_date=run_date, wet=True)

    assert captured["issuer"] == issuer_for(812)
    assert captured["kind"] == "robot"
    assert captured["key"] == key_for(run_date)
    assert captured["project_key"] == c1.project_key
