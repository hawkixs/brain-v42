"""Unit contracts for durable, server-owned claim verification."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast
from uuid import UUID, uuid4

import pytest

from brain_v42.facts.claims import ResolvedClaim, resolve_claim
from brain_v42.facts.model import FactTarget, Measured, Measurement, SourceIdentity, Unreadable
from brain_v42.facts.registry import FactRegistry
from brain_v42.facts.verification import ClaimVerificationService, WriteMeasurement
from brain_v42.models.claim_input import ClaimInput
from brain_v42.models.claim_verdict import ClaimVerificationError
from brain_v42.repositories.pg_claim_verdicts import ScopedClaim, VerdictRow


class _Probe:
    name = "verification_lag"
    definition_version = 1
    target = FactTarget.PRODUCTION
    ttl = timedelta(seconds=30)
    timeout = timedelta(seconds=1)
    briefing = False
    policies: dict[str, int] = {}
    value_schema = {"lag": "int"}

    def __init__(self, *, value: int = 3, definition_version: int = 1) -> None:
        self.value = value
        self.definition_version = definition_version
        self.runs = 0

    async def measure(self, source: object) -> dict[str, object]:
        self.runs += 1
        return {"lag": self.value}


class _Source:
    async def identity(self) -> SourceIdentity:
        return _identity()


def _identity() -> SourceIdentity:
    return SourceIdentity("1", "brain_test", "127.0.0.1", 5432)


def _registry(probe: _Probe) -> FactRegistry:
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def source():
        yield _Source()

    registry = FactRegistry(
        sources={FactTarget.PRODUCTION: source},
        expected={FactTarget.PRODUCTION: _identity()},
    )
    registry.register(probe)
    registry.freeze()
    return registry


def _claim(*, retired: bool = False, fact: str = "verification_lag") -> ScopedClaim:
    return ScopedClaim(
        id=UUID("00000000-0000-0000-0000-000000000001"),
        project_key="project-a",
        retired_at=datetime(2026, 9, 22, tzinfo=UTC) if retired else None,
        fact_name=fact,
        definition_version=1,
        target="production",
        expected_resolved={"path": "/lag", "op": "lte", "value": 5},
        validity_seconds=600,
        definition_ttl_seconds=30,
    )


def _service(registry: FactRegistry) -> ClaimVerificationService:
    return ClaimVerificationService(
        registry, session_factory=lambda: pytest.fail("unexpected factory")
    )


def _registry_many(probes: list[_Probe]) -> FactRegistry:
    """Register several independent facts so concurrent verifications never share a cache slot."""
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def source():
        yield _Source()

    registry = FactRegistry(
        sources={FactTarget.PRODUCTION: source},
        expected={FactTarget.PRODUCTION: _identity()},
    )
    for probe in probes:
        registry.register(probe)
    registry.freeze()
    return registry


def _claim_indexed(index: int) -> ScopedClaim:
    """One distinct claim id and fact per index, so concurrent verifications never collide."""
    return ScopedClaim(
        id=UUID(int=index + 1),
        project_key="project-a",
        retired_at=None,
        fact_name=f"verification_lag_{index}",
        definition_version=1,
        target="production",
        expected_resolved={"path": "/lag", "op": "lte", "value": 5},
        validity_seconds=600,
        definition_ttl_seconds=30,
    )


class _NullAsyncContext:
    """A no-op async context manager standing in for `AsyncSession.begin()`."""

    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


class _FakeOwnedSession:
    def begin(self) -> _NullAsyncContext:
        return _NullAsyncContext()


class _CountingSessionFactory:
    """Fake `async_sessionmaker` that records how many owned sessions are open at once.

    The delay lives INSIDE `__aenter__`, while the service's semaphore is still held:
    this is what turns "several verifications race" into genuine overlap, instead of
    a sequence of calls that never actually run concurrently.
    """

    def __init__(self, *, delay: float = 0.02) -> None:
        self.active = 0
        self.peak = 0
        self.delay = delay

    def __call__(self) -> _CountingSessionCM:
        return _CountingSessionCM(self)


class _CountingSessionCM:
    def __init__(self, factory: _CountingSessionFactory) -> None:
        self._factory = factory

    async def __aenter__(self) -> _FakeOwnedSession:
        self._factory.active += 1
        self._factory.peak = max(self._factory.peak, self._factory.active)
        await asyncio.sleep(self._factory.delay)
        return _FakeOwnedSession()

    async def __aexit__(self, *exc_info: object) -> bool:
        self._factory.active -= 1
        return False


async def _memory_repository(
    monkeypatch: pytest.MonkeyPatch, claim: ScopedClaim | None
) -> list[VerdictRow]:
    """Keep SQL effects at the repository boundary while retaining the real registry."""
    from brain_v42.facts import verification

    rows: list[VerdictRow] = []

    async def lookup_locked_claim(session: object, claim_id: UUID, project_key: str | None):
        if (
            claim is not None
            and claim.id == claim_id
            and (project_key is None or project_key == claim.project_key)
        ):
            return claim
        return None

    async def lookup_request(session: object, **values: object):
        for row in rows:
            if (
                row.claim_id == values["claim_id"]
                and row.issuer_identity == values["issuer_identity"]
                and row.idempotency_key == values["idempotency_key"]
            ):
                return row
        return None

    async def lookup_observation(session: object, claim_id: UUID, observation_id: UUID):
        return next(
            (
                row
                for row in rows
                if row.claim_id == claim_id and row.observation_id == observation_id
            ),
            None,
        )

    async def append_verdict(session: object, **values: object) -> VerdictRow:
        row = VerdictRow(
            id=uuid4(),
            seq=len(rows) + 1,
            claim_id=values["claim_id"],
            verdict=values["verdict"],
            reason=values["reason"],
            measurement=values["measurement"],
            measurement_digest=values["measurement_digest"],
            observation_id=values["observation_id"],
            issuer_identity=values["issuer_identity"],
            issuer_kind=values["issuer_kind"],
            request_fingerprint=values["request_fingerprint"],
            outcome_fingerprint=values["outcome_fingerprint"],
            idempotency_key=values["idempotency_key"],
            emitted_at=values["emitted_at"],
            recorded_at=datetime(2026, 9, 22, tzinfo=UTC),
        )
        rows.append(row)
        return row

    monkeypatch.setattr(verification, "lookup_locked_claim", lookup_locked_claim)
    monkeypatch.setattr(verification, "lookup_request", lookup_request)
    monkeypatch.setattr(verification, "lookup_observation", lookup_observation)
    monkeypatch.setattr(verification, "append_verdict", append_verdict)
    return rows


async def test_invalid_issuer_is_refused_before_opening_a_transaction() -> None:
    """Removing preflight validation would let malformed provenance allocate a DB transaction."""
    service = _service(_registry(_Probe()))

    with pytest.raises(ClaimVerificationError, match="invalid verification argument") as error:
        await service.verify(
            _claim().id,
            issuer_identity=" ",
            issuer_kind="robot",
            idempotency_key="request-1",
        )

    assert error.value.code == "invalid_argument"


async def test_scope_refusal_does_not_probe_or_reveal_an_existing_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dropping scope from the locked query would expose another project's verdict."""
    probe = _Probe()
    service = _service(_registry(probe))
    await _memory_repository(monkeypatch, _claim())

    with pytest.raises(ClaimVerificationError) as error:
        await service.verify(
            _claim().id,
            issuer_identity="mcp:codex",
            issuer_kind="robot",
            idempotency_key="request-1",
            project_key="project-b",
            session=SimpleNamespace(),
        )

    assert error.value.code == "claim_not_found"
    assert probe.runs == 0


async def test_retired_claim_is_refused_without_a_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Checking retirement after measurement would spend evidence on a closed occurrence."""
    probe = _Probe()
    service = _service(_registry(probe))
    await _memory_repository(monkeypatch, _claim(retired=True))

    with pytest.raises(ClaimVerificationError) as error:
        await service.verify(
            _claim().id,
            issuer_identity="mcp:codex",
            issuer_kind="robot",
            idempotency_key="request-1",
            session=SimpleNamespace(),
        )

    assert error.value.code == "claim_retired"
    assert probe.runs == 0


async def test_exact_replay_returns_the_persisted_row_without_a_second_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Moving replay after measure would repeat a server-owned observation."""
    probe = _Probe()
    service = _service(_registry(probe))
    rows = await _memory_repository(monkeypatch, _claim())

    first = await service.verify(
        _claim().id,
        issuer_identity="mcp:codex",
        issuer_kind="robot",
        idempotency_key="request-1",
        session=SimpleNamespace(),
    )
    replay = await service.verify(
        _claim().id,
        issuer_identity="mcp:codex",
        issuer_kind="robot",
        idempotency_key="request-1",
        session=SimpleNamespace(),
    )

    assert replay == first
    assert len(rows) == 1
    assert probe.runs == 1


async def test_definition_mismatch_persists_unreadable_with_the_historical_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Using the live descriptor TTL would rewrite the evidence meaning of old claims."""
    probe = _Probe(definition_version=2)
    service = _service(_registry(probe))
    rows = await _memory_repository(monkeypatch, _claim())

    verdict = await service.verify(
        _claim().id,
        issuer_identity="mcp:codex",
        issuer_kind="robot",
        idempotency_key="request-1",
        session=SimpleNamespace(),
    )

    assert verdict.verdict == "unreadable"
    assert verdict.reason == "definition_changed"
    assert verdict.measurement["ttl_seconds"] == 30
    assert verdict.measurement_digest is None
    assert rows == [verdict]
    assert probe.runs == 0


async def test_removed_definition_is_unreadable_with_the_historical_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Looking up only the current catalogue would make removed definitions unverifiable history."""
    service = _service(_registry(_Probe()))
    removed = _claim(fact="removed_definition")
    rows = await _memory_repository(monkeypatch, removed)

    verdict = await service.verify(
        removed.id,
        issuer_identity="mcp:codex",
        issuer_kind="robot",
        idempotency_key="request-1",
        session=SimpleNamespace(),
    )

    assert verdict.verdict == "unreadable"
    assert verdict.reason == "definition_changed"
    assert verdict.measurement["ttl_seconds"] == 30
    assert verdict.measurement_digest is None
    assert rows == [verdict]


async def test_future_measurement_is_refused_before_it_is_persisted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Accepting arbitrary future instants would corrupt server-sequence validity reads."""
    registry = _registry(_Probe())
    service = ClaimVerificationService(
        registry,
        session_factory=lambda: pytest.fail("unexpected factory"),
        clock=lambda: datetime(2026, 9, 22, tzinfo=UTC),
    )
    rows = await _memory_repository(monkeypatch, _claim())

    async def future_measurement(name: str, *, max_age: timedelta):
        return Measured.from_value(
            fact="verification_lag",
            definition_version=1,
            target=FactTarget.PRODUCTION,
            source=_identity(),
            value={"lag": 3},
            observation_id=uuid4(),
            measured_at=datetime(2026, 9, 22, 0, 1, 1, tzinfo=UTC),
            duration_ms=1,
            ttl_seconds=30,
        )

    monkeypatch.setattr(registry, "measure", future_measurement)
    with pytest.raises(ClaimVerificationError) as error:
        await service.verify(
            _claim().id,
            issuer_identity="mcp:codex",
            issuer_kind="robot",
            idempotency_key="request-1",
            session=SimpleNamespace(),
        )

    assert error.value.code == "invalid_emitted_at"
    assert rows == []


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"fact": "other_fact"}, "fact_mismatch"),
        ({"definition_version": 2}, "definition_changed"),
        ({"target": FactTarget.HOST}, "target_mismatch"),
        (
            {"source": SourceIdentity("2", "brain_test", "127.0.0.1", 5432)},
            "target_mismatch",
        ),
    ],
)
async def test_mismatched_measurement_metadata_is_unreadable_before_comparison(
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, object],
    reason: str,
) -> None:
    """Skipping any metadata check could falsely compare evidence from another fact or source."""
    registry = _registry(_Probe())
    service = _service(registry)
    await _memory_repository(monkeypatch, _claim())

    async def mismatched_measurement(name: str, *, max_age: timedelta) -> Measured:
        fields: dict[str, object] = {
            "fact": "verification_lag",
            "definition_version": 1,
            "target": FactTarget.PRODUCTION,
            "source": _identity(),
            "value": {"lag": 3},
            "observation_id": uuid4(),
            "measured_at": datetime(2026, 9, 22, tzinfo=UTC),
            "duration_ms": 1,
            "ttl_seconds": 30,
        }
        fields.update(overrides)
        return Measured.from_value(**fields)  # type: ignore[arg-type]

    monkeypatch.setattr(registry, "measure", mismatched_measurement)
    verdict = await service.verify(
        _claim().id,
        issuer_identity="mcp:codex",
        issuer_kind="robot",
        idempotency_key="metadata",
        session=SimpleNamespace(),
    )

    assert verdict.verdict == "unreadable"
    assert verdict.reason == reason


async def test_duplicate_observation_gets_one_fresh_measurement_then_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unbounded retry loop would let duplicate cache observations exhaust the probe budget."""
    probe = _Probe()
    registry = _registry(probe)
    service = _service(registry)
    rows = await _memory_repository(monkeypatch, _claim())
    observation_id = UUID("00000000-0000-0000-0000-000000000099")
    ages: list[timedelta] = []

    async def repeated_measurement(name: str, *, max_age: timedelta):
        ages.append(max_age)
        return Unreadable(
            fact="verification_lag",
            definition_version=1,
            target=FactTarget.PRODUCTION,
            error_code="timeout",
            where=None,
            observation_id=observation_id,
            measured_at=datetime(2026, 9, 22, tzinfo=UTC),
            duration_ms=1,
            ttl_seconds=30,
            source_kind="probe",
        )

    monkeypatch.setattr(registry, "measure", repeated_measurement)
    existing = await service.verify(
        _claim().id,
        issuer_identity="mcp:codex",
        issuer_kind="robot",
        idempotency_key="first",
        session=SimpleNamespace(),
    )
    assert existing.observation_id == observation_id

    with pytest.raises(ClaimVerificationError) as error:
        await service.verify(
            _claim().id,
            issuer_identity="mcp:codex",
            issuer_kind="robot",
            idempotency_key="second",
            session=SimpleNamespace(),
        )

    assert error.value.code == "observation_already_verified"
    assert len(rows) == 1
    # The first verdict reads the cache. The duplicate reads it too, gets exactly ONE
    # forced fresh measurement, then refuses -- neither an unbounded retry nor a
    # refusal without trying.
    assert ages == [timedelta(seconds=600), timedelta(seconds=600), timedelta(0)]


@pytest.mark.parametrize("change", ["removed", "version_moved"])
async def test_a_catalogue_refusal_says_that_no_probe_ran(
    monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    """A removed or re-versioned definition is refused by the catalogue, not by a probe.

    The observation keeps the registry's own refusal convention -- `source_kind`
    stays in its closed `probe`/`cache` vocabulary, `duration_ms` is 0, as for a
    disabled fact or an exhausted refresh budget -- and names its origin in `where`,
    so an audit of the ledger cannot count it as a probe attempt that never ran.
    """
    probe = _Probe(definition_version=2) if change == "version_moved" else _Probe()
    service = _service(_registry(probe))
    claim = _claim(fact="removed_definition") if change == "removed" else _claim()
    await _memory_repository(monkeypatch, claim)

    verdict = await service.verify(
        claim.id,
        issuer_identity="mcp:codex",
        issuer_kind="robot",
        idempotency_key="request-1",
        session=SimpleNamespace(),
    )

    assert verdict.measurement["error_code"] == "definition_drift"
    assert verdict.measurement["where"] == "catalogue"
    assert verdict.measurement["duration_ms"] == 0
    assert probe.runs == 0


def test_max_concurrent_verifications_must_be_a_positive_integer() -> None:
    """A zero or negative limit would make every owned-session verification block forever."""
    registry = _registry(_Probe())

    with pytest.raises(ValueError, match="max_concurrent_verifications"):
        ClaimVerificationService(
            registry,
            session_factory=lambda: pytest.fail("unexpected factory"),
            max_concurrent_verifications=0,
        )


async def test_verify_bounds_concurrent_owned_sessions_below_the_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A semaphore acquired before opening any session keeps concurrent owned sessions
    at the configured limit, however many verifications race at once.

    Ticket 8acd4698: without it, N concurrent `verify()` calls with no caller-supplied
    session each open a connection from the pool at once; ~30 concurrent verifications
    against the production pool (20 + 10 overflow) saturate it, and the probe records a
    durable `Unreadable probe:*` verdict for a purely internal capacity cause.
    """
    from brain_v42.facts import verification

    count = 6
    limit = 2
    claims = {index: _claim_indexed(index) for index in range(count)}

    async def lookup_locked_claim(session: object, claim_id: UUID, project_key: str | None):
        return next((claim for claim in claims.values() if claim.id == claim_id), None)

    async def lookup_request(session: object, **values: object):
        return None

    async def lookup_observation(session: object, claim_id: UUID, observation_id: UUID):
        return None

    appended: list[VerdictRow] = []

    async def append_verdict(session: object, **values: object) -> VerdictRow:
        row = VerdictRow(
            id=uuid4(),
            seq=len(appended) + 1,
            claim_id=cast(UUID, values["claim_id"]),
            verdict=values["verdict"],
            reason=values["reason"],
            measurement=values["measurement"],
            measurement_digest=values["measurement_digest"],
            observation_id=values["observation_id"],
            issuer_identity=values["issuer_identity"],
            issuer_kind=values["issuer_kind"],
            request_fingerprint=values["request_fingerprint"],
            outcome_fingerprint=values["outcome_fingerprint"],
            idempotency_key=values["idempotency_key"],
            emitted_at=values["emitted_at"],
            recorded_at=datetime(2026, 9, 22, tzinfo=UTC),
        )
        appended.append(row)
        return row

    monkeypatch.setattr(verification, "lookup_locked_claim", lookup_locked_claim)
    monkeypatch.setattr(verification, "lookup_request", lookup_request)
    monkeypatch.setattr(verification, "lookup_observation", lookup_observation)
    monkeypatch.setattr(verification, "append_verdict", append_verdict)

    probes = []
    for index in range(count):
        probe = _Probe()
        probe.name = f"verification_lag_{index}"
        probes.append(probe)
    registry = _registry_many(probes)

    factory = _CountingSessionFactory()
    service = ClaimVerificationService(
        registry,
        session_factory=factory,
        max_concurrent_verifications=limit,  # type: ignore[arg-type]
    )

    results = await asyncio.gather(
        *(
            service.verify(
                claims[index].id,
                issuer_identity="mcp:codex",
                issuer_kind="robot",
                idempotency_key=f"concurrent-{index}",
            )
            for index in range(count)
        )
    )

    assert len(results) == count
    assert len(appended) == count
    assert factory.peak <= limit
    assert factory.active == 0
    assert all(row.verdict != "unreadable" for row in appended)


async def test_refresh_budget_exhaustion_is_refused_without_writing_a_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A registry refusal for an exhausted refresh budget must not become a permanent verdict.

    Ticket 5c47578b: `FactRegistry._unreadable` mints a fresh `observation_id` on every
    call, so the `observation_already_verified` guard can never catch a `refresh_budget`
    refusal -- without an explicit check, `append_verdict` would insert one unprunable
    row per retry for a purely internal capacity limit.
    """
    probe = _Probe()
    registry = _registry(probe)
    service = _service(registry)
    rows = await _memory_repository(monkeypatch, _claim())

    async def exhausted_measurement(name: str, *, max_age: timedelta) -> Unreadable:
        return Unreadable(
            fact="verification_lag",
            definition_version=1,
            target=FactTarget.PRODUCTION,
            error_code="refresh_budget",
            where=None,
            observation_id=uuid4(),
            measured_at=datetime(2026, 9, 22, tzinfo=UTC),
            duration_ms=0,
            ttl_seconds=30,
            source_kind="probe",
        )

    monkeypatch.setattr(registry, "measure", exhausted_measurement)

    with pytest.raises(ClaimVerificationError) as error:
        await service.verify(
            _claim().id,
            issuer_identity="mcp:codex",
            issuer_kind="robot",
            idempotency_key="fresh-key",
            session=SimpleNamespace(),
        )

    assert error.value.code == "refresh_budget_exhausted"
    assert rows == []
    assert probe.runs == 0


# ---------------------------------------------------------------------------
# `measure_for_write` / `record_write_verdict` (spec 2026-09-19 section 6.3
# last paragraph, order AMENDED 2026-09-26): a claim measured before any row
# exists, then a first verdict appended in the caller's own transaction.
# ---------------------------------------------------------------------------


def _resolved_claim(registry: FactRegistry, *, fact: str = "verification_lag") -> ResolvedClaim:
    """One live-resolved claim, exactly as a writer would produce moments before insert."""
    descriptor = registry.describe(fact)
    claim_input = ClaimInput(
        statement="The measured lag stays low.",
        fact_name=fact,
        expected={"path": "/lag", "op": "lte", "value": 5},
        measure=True,
    )
    return resolve_claim(claim_input, descriptor)


async def test_measure_for_write_holds_is_measured_with_the_comparison_kept() -> None:
    """A conclusive `holds` verdict must produce provenance `measured`, ready to append."""
    probe = _Probe(value=3)
    registry = _registry(probe)
    service = _service(registry)
    resolved = _resolved_claim(registry)

    result = await service.measure_for_write(resolved)

    assert result.provenance == "measured"
    assert result.detail == "holds"
    assert result.measurement is not None
    assert result.comparison is not None
    assert result.comparison.verdict == "holds"


async def test_measure_for_write_falsified_is_measured_not_refused() -> None:
    """A `falsified` verdict is still a conclusive measurement -- the write is not refused."""
    probe = _Probe(value=99)
    registry = _registry(probe)
    service = _service(registry)
    resolved = _resolved_claim(registry)

    result = await service.measure_for_write(resolved)

    assert result.provenance == "measured"
    assert result.detail == "falsified"
    assert result.comparison is not None
    assert result.comparison.verdict == "falsified"


def _registry_with_wrong_expected_identity(probe: _Probe) -> FactRegistry:
    """Diverge the expected source identity so a real measurement is `target_mismatch`."""
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def source():
        yield _Source()

    registry = FactRegistry(
        sources={FactTarget.PRODUCTION: source},
        expected={FactTarget.PRODUCTION: SourceIdentity("1", "brain_test", "127.0.0.1", 9999)},
    )
    registry.register(probe)
    registry.freeze()
    return registry


async def test_measure_for_write_unreadable_stays_declared_but_keeps_the_measurement() -> None:
    """An unreadable result downgrades to `declared`, but the observation is real and kept."""
    probe = _Probe(value=3)
    registry = _registry_with_wrong_expected_identity(probe)
    service = _service(registry)
    resolved = _resolved_claim(registry)

    result = await service.measure_for_write(resolved)

    assert result.provenance == "declared"
    assert result.detail == "unreadable: probe:target_mismatch"
    assert result.measurement is not None
    assert result.comparison is not None
    assert result.comparison.verdict == "unreadable"


async def test_measure_for_write_refused_refresh_budget_keeps_no_measurement() -> None:
    """A refused refresh budget must not leak into a stored observation."""
    probe = _Probe()
    registry = _registry(probe)
    service = _service(registry)
    resolved = _resolved_claim(registry)

    async def exhausted_measurement(name: str, *, max_age: timedelta) -> Unreadable:
        return Unreadable(
            fact="verification_lag",
            definition_version=1,
            target=FactTarget.PRODUCTION,
            error_code="refresh_budget",
            where=None,
            observation_id=uuid4(),
            measured_at=datetime(2026, 9, 22, tzinfo=UTC),
            duration_ms=0,
            ttl_seconds=30,
            source_kind="probe",
        )

    monkeypatch_registry_measure = exhausted_measurement
    registry.measure = monkeypatch_registry_measure  # type: ignore[method-assign]

    result = await service.measure_for_write(resolved)

    assert result.provenance == "declared"
    assert result.detail == "retry later: refresh budget"
    assert result.measurement is None
    assert result.comparison is None


async def test_measure_for_write_unexpected_error_keeps_no_measurement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unexpected registry failure never reaches the caller as an exception at write time."""
    probe = _Probe()
    registry = _registry(probe)
    service = _service(registry)
    resolved = _resolved_claim(registry)

    async def boom(name: str, *, max_age: timedelta) -> Measurement:
        raise RuntimeError("network blip")

    registry.measure = boom  # type: ignore[method-assign]

    result = await service.measure_for_write(resolved)

    assert result.provenance == "declared"
    assert result.detail == "unexpected error"
    assert result.measurement is None
    assert result.comparison is None


async def test_measure_for_write_propagates_cancellation() -> None:
    """A cancelled write must not be swallowed into a false `declared` outcome."""
    probe = _Probe()
    registry = _registry(probe)
    service = _service(registry)
    resolved = _resolved_claim(registry)

    async def cancelled(name: str, *, max_age: timedelta) -> Measurement:
        raise asyncio.CancelledError

    registry.measure = cancelled  # type: ignore[method-assign]

    with pytest.raises(asyncio.CancelledError):
        await service.measure_for_write(resolved)


async def test_record_write_verdict_appends_a_row_keyed_by_the_new_claim_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first verdict of a freshly inserted claim is appended with a `write:` key."""
    probe = _Probe(value=3)
    registry = _registry(probe)
    service = _service(registry)
    resolved = _resolved_claim(registry)
    rows = await _memory_repository(monkeypatch, None)
    write_measurement = await service.measure_for_write(resolved)
    claim_id = uuid4()

    row = await service.record_write_verdict(
        SimpleNamespace(),
        claim_id=claim_id,
        resolved=resolved,
        write_measurement=write_measurement,
        issuer_identity="integration-test",
        issuer_kind="robot",
    )

    assert row is not None
    assert row.claim_id == claim_id
    assert row.idempotency_key == f"write:{claim_id}"
    assert row.verdict == "holds"
    assert rows == [row]


async def test_record_write_verdict_is_a_no_op_when_nothing_was_measured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refused or errored write must never append a verdict row."""
    probe = _Probe()
    registry = _registry(probe)
    service = _service(registry)
    resolved = _resolved_claim(registry)
    rows = await _memory_repository(monkeypatch, None)
    refused = WriteMeasurement("declared", "retry later: refresh budget", None, None)

    row = await service.record_write_verdict(
        SimpleNamespace(),
        claim_id=uuid4(),
        resolved=resolved,
        write_measurement=refused,
        issuer_identity="integration-test",
        issuer_kind="robot",
    )

    assert row is None
    assert rows == []
