"""Caller-transaction persistence for immutable claim-verdict ledger rows."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession

from brain_v42.db.tables import (
    knowledge_claim_verdicts,
    knowledge_claims,
    knowledge_fact_definitions,
)


@dataclass(frozen=True, slots=True)
class ScopedClaim:
    """The immutable claim occurrence and definition snapshot used for one verification."""

    id: UUID
    project_key: str
    retired_at: datetime | None
    fact_name: str
    definition_version: int
    target: str
    expected_resolved: Mapping[str, object]
    validity_seconds: int
    definition_ttl_seconds: int


@dataclass(frozen=True, slots=True)
class VerdictRow:
    """A complete server-returned append-only verdict row."""

    id: UUID
    seq: int
    claim_id: UUID
    verdict: str
    reason: str | None
    measurement: Mapping[str, object]
    measurement_digest: str | None
    observation_id: UUID
    issuer_identity: str
    issuer_kind: str
    request_fingerprint: str
    outcome_fingerprint: str
    idempotency_key: str
    emitted_at: datetime
    recorded_at: datetime


def _scoped_claim(row: RowMapping) -> ScopedClaim:
    return ScopedClaim(
        id=row["id"],
        project_key=row["project_key"],
        retired_at=row["retired_at"],
        fact_name=row["fact_name"],
        definition_version=row["definition_version"],
        target=row["target"],
        expected_resolved=row["expected_resolved"],
        validity_seconds=row["validity_seconds"],
        definition_ttl_seconds=row["definition_ttl_seconds"],
    )


def verdict_row(row: RowMapping) -> VerdictRow:
    """Convert a complete SELECT/RETURNING mapping without discarding audit fields."""
    return VerdictRow(
        id=row["id"],
        seq=row["seq"],
        claim_id=row["claim_id"],
        verdict=row["verdict"],
        reason=row["reason"],
        measurement=row["measurement"],
        measurement_digest=row["measurement_digest"],
        observation_id=row["observation_id"],
        issuer_identity=row["issuer_identity"],
        issuer_kind=row["issuer_kind"],
        request_fingerprint=row["request_fingerprint"],
        outcome_fingerprint=row["outcome_fingerprint"],
        idempotency_key=row["idempotency_key"],
        emitted_at=row["emitted_at"],
        recorded_at=row["recorded_at"],
    )


async def lookup_locked_claim(
    session: AsyncSession, claim_id: UUID, project_key: str | None
) -> ScopedClaim | None:
    """Lock only the claim occurrence while retaining its immutable definition TTL."""
    conditions: list[sa.ColumnElement[bool]] = [knowledge_claims.c.id == claim_id]
    if project_key is not None:
        conditions.append(knowledge_claims.c.project_key == project_key)
    row = (
        (
            await session.execute(
                sa.select(
                    knowledge_claims.c.id,
                    knowledge_claims.c.project_key,
                    knowledge_claims.c.retired_at,
                    knowledge_claims.c.fact_name,
                    knowledge_claims.c.definition_version,
                    knowledge_claims.c.target,
                    knowledge_claims.c.expected_resolved,
                    knowledge_claims.c.validity_seconds,
                    knowledge_fact_definitions.c.ttl_seconds.label("definition_ttl_seconds"),
                )
                .join(
                    knowledge_fact_definitions,
                    sa.and_(
                        knowledge_fact_definitions.c.fact_name == knowledge_claims.c.fact_name,
                        knowledge_fact_definitions.c.definition_version
                        == knowledge_claims.c.definition_version,
                    ),
                )
                .where(*conditions)
                .with_for_update(of=knowledge_claims)
            )
        )
        .mappings()
        .one_or_none()
    )
    return None if row is None else _scoped_claim(row)


async def lookup_request(
    session: AsyncSession,
    *,
    claim_id: UUID,
    issuer_identity: str,
    idempotency_key: str,
) -> VerdictRow | None:
    """Find the one replay candidate under the ledger's idempotency constraint."""
    row = (
        (
            await session.execute(
                sa.select(knowledge_claim_verdicts).where(
                    knowledge_claim_verdicts.c.claim_id == claim_id,
                    knowledge_claim_verdicts.c.issuer_identity == issuer_identity,
                    knowledge_claim_verdicts.c.idempotency_key == idempotency_key,
                )
            )
        )
        .mappings()
        .one_or_none()
    )
    return None if row is None else verdict_row(row)


async def lookup_observation(
    session: AsyncSession, claim_id: UUID, observation_id: UUID
) -> VerdictRow | None:
    """Find whether this server observation already has a verdict for the claim."""
    row = (
        (
            await session.execute(
                sa.select(knowledge_claim_verdicts).where(
                    knowledge_claim_verdicts.c.claim_id == claim_id,
                    knowledge_claim_verdicts.c.observation_id == observation_id,
                )
            )
        )
        .mappings()
        .one_or_none()
    )
    return None if row is None else verdict_row(row)


async def append_verdict(
    session: AsyncSession,
    *,
    claim_id: UUID,
    verdict: str,
    reason: str | None,
    measurement: Mapping[str, object],
    measurement_digest: str | None,
    observation_id: UUID,
    issuer_identity: str,
    issuer_kind: str,
    request_fingerprint: str,
    outcome_fingerprint: str,
    idempotency_key: str,
    emitted_at: datetime,
) -> VerdictRow:
    """Append one complete row; PostgreSQL alone generates its id, sequence and record time."""
    row = (
        (
            await session.execute(
                sa.insert(knowledge_claim_verdicts)
                .values(
                    claim_id=claim_id,
                    verdict=verdict,
                    reason=reason,
                    measurement=dict(measurement),
                    measurement_digest=measurement_digest,
                    observation_id=observation_id,
                    issuer_identity=issuer_identity,
                    issuer_kind=issuer_kind,
                    request_fingerprint=request_fingerprint,
                    outcome_fingerprint=outcome_fingerprint,
                    idempotency_key=idempotency_key,
                    emitted_at=emitted_at,
                )
                .returning(knowledge_claim_verdicts)
            )
        )
        .mappings()
        .one()
    )
    return verdict_row(row)
