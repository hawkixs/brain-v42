"""Shared declared-claim resolution and persistence for knowledge entry writers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

import sqlalchemy as sa

from brain_v42.db.tables import brain_entities
from brain_v42.facts import ResolvedClaim, resolve_claim
from brain_v42.facts.registry import UnknownFactError
from brain_v42.models.claim_input import ClaimInput, validate_claim_inputs
from brain_v42.repositories.pg_knowledge_claims import insert_claim

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from brain_v42.facts.registry import FactRegistry


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
    entity_ref_id = await session.scalar(
        sa.select(brain_entities.c.id).where(brain_entities.c.source_uuid == entry_id)
    )
    if entity_ref_id is None:
        raise RuntimeError(
            f"claim anchor missing for {entity_type} entry {entry_id}; source registry trigger did not fire"
        )

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
