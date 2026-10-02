"""Shared declared-claim resolution and persistence for knowledge entry writers."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager, nullcontext
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal
from uuid import UUID

import sqlalchemy as sa

from brain_v42.db.tables import brain_entities
from brain_v42.facts import ResolvedClaim, resolve_claim
from brain_v42.facts.claim_extractor import ClaimCandidate, EntityKind, extract_candidates
from brain_v42.facts.registry import UnknownFactError
from brain_v42.mcp.tools.claim_tools import _issuer
from brain_v42.models.claim_input import MAX_CLAIMS_PER_WRITE, ClaimInput, validate_claim_inputs
from brain_v42.models.claim_verdict import ClaimVerificationError
from brain_v42.provenance import is_human_actor
from brain_v42.repositories.pg_knowledge_claims import (
    active_claims,
    insert_claim,
    latest_retired,
    retire_claims,
)
from brain_v42.services.claim_extraction_counters import (
    FailureReason,
    record_failure,
    record_skip,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from brain_v42.facts.registry import FactRegistry
    from brain_v42.facts.verification import ClaimVerificationService


#: The fixed server attribution stamped on every automatic, extracted claim.
#: The entry author remains in the entry's own provenance, never forged here.
EXTRACTED_DECLARED_BY = "server:claim-extractor:v1"


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
    provenance: Literal["measured", "declared", "extracted"]
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
    issuer_identity: str | None = None
    if claim.measure:
        if verification is None:
            raise ValueError("measure=true claims require a claim verification service")
        try:
            issuer_identity = _issuer(declared_by)
        except ClaimVerificationError as exc:
            if exc.code != "unknown_actor":
                raise
            detail = "unattributed caller"
        else:
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
        assert issuer_identity is not None
        issuer_kind: Literal["robot", "human"] = "human" if is_human_actor(declared_by) else "robot"
        await verification.record_write_verdict(
            session,
            claim_id=row.id,
            resolved=claim,
            write_measurement=write_measurement,
            issuer_identity=issuer_identity,
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


def plan_extracted_claims(
    *,
    entity: EntityKind,
    project_key: str | None,
    fields: Mapping[str, str | None],
    reserved: int = 0,
) -> tuple[ClaimCandidate, ...]:
    """Parse selected prose into automatic candidates, accounting for every refusal.

    Runs BEFORE the entry transaction opens, so a parser error must be caught
    here: left alone it would fail an entry write that has nothing wrong with it.
    The quota is the one claim batch an entry write may carry
    (`MAX_CLAIMS_PER_WRITE`) after `reserved` slots go to explicit claims, which
    always win; each candidate that no longer fits is counted, never silently
    dropped. Only a parser failure is swallowed -- explicit input was already
    validated by the caller and is not touched here.
    """
    try:
        result = extract_candidates(entity=entity, project_key=project_key, fields=fields)
    except Exception:
        record_failure(entity, "parser_error")
        return ()
    for reason in sorted(result.reasons):
        record_skip(entity, reason)
    slots = max(MAX_CLAIMS_PER_WRITE - reserved, 0)
    for _ in result.candidates[slots:]:
        record_skip(entity, "quota_full")
    return result.candidates[:slots]


async def persist_extracted_claims(
    session: AsyncSession,
    registry: FactRegistry,
    *,
    entry_id: UUID,
    entity_type: EntityKind,
    project_key: str | None,
    candidates: Sequence[ClaimCandidate],
) -> list[ClaimWriteOutcome]:
    """Persist automatic candidates in one savepoint that failure cannot poison.

    Automatic extraction is best-effort and server-owned: an unregistered or
    disabled fact skips its candidate, and any resolver or insert error rolls
    back only this savepoint, is counted under a fixed reason, and lets the
    entry write commit. Explicit claims and the outer commit belong to the
    caller and are never touched here -- only the automatic part is caught. This
    function only ever inserts: it never retires an occurrence, so an automatic
    claim cannot displace an explicit one.
    """
    if project_key is None or not candidates:
        return []
    entry = str(entry_id)
    stage: FailureReason = "persistence_error"
    outcomes: list[ClaimWriteOutcome] = []
    try:
        async with session.begin_nested():
            entity_ref_id = await _claim_anchor_id(session, entry_id, entity_type)
            # An explicit claim for a fact wins whatever its value or statement: it is
            # read here, in the writer's own transaction, so it covers a claim persisted
            # moments ago by this very write as well as one that was already active.
            explicit_facts = {
                claim.fact_name
                for claim in await active_claims(session, entity_ref_id)
                if claim.provenance != "extracted"
            }
            for candidate in candidates:
                if candidate.fact_name in explicit_facts:
                    record_skip(entity_type, "explicit_precedence", entity_id=entry)
                    continue
                try:
                    descriptor = registry.describe(candidate.fact_name)
                except UnknownFactError:
                    record_skip(entity_type, "unregistered_fact", entity_id=entry)
                    continue
                if candidate.fact_name in registry.disabled():
                    record_skip(entity_type, "disabled_fact", entity_id=entry)
                    continue
                stage = "resolver_error"
                resolved = resolve_claim(
                    ClaimInput(
                        statement=candidate.statement,
                        fact_name=candidate.fact_name,
                        expected=dict(candidate.expected),
                        measure=False,
                    ),
                    descriptor,
                )
                stage = "persistence_error"
                row = await insert_claim(
                    session,
                    entity_ref_id=entity_ref_id,
                    entity_type=entity_type,
                    project_key=project_key,
                    claim_key=resolved.claim_key,
                    statement=resolved.statement,
                    fact_name=resolved.fact_name,
                    definition_version=resolved.definition_version,
                    target=resolved.target.value,
                    expected=resolved.expected,
                    expected_resolved=resolved.expected_resolved,
                    validity_seconds=resolved.validity_seconds,
                    provenance="extracted",
                    declared_by=EXTRACTED_DECLARED_BY,
                    declared_at=datetime.now(UTC),
                )
                outcomes.append(
                    ClaimWriteOutcome(claim_id=row.id, provenance="extracted", detail=None)
                )
    except Exception:
        record_failure(entity_type, stage, entity_id=entry)
        return []
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
    # An identical content key does not mean "keep" when the active occurrence is
    # automatic: the explicit claim takes it over as a new `declared` occurrence
    # (migration 055 forbids changing provenance in place), naming the retired
    # one as its predecessor. A caller-supplied `replaces` must agree with that.
    takeover_keys = {
        key
        for key in input_keys.intersection(active_by_key)
        if active_by_key[key].provenance == "extracted"
    }
    for key in takeover_keys:
        supplied = resolved_by_key[key].replaces
        if supplied is not None and supplied != active_by_key[key].id:
            raise ClaimMutationError(
                f"replacement_required: claim key {key} must replace {active_by_key[key].id}"
            )
    new_claims = [
        claim
        for claim in resolved
        if claim.claim_key not in active_by_key or claim.claim_key in takeover_keys
    ]
    retired_ids = [claim.id for key, claim in active_by_key.items() if key not in input_keys]
    retired_ids.extend(active_by_key[key].id for key in takeover_keys)

    for claim in new_claims:
        if claim.claim_key in takeover_keys:
            continue
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
        if claim.claim_key in takeover_keys:
            # Checked after retirement, before insertion: the trigger accepts only the
            # latest retired occurrence of the key, which must be the one just retired.
            predecessor = await latest_retired(session, entity_ref_id, claim.claim_key)
            if predecessor != active_by_key[claim.claim_key].id:
                raise ClaimMutationError(
                    f"replacement_required: claim key {claim.claim_key} must replace {predecessor}"
                )
            claim = replace(claim, replaces=predecessor)
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
        kept=len(input_keys.intersection(active_by_key)) - len(takeover_keys),
        created=tuple(created),
        retired=retired,
    )
