"""AccessLog repository — aggregate access stats and purge old entries."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID

import sqlalchemy as sa
import structlog
from sqlalchemy.dialects.postgresql import insert as pg_insert

from brain_v42.db.tables import access_log, access_log_daily
from brain_v42.provenance import is_human_actor

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = structlog.get_logger(__name__)


def _utc_day() -> Any:
    """The calendar day of an access, in UTC and never in the session's zone.

    `date(accessed_at)` alone would resolve through `TimeZone`, so the same row
    could land on two different days depending on which connection flushed it —
    and the journal's primary key would then hold two rows where there is one
    fact. `AT TIME ZONE 'UTC'` pins it.

    `literal_column`, not a bound parameter, and that is load-bearing: passing
    `"UTC"` as a value renders `timezone($1, …)` in the SELECT and `timezone($3, …)`
    in the GROUP BY, and PostgreSQL matches grouping expressions by TEXT — two
    different placeholders are two different expressions, and the query dies with
    `column "access_log.accessed_at" must appear in the GROUP BY clause`.
    """
    return sa.func.date(sa.func.timezone(sa.literal_column("'UTC'"), access_log.c.accessed_at))


async def _write_daily_journal(session: AsyncSession, rows: Sequence[Any]) -> None:
    """Persist what the aggregation ALREADY grouped: (entity, actor, day).

    Ticket b93e32be. The counters keep `is_human_actor()`'s verdict; this keeps
    the ACTOR STRING that produced it, which is what makes a change to the
    human/machine rule replayable after the queue has been drained.

    `count` ACCUMULATES and `last_accessed_at` only moves forward. Writing
    `count = excluded.count` instead would pass every single-flush test and
    silently turn a daily total into "whatever the last 300 s window saw"; a
    bare assignment on the instant would let a late flush of older events walk
    the last access backwards. Both are pinned by
    `tests/integration/db/test_migration_052_access_log_daily.py`.
    """
    statement = pg_insert(access_log_daily).values(
        [
            {
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "actor": row["actor"],
                "day": row["day"],
                "count": row["cnt"],
                "last_accessed_at": row["max_accessed"],
            }
            for row in rows
        ]
    )
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=["entity_type", "entity_id", "actor", "day"],
            set_={
                "count": access_log_daily.c.count + statement.excluded.count,
                "last_accessed_at": sa.func.greatest(
                    access_log_daily.c.last_accessed_at,
                    statement.excluded.last_accessed_at,
                ),
            },
        )
    )


# Advisory lock key dedicated to the decay-flush critical section.
# Chosen as a stable, documented constant in the 0x_BRAIN_V42 namespace
# (decimal: 0x42_DECAY_0 → 0x4244454341590000 truncated to bigint range).
# Any two DecayFlusher processes (multi-process metrics sidecar) that race
# will serialize here, preventing double-counting the same access_log rows.
DECAY_FLUSH_ADVISORY_LOCK: int = 0x424465634179_0000 & 0x7FFF_FFFF_FFFF_FFFF


class PgAccessLogRepo:
    """Repository for access_log table: aggregate + purge."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def aggregate_in_session(
        self,
        session: AsyncSession,
    ) -> dict[tuple[str, UUID], dict[str, Any]]:
        """Aggregate access_log rows and delete them — caller owns the transaction.

        This method does NOT commit.  The caller is responsible for committing
        (or rolling back) the session.  This allows the aggregation, entity
        freshness updates, and access_log DELETE to all live inside a single
        atomic transaction, so a mid-flush crash leaves access_log intact for
        the next run.

        Cross-process serialization is achieved by acquiring
        ``pg_advisory_xact_lock(DECAY_FLUSH_ADVISORY_LOCK)`` as the very first
        statement.  The lock is released automatically when the transaction ends.

        Returns dict keyed by (entity_type, entity_id) with values:
            {"max_accessed": datetime, "count": int, "count_human": int}
        """
        # 0. Serialize concurrent flushers via an exclusive advisory lock.
        #    Released automatically at transaction end (xact-level lock).
        await session.execute(sa.select(sa.func.pg_advisory_xact_lock(DECAY_FLUSH_ADVISORY_LOCK)))

        # 1. Capture snapshot boundary
        max_id_result = await session.execute(sa.select(sa.func.max(access_log.c.id)))
        max_id = max_id_result.scalar_one_or_none()
        if max_id is None:
            return {}

        # 2. Aggregate only snapshotted rows, split by actor so the
        #    human/system rule stays in ONE place (brain_v42.provenance).
        stmt = (
            sa.select(
                access_log.c.entity_type,
                access_log.c.entity_id,
                access_log.c.actor,
                _utc_day().label("day"),
                sa.func.max(access_log.c.accessed_at).label("max_accessed"),
                sa.func.count().label("cnt"),
            )
            .where(access_log.c.id <= max_id)
            .group_by(
                access_log.c.entity_type,
                access_log.c.entity_id,
                access_log.c.actor,
                _utc_day(),
            )
        )
        result = await session.execute(stmt)
        rows = result.mappings().all()

        if not rows:
            return {}

        # 3. Build result dict, folding the per-actor groups back together
        aggregated: dict[tuple[str, UUID], dict[str, Any]] = {}
        for row in rows:
            key = (row["entity_type"], row["entity_id"])
            entry = aggregated.setdefault(
                key,
                {
                    "max_accessed": row["max_accessed"],
                    "max_accessed_human": None,
                    "count": 0,
                    "count_human": 0,
                },
            )
            entry["count"] += row["cnt"]
            if is_human_actor(row["actor"]):
                entry["count_human"] += row["cnt"]
                # §5.2 — the human counterpart of `max_accessed`. Without it,
                # the recency term stays driven by MACHINE reads: 1,522
                # learnings in that state on 2026-08-22, 2,060 across the six
                # tables. Its weight is PER TYPE — 0.3 on five types, 0.2 for
                # `adr` — and it is NEVER dominated by age (`w_access >= w_age`
                # on all six): "the heaviest after age" understated it.
                # `None` means "no human read in this batch", not "never read"
                # — the flusher must then overwrite nothing.
                if (
                    entry["max_accessed_human"] is None
                    or row["max_accessed"] > entry["max_accessed_human"]
                ):
                    entry["max_accessed_human"] = row["max_accessed"]
            if row["max_accessed"] > entry["max_accessed"]:
                entry["max_accessed"] = row["max_accessed"]

        # 4. The DURABLE journal, written BEFORE the delete and in the SAME
        #    transaction — otherwise a crash between the two would lose exactly
        #    the events this table exists to keep (ticket b93e32be).
        await _write_daily_journal(session, rows)

        # 5. Delete only snapshotted rows (same txn — caller commits).
        #    The queue stays transient: ADR #21 rules that `access_log` is an
        #    aggregation buffer and not a journal. The journal is a SECOND
        #    store, never a reason to keep the queue.
        await session.execute(sa.delete(access_log).where(access_log.c.id <= max_id))

        logger.debug(
            "access_log.aggregated",
            entities=len(aggregated),
        )
        return aggregated

    async def purge_old(self, days: int = 30) -> None:
        """Delete access_log entries older than N days."""
        cutoff = datetime.now(tz=UTC) - timedelta(days=days)
        async with self._session_factory() as session:
            await session.execute(sa.delete(access_log).where(access_log.c.accessed_at < cutoff))
            await session.commit()
            logger.debug("access_log.purged", days=days)

    async def row_count(self) -> int:
        """Return the number of rows in access_log."""
        async with self._session_factory() as session:
            result = await session.execute(sa.select(sa.func.count()).select_from(access_log))
            return result.scalar_one()
