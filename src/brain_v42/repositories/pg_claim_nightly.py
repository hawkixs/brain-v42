"""Selection of stale-or-never-verified claims for the nightly verification step.

Spec §4: active claims, all projects, oldest-first per-project round robin,
capped. No view (`knowledge_claim_current` is a whole-table `DISTINCT ON`) and
no new index -- both LATERAL subqueries below walk the existing
`ix_knowledge_claim_verdicts_claim_seq (claim_id, seq DESC)`.

NOTE for reviewers: this SQL (LATERAL joins + a `row_number()` window) was
written and reasoned through without a reachable PostgreSQL to execute it
against (ADR 27 lot C, T1.3, implemented from a container with no database).
`tests/integration/db/test_claim_nightly_selection.py` exercises every
predicate and the ranking, but it SKIPPED here for lack of a database -- it
must be run for real before this is trusted.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

import sqlalchemy as sa

from brain_v42.db.tables import knowledge_claim_verdicts, knowledge_claims

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@dataclass(frozen=True, slots=True)
class NightlyClaim:
    """One claim selected for tonight's verification, with what `verify_outcome` needs."""

    id: UUID
    project_key: str
    fact_name: str
    target: str
    definition_version: int
    validity_seconds: int
    seq: int
    age_key: datetime


def _eligible_query(now: datetime) -> sa.Select:
    """The eligibility predicate of spec §4.1, before ranking or the cap."""
    claims = knowledge_claims
    verdicts = knowledge_claim_verdicts

    latest = (
        sa.select(
            verdicts.c.seq.label("seq"),
            verdicts.c.verdict.label("verdict"),
            verdicts.c.emitted_at.label("emitted_at"),
        )
        .where(verdicts.c.claim_id == claims.c.id)
        .order_by(verdicts.c.seq.desc())
        .limit(1)
        .correlate(claims)
        .lateral("latest")
    )
    conclusive = (
        sa.select(
            verdicts.c.seq.label("seq"),
            verdicts.c.emitted_at.label("emitted_at"),
        )
        .where(verdicts.c.claim_id == claims.c.id, verdicts.c.verdict.in_(("holds", "falsified")))
        .order_by(verdicts.c.seq.desc())
        .limit(1)
        .correlate(claims)
        .lateral("conclusive")
    )

    one_second: sa.ColumnElement[Any] = sa.literal_column("interval '1 second'")
    validity_expiry = conclusive.c.emitted_at + claims.c.validity_seconds * one_second
    age_key = sa.func.coalesce(latest.c.emitted_at, claims.c.recorded_at).label("age_key")

    return (
        sa.select(
            claims.c.id,
            claims.c.project_key,
            claims.c.fact_name,
            claims.c.target,
            claims.c.definition_version,
            claims.c.validity_seconds,
            claims.c.seq,
            age_key,
        )
        .select_from(claims.outerjoin(latest, sa.true()).outerjoin(conclusive, sa.true()))
        .where(
            claims.c.retired_at.is_(None),
            sa.or_(
                conclusive.c.seq.is_(None),
                validity_expiry <= now,
                sa.and_(latest.c.verdict == "unreadable", latest.c.seq > conclusive.c.seq),
            ),
        )
    )


def _ranked_query(now: datetime, max_claims: int) -> sa.Select:
    """Wrap the eligibility predicate in the spec §4.3 per-project round robin."""
    eligible = _eligible_query(now).subquery("eligible")
    rank = (
        sa.func.row_number()
        .over(partition_by=eligible.c.project_key, order_by=(eligible.c.age_key, eligible.c.seq))
        .label("rank")
    )
    ranked = sa.select(eligible, rank).subquery("ranked")
    return (
        sa.select(ranked).order_by(ranked.c.rank, ranked.c.age_key, ranked.c.seq).limit(max_claims)
    )


def _nightly_claim(row: sa.RowMapping) -> NightlyClaim:
    return NightlyClaim(
        id=row["id"],
        project_key=row["project_key"],
        fact_name=row["fact_name"],
        target=row["target"],
        definition_version=row["definition_version"],
        validity_seconds=row["validity_seconds"],
        seq=row["seq"],
        age_key=row["age_key"],
    )


async def select_nightly_claims(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    now: datetime,
    max_claims: int,
) -> tuple[NightlyClaim, ...]:
    """One short read-only transaction, separate from the verifications (spec §4.4)."""
    async with session_factory() as session, session.begin():
        await session.execute(sa.text("SET TRANSACTION READ ONLY"))
        rows = (await session.execute(_ranked_query(now, max_claims))).mappings().all()
    return tuple(_nightly_claim(row) for row in rows)


async def eligible_fact_names(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    now: datetime,
) -> tuple[str, ...]:
    """The distinct `fact_name` of every eligible claim, ignoring the cap (spec §6.1 step 3)."""
    query = sa.select(_eligible_query(now).subquery("eligible").c.fact_name).distinct()
    async with session_factory() as session, session.begin():
        await session.execute(sa.text("SET TRANSACTION READ ONLY"))
        names = (await session.execute(query)).scalars().all()
    return tuple(sorted(set(names)))
