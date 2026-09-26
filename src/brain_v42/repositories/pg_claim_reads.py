"""SELECT-only, scoped reads of claim occurrences and their verdict ledger."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession

from brain_v42.db.tables import brain_entities, knowledge_claim_verdicts, knowledge_claims
from brain_v42.models.claim_read import ClaimRead, VerdictRead
from brain_v42.repositories.pg_claim_verdicts import VerdictRow, verdict_row


@dataclass(frozen=True, slots=True)
class ReadPage[T]:
    items: tuple[T, ...]
    next_after_seq: int | None


_current = sa.table(
    "knowledge_claim_current",
    sa.column("claim_id", sa.Uuid),
    sa.column("latest_seq", sa.BigInteger),
    sa.column("conclusive_seq", sa.BigInteger),
)
_latest = knowledge_claim_verdicts.alias("latest_verdict")
_conclusive = knowledge_claim_verdicts.alias("conclusive_verdict")


def _verdict_columns(table: sa.FromClause, prefix: str) -> tuple[sa.Label[object], ...]:
    return tuple(
        table.c[name].label(f"{prefix}_{name}")
        for name in (
            "id",
            "seq",
            "verdict",
            "reason",
            "emitted_at",
            "recorded_at",
            "observation_id",
            "measurement",
        )
    )


def _base_select() -> sa.Select[tuple[object, ...]]:
    return (
        sa.select(
            knowledge_claims,
            brain_entities.c.source_uuid.label("entry_id"),
            *_verdict_columns(_latest, "latest"),
            *_verdict_columns(_conclusive, "conclusive"),
        )
        .join(brain_entities, brain_entities.c.id == knowledge_claims.c.entity_ref_id)
        .outerjoin(_current, _current.c.claim_id == knowledge_claims.c.id)
        .outerjoin(
            _latest,
            sa.and_(
                _latest.c.claim_id == knowledge_claims.c.id,
                _latest.c.seq == _current.c.latest_seq,
            ),
        )
        .outerjoin(
            _conclusive,
            sa.and_(
                _conclusive.c.claim_id == knowledge_claims.c.id,
                _conclusive.c.seq == _current.c.conclusive_seq,
            ),
        )
    )


def _verdict_read(row: RowMapping, prefix: str) -> VerdictRead | None:
    if row[f"{prefix}_id"] is None:
        return None
    return VerdictRead(
        id=row[f"{prefix}_id"],
        seq=row[f"{prefix}_seq"],
        verdict=row[f"{prefix}_verdict"],
        reason=row[f"{prefix}_reason"],
        emitted_at=row[f"{prefix}_emitted_at"],
        recorded_at=row[f"{prefix}_recorded_at"],
        observation_id=row[f"{prefix}_observation_id"],
        measurement=row[f"{prefix}_measurement"],
    )


def _claim_read(row: RowMapping) -> ClaimRead:
    return ClaimRead(
        id=row["id"],
        seq=row["seq"],
        entry_id=row["entry_id"],
        entity_type=row["entity_type"],
        project_key=row["project_key"],
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
        retired_at=row["retired_at"],
        replaces_id=row["replaces_id"],
        latest=_verdict_read(row, "latest"),
        conclusive=_verdict_read(row, "conclusive"),
    )


async def fetch_active_for_entries(
    session: AsyncSession,
    entries: Sequence[tuple[str, UUID]],
    *,
    trusted_project_key: str | None = None,
) -> list[ClaimRead]:
    """Fetch all active occurrences for at most 100 authoritative entry keys in one query."""
    if not entries:
        return []
    if len(entries) > 100:
        raise ValueError("entry batch exceeds 100")
    conditions: list[sa.ColumnElement[bool]] = [
        sa.tuple_(brain_entities.c.entity_type, brain_entities.c.source_uuid).in_(entries),
        knowledge_claims.c.retired_at.is_(None),
    ]
    if trusted_project_key is not None:
        conditions.append(brain_entities.c.project_key == trusted_project_key)
        conditions.append(knowledge_claims.c.project_key == trusted_project_key)
    rows = (
        (await session.execute(_base_select().where(*conditions).order_by(knowledge_claims.c.seq)))
        .mappings()
        .all()
    )
    return [_claim_read(row) for row in rows]


async def list_occurrences(
    session: AsyncSession,
    *,
    project_key: str | None = None,
    entry_id: UUID | None = None,
    include_retired: bool = False,
    after_seq: int = 0,
    limit: int = 50,
    trusted_project_key: str | None = None,
) -> ReadPage[ClaimRead]:
    """Page occurrences in insertion order, applying every filter on every page."""
    conditions: list[sa.ColumnElement[bool]] = [knowledge_claims.c.seq > after_seq]
    if project_key is not None:
        conditions.append(knowledge_claims.c.project_key == project_key)
    if trusted_project_key is not None:
        conditions.append(brain_entities.c.project_key == trusted_project_key)
        conditions.append(knowledge_claims.c.project_key == trusted_project_key)
    if entry_id is not None:
        conditions.append(brain_entities.c.source_uuid == entry_id)
    if not include_retired:
        conditions.append(knowledge_claims.c.retired_at.is_(None))
    rows = (
        (
            await session.execute(
                _base_select().where(*conditions).order_by(knowledge_claims.c.seq).limit(limit + 1)
            )
        )
        .mappings()
        .all()
    )
    items = tuple(_claim_read(row) for row in rows[:limit])
    return ReadPage(items, items[-1].seq if len(rows) > limit else None)


async def fetch_claim(
    session: AsyncSession, claim_id: UUID, *, trusted_project_key: str | None = None
) -> ClaimRead | None:
    """Mask a missing occurrence and one outside the trusted scope identically."""
    conditions: list[sa.ColumnElement[bool]] = [knowledge_claims.c.id == claim_id]
    if trusted_project_key is not None:
        conditions.append(knowledge_claims.c.project_key == trusted_project_key)
        conditions.append(brain_entities.c.project_key == trusted_project_key)
    row = (await session.execute(_base_select().where(*conditions))).mappings().one_or_none()
    return None if row is None else _claim_read(row)


async def fetch_verdict_page(
    session: AsyncSession, claim_id: UUID, *, after_seq: int = 0, limit: int = 50
) -> ReadPage[VerdictRow]:
    """Read complete verdict rows in server sequence order, including old evidence."""
    rows = (
        (
            await session.execute(
                sa.select(knowledge_claim_verdicts)
                .where(
                    knowledge_claim_verdicts.c.claim_id == claim_id,
                    knowledge_claim_verdicts.c.seq > after_seq,
                )
                .order_by(knowledge_claim_verdicts.c.seq.asc())
                .limit(limit + 1)
            )
        )
        .mappings()
        .all()
    )
    items = tuple(verdict_row(row) for row in rows[:limit])
    return ReadPage(items, items[-1].seq if len(rows) > limit else None)
