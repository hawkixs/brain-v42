"""Unit contracts for durable, server-owned claim verification."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from brain_v42.facts.model import FactTarget, Measured, SourceIdentity, Unreadable
from brain_v42.facts.registry import FactRegistry
from brain_v42.facts.verification import ClaimVerificationService
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
