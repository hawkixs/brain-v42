"""Coordinate one server-owned fact observation into an immutable claim verdict."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager, nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal, cast
from uuid import UUID, uuid4

import structlog

from brain_v42.facts.claims import ResolvedClaim
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

logger = structlog.get_logger(__name__)

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


def _reject_if_refresh_budget_exhausted(measurement: Measurement) -> None:
    """Refuse cleanly instead of appending a durable verdict for an internal capacity limit.

    `FactRegistry._unreadable` mints a fresh `observation_id` on every refusal, so an
    exhausted refresh budget (`error_code="refresh_budget"`) never matches an existing
    stored observation: unchecked, `_verify` would append one unprunable ledger row per
    retry for a cause that has nothing to do with the claim itself (ticket 5c47578b).
    """
    if isinstance(measurement, Unreadable) and measurement.error_code == "refresh_budget":
        raise ClaimVerificationError("refresh_budget_exhausted")


def _comparison(
    *,
    fact_name: str,
    definition_version: int,
    target: str,
    expected_resolved: Mapping[str, object],
    registry: FactRegistry,
    measurement: Measurement,
) -> Comparison:
    """Reject mismatched metadata before comparison can turn it into a false verdict.

    Shared by `_verify` (an existing, locked `ScopedClaim`) and the write-time path
    (a `ResolvedClaim` that has no row yet): both pass the same four immutable fields,
    so there is exactly one place that decides `holds` / `falsified` / `unreadable`.
    """
    if measurement.fact != fact_name:
        return Comparison("unreadable", "fact_mismatch")
    if measurement.definition_version != definition_version:
        return Comparison("unreadable", "definition_changed")
    if measurement.target.value != target:
        return Comparison("unreadable", "target_mismatch")
    if isinstance(measurement, Unreadable) and measurement.error_code == "definition_drift":
        return Comparison("unreadable", "definition_changed")
    if isinstance(measurement, Measured):
        expected = registry.expected_identity(measurement.target)
        if expected is None or measurement.source != expected:
            return Comparison("unreadable", "target_mismatch")
    return compare(expected_resolved, measurement)


@dataclass(frozen=True, slots=True)
class WriteMeasurement:
    """The AMENDED write-time result (spec 2026-09-19 section 6.3, order amended 2026-09-26).

    `measurement`/`comparison` are `None` exactly when no verdict row may ever be
    appended: a refused refresh budget or an unexpected measurement error. Both leave
    the claim `declared` with no server observation kept. An `unreadable` real
    measurement is different: it IS a server observation, so both fields carry it and
    `record_write_verdict` appends it -- provenance stays `declared` because it is not
    conclusive.
    """

    provenance: Literal["measured", "declared"]
    detail: str
    measurement: Measurement | None
    comparison: Comparison | None


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

    def write_gate(self, resolved: Sequence[ResolvedClaim]) -> AbstractAsyncContextManager[None]:
        """Admission gate a write-time caller holds BEFORE opening its own entry transaction.

        MAJOR review finding (PR #233): a `measure=true` writer already keeps a DB
        transaction open across `measure_for_write`, entirely outside `verify()`'s
        owned-session semaphore -- so N concurrent slow measured writes could exhaust
        the pool on their own, independently of the budget above. The caller acquires
        this gate first, then opens `session_factory()` and holds the gate through
        BOTH measurement and persistence, sharing the exact same limit.

        A batch where no claim asks to be measured (`claim.measure` all falsy, or
        the batch is empty) gets a free `nullcontext`: a purely declared write never
        contends with `verify()` for this budget, unaffected by this admission
        control (spec 2026-09-19 section 6.3 contract: absent/false measure changes
        nothing).
        """
        if not any(claim.measure for claim in resolved):
            return nullcontext()
        return self._measured_write_gate()

    @asynccontextmanager
    async def _measured_write_gate(self) -> AsyncIterator[None]:
        async with self._verification_semaphore:
            yield

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
        _reject_if_refresh_budget_exhausted(measurement)
        if await lookup_observation(session, claim.id, measurement.observation_id) is not None:
            measurement = await self._measure(claim, force_fresh=True)
            _reject_if_refresh_budget_exhausted(measurement)
            if await lookup_observation(session, claim.id, measurement.observation_id) is not None:
                raise ClaimVerificationError("observation_already_verified")

        if measurement.measured_at > self._clock() + _FUTURE_TOLERANCE:
            raise ClaimVerificationError("invalid_emitted_at")
        comparison = _comparison(
            fact_name=claim.fact_name,
            definition_version=claim.definition_version,
            target=claim.target,
            expected_resolved=claim.expected_resolved,
            registry=self._registry,
            measurement=measurement,
        )
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

    async def measure_for_write(self, resolved: ResolvedClaim) -> WriteMeasurement:
        """Measure and compare a claim that has NO row yet (AMENDED order, plan 2026-09-26).

        Migration 055's `knowledge_claims_update_gate` forbids any `provenance` update, so
        the caller cannot insert `declared` and upgrade it later: this measures FIRST, and
        the caller inserts the occurrence with the provenance this method already decided.
        Nothing is looked up or locked -- there is no claim id to look up yet.

        Shares `_comparison` with `_verify`: this is the only place besides `_verify` that
        may decide `holds` / `falsified` / `unreadable`.
        """
        try:
            measurement = await self._registry.measure(
                resolved.fact_name, max_age=timedelta(seconds=resolved.validity_seconds)
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            # Bounded on purpose: no probe payload, no DB text, one structured event.
            # A measurement failure here must never surface an internal detail through
            # the claim confirmation text (spec 2026-09-19 section 6.3).
            logger.warning(
                "brain_v42.claim_write.measurement_error",
                fact_name=resolved.fact_name,
            )
            return WriteMeasurement("declared", "unexpected error", None, None)

        try:
            _reject_if_refresh_budget_exhausted(measurement)
        except ClaimVerificationError:
            return WriteMeasurement("declared", "retry later: refresh budget", None, None)

        comparison = _comparison(
            fact_name=resolved.fact_name,
            definition_version=resolved.definition_version,
            target=resolved.target.value,
            expected_resolved=resolved.expected_resolved,
            registry=self._registry,
            measurement=measurement,
        )
        if comparison.verdict == "unreadable":
            reason = comparison.reason or "unreadable"
            return WriteMeasurement("declared", f"unreadable: {reason}", measurement, comparison)
        return WriteMeasurement("measured", comparison.verdict, measurement, comparison)

    async def record_write_verdict(
        self,
        session: AsyncSession,
        *,
        claim_id: UUID,
        resolved: ResolvedClaim,
        write_measurement: WriteMeasurement,
        issuer_identity: str,
        issuer_kind: Literal["robot", "human"],
    ) -> VerdictRow | None:
        """Append the first verdict of a claim just inserted in the caller's own transaction.

        INTERNAL: never registered as an MCP tool, and never accepts a caller-supplied
        measurement -- `write_measurement` was produced by `measure_for_write` earlier in
        the SAME request, from the registry, not from an argument. A no-op (no row) when
        that step kept no measurement: a refused refresh budget or an unexpected error.
        The idempotency key is derived from the fresh `claim_id`
        (`write:<claim_id>`): it can only ever collide with a retry inside this same
        transaction, never across requests, because `claim_id` did not exist before it.
        """
        if write_measurement.measurement is None or write_measurement.comparison is None:
            return None
        measurement = write_measurement.measurement
        comparison = write_measurement.comparison
        idempotency_key = f"write:{claim_id}"
        request = request_fingerprint(
            claim_id=claim_id,
            issuer_identity=issuer_identity,
            idempotency_key=idempotency_key,
            expected_resolved=resolved.expected_resolved,
            definition_version=resolved.definition_version,
            validity_seconds=resolved.validity_seconds,
        )
        return await append_verdict(
            session,
            claim_id=claim_id,
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
