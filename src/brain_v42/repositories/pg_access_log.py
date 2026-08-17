"""AccessLog repository — aggregate access stats and purge old entries."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID

import sqlalchemy as sa
import structlog

from brain_v42.db.tables import access_log
from brain_v42.provenance import is_human_actor

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = structlog.get_logger(__name__)

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
                sa.func.max(access_log.c.accessed_at).label("max_accessed"),
                sa.func.count().label("cnt"),
            )
            .where(access_log.c.id <= max_id)
            .group_by(access_log.c.entity_type, access_log.c.entity_id, access_log.c.actor)
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
                # §5.2 — le pendant humain du `max_accessed`. Sans lui, le terme
                # de récence (poids 0,3, le plus lourd après l'âge) reste piloté
                # par les lectures MACHINE : 1 779 learnings mesurés dans ce cas.
                # `None` veut dire « aucune lecture humaine dans ce lot », pas
                # « jamais lu » — le flusher ne doit alors rien écraser.
                if (
                    entry["max_accessed_human"] is None
                    or row["max_accessed"] > entry["max_accessed_human"]
                ):
                    entry["max_accessed_human"] = row["max_accessed"]
            if row["max_accessed"] > entry["max_accessed"]:
                entry["max_accessed"] = row["max_accessed"]

        # 4. Delete only snapshotted rows (same txn — caller commits)
        await session.execute(sa.delete(access_log).where(access_log.c.id <= max_id))

        logger.debug(
            "access_log.aggregated",
            entities=len(aggregated),
        )
        return aggregated

    async def aggregate_and_flush(
        self,
    ) -> dict[tuple[str, UUID], dict[str, Any]]:
        """Aggregate access_log rows and delete them.

        .. deprecated::
            Use :meth:`aggregate_in_session` instead.  This method opens its
            own session and commits before the caller has updated entities,
            creating a window where a crash loses access counts permanently.
            Kept for backward-compatibility during the transition period.

        Returns dict keyed by (entity_type, entity_id) with values:
            {"max_accessed": datetime, "count": int}
        """
        async with self._session_factory() as session:
            # 1. Capture snapshot boundary
            max_id_result = await session.execute(sa.select(sa.func.max(access_log.c.id)))
            max_id = max_id_result.scalar_one_or_none()
            if max_id is None:
                return {}

            # 2. Aggregate only snapshotted rows
            stmt = (
                sa.select(
                    access_log.c.entity_type,
                    access_log.c.entity_id,
                    sa.func.max(access_log.c.accessed_at).label("max_accessed"),
                    sa.func.count().label("cnt"),
                )
                .where(access_log.c.id <= max_id)
                .group_by(access_log.c.entity_type, access_log.c.entity_id)
            )
            result = await session.execute(stmt)
            rows = result.mappings().all()

            if not rows:
                return {}

            # 3. Build result dict
            aggregated: dict[tuple[str, UUID], dict[str, Any]] = {}
            for row in rows:
                key = (row["entity_type"], row["entity_id"])
                aggregated[key] = {
                    "max_accessed": row["max_accessed"],
                    "count": row["cnt"],
                }

            # 4. Delete only snapshotted rows
            await session.execute(sa.delete(access_log).where(access_log.c.id <= max_id))
            await session.commit()

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
