"""Coordinate one server-owned fact observation into an immutable claim verdict."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal, cast
from uuid import UUID, uuid4

from brain_v42.facts.compare import Comparison, compare
from brain_v42.facts.model import FactTarget, Measured, Measurement, Unreadable, measurement_to_json
from brain_v42.facts.registry import FactRegistry, UnknownFactError
from brain_v42.facts.verdict_fingerprints import outcome_fingerprint, request_fingerprint
from brain_v42.models.claim_verdict import ClaimVerificationError, validate_caller_string
from brain_v42.repositories.pg_claim_verdicts import (
    ScopedClaim,
    VerdictRow,
    append_verdict,
    lookup_locked_claim,
    lookup_observation,
    lookup_request,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_FUTURE_TOLERANCE = timedelta(seconds=60)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _invalid_argument() -> ClaimVerificationError:
    return ClaimVerificationError("invalid_argument")


def _validate_arguments(
    claim_id: object, issuer_identity: object, issuer_kind: object, idempotency_key: object
) -> tuple[UUID, str, Literal["robot", "human"], str]:
    if not isinstance(claim_id, UUID):
        raise _invalid_argument()
    issuer = validate_caller_string(issuer_identity)
    key = validate_caller_string(idempotency_key)
    if not isinstance(issuer_kind, str) or issuer_kind not in {"robot", "human"}:
        raise _invalid_argument()
    return claim_id, issuer, cast(Literal["robot", "human"], issuer_kind), key


def _historical_unreadable(claim: ScopedClaim, *, now: datetime) -> Unreadable:
    """Keep a removed live definition auditable with its stored historical TTL.

    No probe runs: the catalogue refuses. The observation follows the registry's own
    refusal convention (`FactRegistry._unreadable`: `source_kind="probe"`, zero
    duration, as for a disabled fact or an exhausted refresh budget) and names its
    origin in `where`, so the ledger never passes it off as a probe attempt.
    """
    return Unreadable(
        fact=claim.fact_name,
        definition_version=claim.definition_version,
        target=FactTarget(claim.target),
        error_code="definition_drift",
        where="catalogue",
        observation_id=uuid4(),
        measured_at=now,
        duration_ms=0,
        ttl_seconds=claim.definition_ttl_seconds,
        source_kind="probe",
    )


def _comparison(claim: ScopedClaim, registry: FactRegistry, measurement: Measurement) -> Comparison:
    """Reject mismatched metadata before comparison can turn it into a false verdict."""
    if measurement.fact != claim.fact_name:
        return Comparison("unreadable", "fact_mismatch")
    if measurement.definition_version != claim.definition_version:
        return Comparison("unreadable", "definition_changed")
    if measurement.target.value != claim.target:
        return Comparison("unreadable", "target_mismatch")
    if isinstance(measurement, Unreadable) and measurement.error_code == "definition_drift":
        return Comparison("unreadable", "definition_changed")
    if isinstance(measurement, Measured):
        expected = registry.expected_identity(measurement.target)
        if expected is None or measurement.source != expected:
            return Comparison("unreadable", "target_mismatch")
    return compare(claim.expected_resolved, measurement)


class ClaimVerificationService:
    """Verify one active claim in exactly the transaction owned by the caller or service."""

    def __init__(
        self,
        registry: FactRegistry,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        clock: Callable[[], datetime] = _utcnow,
        max_concurrent_verifications: int = 4,
    ) -> None:
        if type(max_concurrent_verifications) is not int or max_concurrent_verifications < 1:
            raise ValueError("max_concurrent_verifications must be a positive integer")
        self._registry = registry
        self._session_factory = session_factory
        self._clock = clock
        # Owned-session verifications each open a connection from the shared pool
        # (pool_size=20 + max_overflow=10). Bounding entry here, well below that
        # capacity, keeps a burst of callers from starving every other MCP tool of
        # connections instead of queuing safely inside this service (ticket 8acd4698).
        self._verification_semaphore = asyncio.Semaphore(max_concurrent_verifications)

    async def verify(
        self,
        claim_id: UUID,
        issuer_identity: str,
        issuer_kind: Literal["robot", "human"],
        idempotency_key: str,
        *,
        project_key: str | None = None,
        session: AsyncSession | None = None,
    ) -> VerdictRow:
        """Return a replay or append a verdict from a registry-owned observation."""
        checked_id, issuer, kind, key = _validate_arguments(
            claim_id, issuer_identity, issuer_kind, idempotency_key
        )
        if project_key is not None:
            validate_caller_string(project_key)
        if session is not None:
            return await self._verify(
                session, checked_id, issuer, kind, key, project_key=project_key
            )
        async with self._verification_semaphore:
            async with self._session_factory() as owned_session, owned_session.begin():
                return await self._verify(
                    owned_session, checked_id, issuer, kind, key, project_key=project_key
                )

    async def _verify(
        self,
        session: AsyncSession,
        claim_id: UUID,
        issuer_identity: str,
        issuer_kind: Literal["robot", "human"],
        idempotency_key: str,
        *,
        project_key: str | None,
    ) -> VerdictRow:
        claim = await lookup_locked_claim(session, claim_id, project_key)
        if claim is None:
            raise ClaimVerificationError("claim_not_found")
        if claim.retired_at is not None:
            raise ClaimVerificationError("claim_retired")
        request = request_fingerprint(
            claim_id=claim.id,
            issuer_identity=issuer_identity,
            idempotency_key=idempotency_key,
            expected_resolved=claim.expected_resolved,
            definition_version=claim.definition_version,
            validity_seconds=claim.validity_seconds,
        )
        existing = await lookup_request(
            session,
            claim_id=claim.id,
            issuer_identity=issuer_identity,
            idempotency_key=idempotency_key,
        )
        if existing is not None:
            if existing.request_fingerprint != request:
                raise ClaimVerificationError("idempotency_conflict")
            return existing

        measurement = await self._measure(claim)
        if await lookup_observation(session, claim.id, measurement.observation_id) is not None:
            measurement = await self._measure(claim, force_fresh=True)
            if await lookup_observation(session, claim.id, measurement.observation_id) is not None:
                raise ClaimVerificationError("observation_already_verified")

        if measurement.measured_at > self._clock() + _FUTURE_TOLERANCE:
            raise ClaimVerificationError("invalid_emitted_at")
        comparison = _comparison(claim, self._registry, measurement)
        return await append_verdict(
            session,
            claim_id=claim.id,
            verdict=comparison.verdict,
            reason=comparison.reason,
            measurement=measurement_to_json(measurement),
            measurement_digest=measurement.digest if isinstance(measurement, Measured) else None,
            observation_id=measurement.observation_id,
            issuer_identity=issuer_identity,
            issuer_kind=issuer_kind,
            request_fingerprint=request,
            outcome_fingerprint=outcome_fingerprint(comparison, measurement),
            idempotency_key=idempotency_key,
            emitted_at=measurement.measured_at,
        )

    async def _measure(self, claim: ScopedClaim, *, force_fresh: bool = False) -> Measurement:
        """Use the closed registry only after replay; a missing definition is historical drift."""
        try:
            descriptor = self._registry.describe(claim.fact_name)
        except UnknownFactError:
            return _historical_unreadable(claim, now=self._clock())
        if (
            descriptor.definition_version != claim.definition_version
            or descriptor.target.value != claim.target
        ):
            return _historical_unreadable(claim, now=self._clock())
        return await self._registry.measure(
            claim.fact_name,
            max_age=timedelta(0) if force_fresh else timedelta(seconds=claim.validity_seconds),
        )
