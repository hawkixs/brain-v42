"""Shared declared-claim resolution and persistence for knowledge entry writers."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager, nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal
from uuid import UUID

import sqlalchemy as sa

from brain_v42.db.tables import brain_entities
from brain_v42.facts import ResolvedClaim, resolve_claim
from brain_v42.facts.registry import UnknownFactError
from brain_v42.models.claim_input import ClaimInput, validate_claim_inputs
from brain_v42.provenance import is_human_actor
from brain_v42.repositories.pg_knowledge_claims import (
    active_claims,
    insert_claim,
    latest_retired,
    retire_claims,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from brain_v42.facts.registry import FactRegistry
    from brain_v42.facts.verification import ClaimVerificationService


class ClaimMutationError(ValueError):
    """A claim PATCH refusal whose caller must roll back its enclosing entry update."""


@dataclass(frozen=True, slots=True)
class ClaimWriteOutcome:
    """The stored provenance of one persisted claim, and why, when measurement ran.

    `detail` is `None` exactly when `measure` was never requested for this claim --
    the byte-for-byte "declared, no verdict" path the contract promises stays
    distinguishable from a measurement that was attempted and still ended up
    `declared` (an unreadable result, a refused refresh budget, an unexpected error).
    """

    claim_id: UUID
    provenance: Literal["measured", "declared"]
    detail: str | None


def describe_claim_outcome(outcome: ClaimWriteOutcome) -> str:
    """One outcome's confirmation fragment: a bare id when never measured, else annotated."""
    if outcome.detail is None:
        return str(outcome.claim_id)
    return f"{outcome.claim_id} {outcome.provenance} ({outcome.detail})"


@dataclass(frozen=True, slots=True)
class ClaimReplacement:
    """Counts and generated occurrence outcomes a caller needs to describe a replacement."""

    kept: int
    created: tuple[ClaimWriteOutcome, ...]
    retired: int


def claims_confirmation(outcomes: Sequence[ClaimWriteOutcome]) -> str:
    """Name every recorded occurrence in full, and the tool that verifies one.

    `brain_claim_verify` names a claim by its canonical UUID alone, so a writer that
    only counted its claims would leave the caller nothing to verify. No comma: the
    confirmation line separates its own fields with commas.

    When no claim in the batch ever requested `measure=true`, this is BYTE FOR BYTE
    the pre-measurement text (contract: absent/false measure changes nothing). Only
    once at least one claim was actually measured does the per-claim annotated form
    appear, because that is the only case with anything new to say.
    """
    if all(outcome.detail is None for outcome in outcomes):
        ids = " ".join(str(outcome.claim_id) for outcome in outcomes)
        return f"{len(outcomes)} recorded as declared [{ids}]; brain_claim_verify measures one"
    labels = " ".join(describe_claim_outcome(outcome) for outcome in outcomes)
    return f"{len(outcomes)} recorded [{labels}]; brain_claim_verify measures one"


#: The structured-log reason when a writer's claim batch never asked to be measured.
#: Distinct from "unavailable": since `measure=true` without a service now fails
#: closed (`ValueError`), the only way to reach `declared` with no attempt is that
#: no claim in the batch requested it.
CLAIM_VERIFICATION_NOT_REQUESTED = "not_requested"


def claim_write_log_fields(outcomes: Sequence[ClaimWriteOutcome]) -> dict[str, object]:
    """Structured-log fields for one writer's claim batch, honest about what happened.

    Never claims `not_requested` once a measurement was actually attempted -- that
    field said "verification_unavailable" unconditionally before this feature existed,
    which stopped being true the moment `measure=true` could do anything.
    """
    measured = sum(1 for outcome in outcomes if outcome.provenance == "measured")
    fields: dict[str, object] = {"claim_count": len(outcomes)}
    if measured or any(outcome.detail is not None for outcome in outcomes):
        fields["measured_count"] = measured
    else:
        fields["claim_verification_reason"] = CLAIM_VERIFICATION_NOT_REQUESTED
    return fields


async def _claim_anchor_id(session: AsyncSession, entry_id: UUID, entity_type: str) -> UUID:
    """Find the ledger-created anchor so entry and claim writes share one transaction."""
    entity_ref_id = await session.scalar(
        sa.select(brain_entities.c.id).where(brain_entities.c.source_uuid == entry_id)
    )
    if not isinstance(entity_ref_id, UUID):
        raise RuntimeError(
            f"claim anchor missing for {entity_type} entry {entry_id}; source registry trigger did not fire"
        )
    return entity_ref_id


async def resolve_claim_inputs(
    registry: FactRegistry, claims: Sequence[Mapping[str, object]]
) -> list[ResolvedClaim]:
    """Resolve every input before a transaction can persist a partial declaration batch."""
    inputs = [ClaimInput.model_validate(dict(claim)) for claim in claims]
    validate_claim_inputs(inputs)

    resolved: list[ResolvedClaim] = []
    for claim in inputs:
        try:
            descriptor = registry.describe(claim.fact_name)
        except UnknownFactError as exc:
            raise ValueError(
                f"unknown fact {claim.fact_name!r}; available facts: {list(registry.names())!r}"
            ) from exc
        resolved.append(resolve_claim(claim, descriptor))
    return resolved


@asynccontextmanager
async def gated_claim_session(
    session_factory: async_sessionmaker[AsyncSession],
    verification: ClaimVerificationService | None,
    resolved: Sequence[ResolvedClaim],
) -> AsyncIterator[AsyncSession]:
    """Open one writer's entry transaction behind the write-time measurement gate.

    MAJOR review finding (PR #233): a `measure=true` writer already keeps its entry
    transaction open across `measure_for_write`. Without an admission control shared
    with `verify()`'s owned-session semaphore, N concurrent slow measured writes can
    exhaust the connection pool on their own. The gate is acquired BEFORE
    `session_factory()` runs and held for as long as this transaction stays open --
    every writer (`persist_claims` and `replace_claims` callers alike) opens its
    session through this one function so the admission rule cannot drift between
    call sites.

    A batch with no `measure=true` claim, or no `verification` service at all
    (declared writes stay legal without one), gets a free `nullcontext`: it never
    contends with `verify()` for this budget -- unaffected, as the contract requires.
    """
    gate = verification.write_gate(resolved) if verification is not None else nullcontext()
    async with gate, session_factory() as session, session.begin():
        yield session


async def _write_claim(
    session: AsyncSession,
    *,
    entity_ref_id: UUID,
    entity_type: str,
    project_key: str,
    claim: ResolvedClaim,
    declared_by: str,
    declared_at: datetime,
    verification: ClaimVerificationService | None,
) -> ClaimWriteOutcome:
    """Insert one occurrence, measuring first when it asked to (AMENDED write order).

    Migration 055's `knowledge_claims_update_gate` forbids any later `provenance`
    UPDATE, so a requested measurement must run and decide the stored provenance
    BEFORE the INSERT -- there is no "insert declared, upgrade after" here. The first
    verdict, when there is one, is appended only once the row exists, because
    `knowledge_claim_verdicts.claim_id` references it (spec 2026-09-19 section 6.3,
    order amended 2026-09-26).
    """
    provenance: Literal["measured", "declared"] = "declared"
    detail: str | None = None
    write_measurement = None
    if claim.measure:
        if verification is None:
            raise ValueError("measure=true claims require a claim verification service")
        write_measurement = await verification.measure_for_write(claim)
        provenance = write_measurement.provenance
        detail = write_measurement.detail

    row = await insert_claim(
        session,
        entity_ref_id=entity_ref_id,
        entity_type=entity_type,
        project_key=project_key,
        claim_key=claim.claim_key,
        statement=claim.statement,
        fact_name=claim.fact_name,
        definition_version=claim.definition_version,
        target=claim.target.value,
        expected=claim.expected,
        expected_resolved=claim.expected_resolved,
        validity_seconds=claim.validity_seconds,
        provenance=provenance,
        declared_by=declared_by,
        declared_at=declared_at,
        replaces_id=claim.replaces,
    )

    if write_measurement is not None:
        assert verification is not None  # `claim.measure` guarantees a service above
        issuer_kind: Literal["robot", "human"] = "human" if is_human_actor(declared_by) else "robot"
        await verification.record_write_verdict(
            session,
            claim_id=row.id,
            resolved=claim,
            write_measurement=write_measurement,
            issuer_identity=declared_by,
            issuer_kind=issuer_kind,
        )

    return ClaimWriteOutcome(claim_id=row.id, provenance=provenance, detail=detail)


async def persist_claims(
    session: AsyncSession,
    *,
    entry_id: UUID,
    entity_type: str,
    project_key: str,
    resolved: Sequence[ResolvedClaim],
    declared_by: str,
    declared_at: datetime,
    verification: ClaimVerificationService | None = None,
) -> list[ClaimWriteOutcome]:
    """Persist claims through the trigger-created anchor inside the caller's entry transaction."""
    entity_ref_id = await _claim_anchor_id(session, entry_id, entity_type)

    outcomes: list[ClaimWriteOutcome] = []
    for claim in resolved:
        outcomes.append(
            await _write_claim(
                session,
                entity_ref_id=entity_ref_id,
                entity_type=entity_type,
                project_key=project_key,
                claim=claim,
                declared_by=declared_by,
                declared_at=declared_at,
                verification=verification,
            )
        )
    return outcomes


async def replace_claims(
    session: AsyncSession,
    *,
    entry_id: UUID,
    entity_type: str,
    project_key: str,
    resolved: Sequence[ResolvedClaim],
    expected_active_claim_ids: Sequence[str],
    declared_by: str,
    verification: ClaimVerificationService | None = None,
) -> ClaimReplacement:
    """Apply one guarded complete replacement, retiring before re-assertions release the unique key.

    `resolved` is resolved by the caller BEFORE the entry transaction opens
    (`resolve_claim_inputs`, same as every other writer) -- so the caller can also
    compute `gated_claim_session`'s admission gate from it up front, instead of this
    function resolving against the registry from inside an already-open transaction.
    """
    entity_ref_id = await _claim_anchor_id(session, entry_id, entity_type)
    active = await active_claims(session, entity_ref_id)
    current_ids = {str(claim.id) for claim in active}
    expected_ids = set(expected_active_claim_ids)
    if expected_ids != current_ids:
        raise ClaimMutationError(
            "claims_conflict: "
            f"expected active ids {sorted(expected_ids)!r}; current active ids {sorted(current_ids)!r}"
        )

    resolved_by_key = {claim.claim_key: claim for claim in resolved}
    if len(resolved_by_key) != len(resolved):
        raise ClaimMutationError("duplicate claim key in replacement input")

    active_by_key = {claim.claim_key: claim for claim in active}
    input_keys = set(resolved_by_key)
    new_claims = [claim for claim in resolved if claim.claim_key not in active_by_key]
    retired_ids = [claim.id for key, claim in active_by_key.items() if key not in input_keys]

    for claim in new_claims:
        predecessor = await latest_retired(session, entity_ref_id, claim.claim_key)
        if predecessor is None:
            if claim.replaces is not None:
                raise ClaimMutationError(
                    "replacement_required: first assertion "
                    f"for claim key {claim.claim_key} must omit replaces"
                )
        elif claim.replaces != predecessor:
            raise ClaimMutationError(
                f"replacement_required: claim key {claim.claim_key} must replace {predecessor}"
            )

    # `claims=[]` deliberately reaches this shared path: its destructive
    # retirement must remain under the same CAS as every other replacement.
    retired = await retire_claims(session, retired_ids, retired_at=datetime.now(UTC))
    created: list[ClaimWriteOutcome] = []
    for claim in new_claims:
        created.append(
            await _write_claim(
                session,
                entity_ref_id=entity_ref_id,
                entity_type=entity_type,
                project_key=project_key,
                claim=claim,
                declared_by=declared_by,
                declared_at=datetime.now(UTC),
                verification=verification,
            )
        )

    return ClaimReplacement(
        kept=len(input_keys.intersection(active_by_key)),
        created=tuple(created),
        retired=retired,
    )
