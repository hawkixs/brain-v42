"""Shared declared-claim resolution and persistence for knowledge entry writers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

import sqlalchemy as sa

from brain_v42.db.tables import brain_entities
from brain_v42.facts import ResolvedClaim, resolve_claim
from brain_v42.facts.registry import UnknownFactError
from brain_v42.models.claim_input import ClaimInput, validate_claim_inputs
from brain_v42.repositories.pg_knowledge_claims import (
    active_claims,
    insert_claim,
    latest_retired,
    retire_claims,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from brain_v42.facts.registry import FactRegistry


class ClaimMutationError(ValueError):
    """A claim PATCH refusal whose caller must roll back its enclosing entry update."""


@dataclass(frozen=True, slots=True)
class ClaimReplacement:
    """Counts and generated occurrence ids a caller needs to describe a claim replacement."""

    kept: int
    created_ids: tuple[UUID, ...]
    retired: int


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


async def persist_claims(
    session: AsyncSession,
    *,
    entry_id: UUID,
    entity_type: str,
    project_key: str,
    resolved: Sequence[ResolvedClaim],
    declared_by: str,
    declared_at: datetime,
) -> list[UUID]:
    """Persist claims through the trigger-created anchor inside the caller's entry transaction."""
    entity_ref_id = await _claim_anchor_id(session, entry_id, entity_type)

    claim_ids: list[UUID] = []
    for claim in resolved:
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
            provenance="declared",
            declared_by=declared_by,
            declared_at=declared_at,
            replaces_id=claim.replaces,
        )
        claim_ids.append(row.id)
    return claim_ids


async def replace_claims(
    session: AsyncSession,
    *,
    entry_id: UUID,
    entity_type: str,
    project_key: str,
    registry: FactRegistry,
    claims: Sequence[Mapping[str, object]],
    expected_active_claim_ids: Sequence[str],
    declared_by: str,
) -> ClaimReplacement:
    """Apply one guarded complete replacement, retiring before re-assertions release the unique key."""
    entity_ref_id = await _claim_anchor_id(session, entry_id, entity_type)
    active = await active_claims(session, entity_ref_id)
    current_ids = {str(claim.id) for claim in active}
    expected_ids = set(expected_active_claim_ids)
    if expected_ids != current_ids:
        raise ClaimMutationError(
            "claims_conflict: "
            f"expected active ids {sorted(expected_ids)!r}; current active ids {sorted(current_ids)!r}"
        )

    resolved = await resolve_claim_inputs(registry, claims)
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
    created_ids: list[UUID] = []
    for claim in new_claims:
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
            provenance="declared",
            declared_by=declared_by,
            declared_at=datetime.now(UTC),
            replaces_id=claim.replaces,
        )
        created_ids.append(row.id)

    return ClaimReplacement(
        kept=len(input_keys.intersection(active_by_key)),
        created_ids=tuple(created_ids),
        retired=retired,
    )
