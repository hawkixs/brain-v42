"""ConsolidationLog repository — track handled duplicate pairs."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

import sqlalchemy as sa
import structlog

from brain_v42.db.tables import consolidation_log

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = structlog.get_logger(__name__)


class PgConsolidationLogRepo:
    """Repository for consolidation_log table."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_handled_pairs(self, entity_type: str) -> set[tuple[UUID, UUID]]:
        """Get set of (source_id, target_id) pairs already handled."""
        async with self._session_factory() as session:
            stmt = sa.select(
                consolidation_log.c.source_id,
                consolidation_log.c.target_id,
            ).where(consolidation_log.c.entity_type == entity_type)
            result = await session.execute(stmt)
            pairs: set[tuple[UUID, UUID]] = set()
            for row in result.mappings().all():
                pairs.add((row["source_id"], row["target_id"]))
                pairs.add((row["target_id"], row["source_id"]))  # both directions
            return pairs

    async def log_action(
        self,
        source_id: UUID,
        target_id: UUID,
        entity_type: str,
        similarity: float,
        action: str,
    ) -> None:
        """Log a merge or dismiss action."""
        async with self._session_factory() as session:
            await self.log_action_in_session(
                session,
                source_id=source_id,
                target_id=target_id,
                entity_type=entity_type,
                similarity=similarity,
                action=action,
            )
            await session.commit()

    async def log_action_in_session(
        self,
        session: AsyncSession,
        *,
        source_id: UUID,
        target_id: UUID,
        entity_type: str,
        similarity: float,
        action: str,
    ) -> None:
        """Insert one audit row without owning the surrounding transaction."""
        await session.execute(
            sa.insert(consolidation_log).values(
                source_id=source_id,
                target_id=target_id,
                entity_type=entity_type,
                similarity=similarity,
                action=action,
            )
        )
