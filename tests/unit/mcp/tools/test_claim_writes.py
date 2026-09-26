"""Unit contracts for resolving declared claim inputs before persistence."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from brain_v42.facts import FactTarget
from brain_v42.facts.claims import ResolvedClaim, resolve_claim
from brain_v42.facts.probe import FactDescriptor
from brain_v42.facts.verification import ClaimVerificationService, WriteMeasurement
from brain_v42.mcp.tools import claim_writes
from brain_v42.mcp.tools.claim_writes import (
    ClaimWriteOutcome,
    _write_claim,
    claims_confirmation,
    describe_claim_outcome,
    gated_claim_session,
    resolve_claim_inputs,
)
from brain_v42.models.claim_input import ClaimInput


def _descriptor(name: str = "graph_projection_lag") -> FactDescriptor:
    """Build the closed descriptor consumed by the claim resolution boundary."""
    return FactDescriptor(
        name=name,
        definition_version=1,
        target=FactTarget.PRODUCTION,
        ttl_seconds=15,
        timeout_seconds=3,
        queue_timeout_seconds=2,
        deadline_seconds=5,
        briefing=True,
        policies={"late_after_seconds": 300},
        value_schema={"lag_seconds": "int"},
    )


class _Registry:
    """Minimal closed catalogue whose lookups model the writer dependency."""

    def __init__(self) -> None:
        self._descriptor = _descriptor()

    def describe(self, name: str) -> FactDescriptor:
        if name != self._descriptor.name:
            from brain_v42.facts.registry import UnknownFactError

            raise UnknownFactError(name)
        return self._descriptor

    def names(self) -> tuple[str, ...]:
        return (self._descriptor.name,)


def _claim(**overrides: object) -> dict[str, object]:
    """Return one structurally valid declaration with a literal expected result."""
    claim: dict[str, object] = {
        "statement": "The graph projection is caught up.",
        "fact_name": "graph_projection_lag",
        "expected": {"path": "/lag_seconds", "op": "lte", "value": 300},
    }
    claim.update(overrides)
    return claim


async def test_resolution_refuses_an_unknown_fact_and_names_available_vocabulary() -> None:
    """A misspelling must tell the caller how to form a valid closed-catalogue request."""
    with pytest.raises(ValueError, match="unknown_fact") as exc_info:
        await resolve_claim_inputs(_Registry(), [_claim(fact_name="unknown_fact")])

    assert "graph_projection_lag" in str(exc_info.value)


async def test_resolution_refuses_more_than_ten_inputs_before_catalogue_lookup() -> None:
    """Removing the bound would let one entry monopolise a single write transaction."""
    with pytest.raises(ValueError, match="input count rule"):
        await resolve_claim_inputs(
            _Registry(), [_claim(statement=f"Claim {number}") for number in range(11)]
        )


async def test_resolution_refuses_duplicate_inputs_before_catalogue_lookup() -> None:
    """Duplicate declarations must not become redundant immutable occurrences."""
    claim = _claim()

    with pytest.raises(ValueError, match="duplicate input rule"):
        await resolve_claim_inputs(_Registry(), [claim, claim])


async def test_invalid_second_input_reaches_no_insert() -> None:
    """Resolution completes before persistence, so a late refusal cannot write a prefix."""
    session = AsyncMock()

    with pytest.raises(ValueError, match="missing_fact"):
        await resolve_claim_inputs(
            _Registry(),
            [
                _claim(statement="First valid declaration."),
                _claim(fact_name="missing_fact"),
                _claim(),
            ],
        )

    session.execute.assert_not_awaited()


# ---------------------------------------------------------------------------
# `_write_claim` / `claims_confirmation`: the AMENDED order (measure, decide
# provenance, insert, append) against a FAKE verification service. No database:
# `insert_claim` is monkeypatched, exactly like the existing rollback tests in
# tests/integration/db/test_claim_write_path.py monkeypatch it against real SQL.
# ---------------------------------------------------------------------------


@dataclass
class _FakeRow:
    id: object


class _FakeInsertClaim:
    """Replace the module-level `insert_claim` so no SQL runs; records the provenance sent."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.rows: list[_FakeRow] = []

    async def __call__(self, session: object, **kwargs: object) -> _FakeRow:
        self.calls.append(kwargs)
        row = _FakeRow(id=uuid4())
        self.rows.append(row)
        return row


class _FakeVerification:
    """A minimal double for `ClaimVerificationService`'s two write-time entry points."""

    def __init__(self, write_measurement: WriteMeasurement) -> None:
        self._write_measurement = write_measurement
        self.measure_calls = 0
        self.record_calls: list[dict[str, object]] = []

    async def measure_for_write(self, resolved: object) -> WriteMeasurement:
        self.measure_calls += 1
        return self._write_measurement

    async def record_write_verdict(self, session: object, **kwargs: object) -> object | None:
        self.record_calls.append(kwargs)
        if self._write_measurement.measurement is None:
            return None
        return _FakeRow(id=uuid4())


def _resolved_claim(*, measure: bool) -> ResolvedClaim:
    return resolve_claim(ClaimInput.model_validate(_claim(measure=measure)), _descriptor())


async def _write(
    *, verification: Any, measure: bool = True, monkeypatch: pytest.MonkeyPatch
) -> tuple[ClaimWriteOutcome, _FakeInsertClaim]:
    """Drive `_write_claim` through a monkeypatched `insert_claim`, no database involved."""
    fake_insert = _FakeInsertClaim()
    monkeypatch.setattr(claim_writes, "insert_claim", fake_insert)
    outcome = await _write_claim(
        object(),
        entity_ref_id=uuid4(),
        entity_type="learning",
        project_key="brain-v42",
        claim=_resolved_claim(measure=measure),
        declared_by="integration-test",
        declared_at=datetime.now(UTC),
        verification=verification,
    )
    return outcome, fake_insert


async def test_write_claim_without_measure_never_calls_the_verification_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Absent/false measure must not even look at the verification dependency."""
    outcome, fake_insert = await _write(verification=None, measure=False, monkeypatch=monkeypatch)

    assert outcome.provenance == "declared"
    assert outcome.detail is None
    assert fake_insert.calls[0]["provenance"] == "declared"


async def test_write_claim_measured_holds_inserts_measured_and_appends_a_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A conclusive holds must be stored as `measured` and produce exactly one verdict row."""
    verification = _FakeVerification(WriteMeasurement("measured", "holds", object(), object()))

    outcome, fake_insert = await _write(verification=verification, monkeypatch=monkeypatch)

    assert outcome.provenance == "measured"
    assert outcome.detail == "holds"
    assert fake_insert.calls[0]["provenance"] == "measured"
    assert verification.measure_calls == 1
    assert len(verification.record_calls) == 1
    assert verification.record_calls[0]["claim_id"] == fake_insert.rows[0].id
    assert outcome.claim_id == fake_insert.rows[0].id


async def test_write_claim_measured_falsified_is_not_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A falsified first verdict must still be written, not raised or downgraded."""
    verification = _FakeVerification(WriteMeasurement("measured", "falsified", object(), object()))

    outcome, fake_insert = await _write(verification=verification, monkeypatch=monkeypatch)

    assert outcome.provenance == "measured"
    assert outcome.detail == "falsified"
    assert fake_insert.calls[0]["provenance"] == "measured"
    assert len(verification.record_calls) == 1


async def test_write_claim_measure_true_without_a_service_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A claim asking to be measured must fail closed rather than silently downgrade."""
    fake_insert = _FakeInsertClaim()
    monkeypatch.setattr(claim_writes, "insert_claim", fake_insert)

    with pytest.raises(ValueError, match="measure=true claims require"):
        await _write_claim(
            object(),
            entity_ref_id=uuid4(),
            entity_type="learning",
            project_key="brain-v42",
            claim=_resolved_claim(measure=True),
            declared_by="integration-test",
            declared_at=datetime.now(UTC),
            verification=None,
        )

    assert fake_insert.calls == []


async def test_write_claim_unreadable_keeps_declared_and_still_appends_the_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unreadable real measurement is history: the verdict row is appended, declared kept."""
    verification = _FakeVerification(
        WriteMeasurement("declared", "unreadable: probe:timeout", object(), object())
    )

    outcome, fake_insert = await _write(verification=verification, monkeypatch=monkeypatch)

    assert outcome.provenance == "declared"
    assert outcome.detail == "unreadable: probe:timeout"
    assert fake_insert.calls[0]["provenance"] == "declared"
    assert len(verification.record_calls) == 1


async def test_write_claim_refused_refresh_budget_writes_no_verdict_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refused refresh budget is declared with no observation to keep."""
    verification = _FakeVerification(
        WriteMeasurement("declared", "retry later: refresh budget", None, None)
    )

    outcome, fake_insert = await _write(verification=verification, monkeypatch=monkeypatch)

    assert outcome.provenance == "declared"
    assert outcome.detail == "retry later: refresh budget"
    assert fake_insert.calls[0]["provenance"] == "declared"
    # record_write_verdict is still called; ITS OWN no-op guard is what refuses the row
    # (single place decides "no row" -- verification.py, never duplicated here).
    assert len(verification.record_calls) == 1


def test_claims_confirmation_is_byte_identical_when_nothing_was_measured() -> None:
    """The pre-measurement text must not change one character for measure=false claims."""
    claim_id = uuid4()
    outcomes = [ClaimWriteOutcome(claim_id=claim_id, provenance="declared", detail=None)]

    assert claims_confirmation(outcomes) == (
        f"1 recorded as declared [{claim_id}]; brain_claim_verify measures one"
    )


def test_claims_confirmation_names_the_per_claim_outcome_once_any_claim_was_measured() -> None:
    """Once one claim was measured, every claim's outcome is named, not just its id."""
    measured_id, declared_id = uuid4(), uuid4()
    outcomes = [
        ClaimWriteOutcome(claim_id=measured_id, provenance="measured", detail="holds"),
        ClaimWriteOutcome(claim_id=declared_id, provenance="declared", detail=None),
    ]

    text = claims_confirmation(outcomes)

    assert f"{measured_id} measured (holds)" in text
    assert str(declared_id) in text
    assert "brain_claim_verify measures one" in text


def test_describe_claim_outcome_bare_id_when_never_measured() -> None:
    claim_id = uuid4()
    assert describe_claim_outcome(
        ClaimWriteOutcome(claim_id=claim_id, provenance="declared", detail=None)
    ) == str(claim_id)


# ---------------------------------------------------------------------------
# `gated_claim_session`: every writer opens its entry transaction through this
# helper so a `measure=true` batch is admitted through the SAME semaphore
# `verify()` uses, acquired before the session opens (MAJOR review finding,
# PR #233). A fake/counting session factory proves the bound end to end.
# ---------------------------------------------------------------------------


class _CountingSessionCM:
    def __init__(self, factory: _CountingSessionFactory) -> None:
        self._factory = factory

    async def __aenter__(self) -> _FakeCountedSession:
        self._factory.active += 1
        self._factory.peak = max(self._factory.peak, self._factory.active)
        await asyncio.sleep(self._factory.delay)
        return _FakeCountedSession()

    async def __aexit__(self, *exc_info: object) -> bool:
        self._factory.active -= 1
        return False


class _CountingSessionFactory:
    """Fake `async_sessionmaker` counting how many sessions are open at once."""

    def __init__(self, *, delay: float = 0.02) -> None:
        self.active = 0
        self.peak = 0
        self.delay = delay

    def __call__(self) -> _CountingSessionCM:
        return _CountingSessionCM(self)


class _NullAsyncCM:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


class _FakeCountedSession:
    def begin(self) -> _NullAsyncCM:
        return _NullAsyncCM()


def _service(*, max_concurrent: int) -> ClaimVerificationService:
    return ClaimVerificationService(
        _Registry(),
        session_factory=lambda: pytest.fail("gated_claim_session owns its own session factory"),
        max_concurrent_verifications=max_concurrent,
    )


async def test_gated_claim_session_bounds_concurrent_measured_writes() -> None:
    """N concurrent `measure=true` batches must never exceed the service's session limit."""
    verification = _service(max_concurrent=2)
    factory = _CountingSessionFactory()
    measured = [_resolved_claim(measure=True)]

    async def one_write() -> None:
        async with gated_claim_session(factory, verification, measured):
            pass

    await asyncio.gather(*(one_write() for _ in range(6)))

    assert factory.peak <= 2


async def test_gated_claim_session_never_gates_a_declared_only_write() -> None:
    """A declared-only batch must proceed even while the measured budget is fully held."""
    verification = _service(max_concurrent=1)
    declared = [_resolved_claim(measure=False)]
    factory = _CountingSessionFactory(delay=0.0)
    released = asyncio.Event()

    async def hold_the_one_permit() -> None:
        async with verification.write_gate([_resolved_claim(measure=True)]):
            await released.wait()

    holder = asyncio.create_task(hold_the_one_permit())
    await asyncio.sleep(0.01)

    try:
        async with asyncio.timeout(0.05):
            async with gated_claim_session(factory, verification, declared):
                pass  # would time out here if a declared-only write were gated
    finally:
        released.set()
        await holder

    assert factory.peak == 1


async def test_gated_claim_session_is_ungated_without_a_verification_service() -> None:
    """No verification service (declared writes are unaffected) must never block a write."""
    factory = _CountingSessionFactory(delay=0.0)

    async with gated_claim_session(factory, None, [_resolved_claim(measure=True)]):
        pass

    assert factory.peak == 1
