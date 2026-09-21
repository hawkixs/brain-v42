"""Persistence primitives for immutable knowledge-claim occurrences.

Transactions belong to entry writers: retiring and replacing claims must share
the caller's one transaction and one retirement instant.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession

from brain_v42.db.tables import knowledge_claims


@dataclass(frozen=True, slots=True)
class ClaimRow:
    """One immutable claim occurrence returned by PostgreSQL."""

    id: UUID
    seq: int
    claim_key: str
    statement: str
    fact_name: str
    definition_version: int
    target: str
    expected: Mapping[str, object]
    expected_resolved: Mapping[str, object]
    validity_seconds: int
    provenance: str
    declared_by: str
    declared_at: datetime
    recorded_at: datetime
    replaces_id: UUID | None


def _claim_row(row: RowMapping) -> ClaimRow:
    """Discard lifecycle-only fields so callers cannot rewrite an occurrence."""
    return ClaimRow(
        id=row["id"],
        seq=row["seq"],
        claim_key=row["claim_key"],
        statement=row["statement"],
        fact_name=row["fact_name"],
        definition_version=row["definition_version"],
        target=row["target"],
        expected=row["expected"],
        expected_resolved=row["expected_resolved"],
        validity_seconds=row["validity_seconds"],
        provenance=row["provenance"],
        declared_by=row["declared_by"],
        declared_at=row["declared_at"],
        recorded_at=row["recorded_at"],
        replaces_id=row["replaces_id"],
    )


async def insert_claim(
    session: AsyncSession,
    *,
    entity_ref_id: UUID,
    entity_type: str,
    project_key: str,
    claim_key: str,
    statement: str,
    fact_name: str,
    definition_version: int,
    target: str,
    expected: Mapping[str, object],
    expected_resolved: Mapping[str, object],
    validity_seconds: int,
    provenance: str,
    declared_by: str,
    declared_at: datetime,
    replaces_id: UUID | None = None,
) -> ClaimRow:
    """Insert an occurrence and retain trigger diagnostics as the caller's diagnosis.

    The server owns identity, order, and recorded time, so returning this INSERT
    row avoids both a race and a second query.
    """
    row = (
        (
            await session.execute(
                sa.insert(knowledge_claims)
                .values(
                    entity_ref_id=entity_ref_id,
                    entity_type=entity_type,
                    project_key=project_key,
                    claim_key=claim_key,
                    statement=statement,
                    fact_name=fact_name,
                    definition_version=definition_version,
                    target=target,
                    expected=dict(expected),
                    expected_resolved=dict(expected_resolved),
                    validity_seconds=validity_seconds,
                    provenance=provenance,
                    declared_by=declared_by,
                    declared_at=declared_at,
                    replaces_id=replaces_id,
                )
                .returning(knowledge_claims)
            )
        )
        .mappings()
        .one()
    )
    return _claim_row(row)


async def active_claims(session: AsyncSession, entity_ref_id: UUID) -> list[ClaimRow]:
    """Return the deterministic active-set snapshot used by compare-and-swap."""
    rows = (
        await session.execute(
            sa.select(knowledge_claims)
            .where(
                knowledge_claims.c.entity_ref_id == entity_ref_id,
                knowledge_claims.c.retired_at.is_(None),
            )
            .order_by(knowledge_claims.c.seq)
        )
    ).mappings()
    return [_claim_row(row) for row in rows]


async def retire_claims(
    session: AsyncSession, claim_ids: Sequence[UUID], *, retired_at: datetime
) -> int:
    """Retire active occurrences once, letting an already-complete CAS stay a no-op."""
    if not claim_ids:
        return 0
    result = await session.execute(
        sa.update(knowledge_claims)
        .where(
            knowledge_claims.c.id.in_(claim_ids),
            knowledge_claims.c.retired_at.is_(None),
        )
        .values(retired_at=retired_at)
        .returning(knowledge_claims.c.id)
    )
    return len(result.scalars().all())


async def latest_retired(session: AsyncSession, entity_ref_id: UUID, claim_key: str) -> UUID | None:
    """Find the sole predecessor the supersession trigger will accept."""
    return (
        await session.execute(
            sa.select(knowledge_claims.c.id)
            .where(
                knowledge_claims.c.entity_ref_id == entity_ref_id,
                knowledge_claims.c.claim_key == claim_key,
                knowledge_claims.c.retired_at.is_not(None),
            )
            .order_by(knowledge_claims.c.seq.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
