"""Decay MCP tools: brain_decay_status, brain_refresh_entity,
brain_consolidation_candidates, brain_merge_entities."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID

import sqlalchemy as sa
import structlog

from brain_v42.db.tables import adrs, decisions, indexed_plans, learnings, runbooks, snippets
from brain_v42.mcp.dream_project_authorization import get_dream_project_scope
from brain_v42.mcp.tools.formatters import (
    format_confirmation,
    format_consolidation_candidates,
    format_decay_status,
    format_error,
    format_id,
)
from brain_v42.mcp.tools.tool_annotations import (
    _DESTRUCTIVE_ANNOTATIONS,
    _READ_ANNOTATIONS,
)
from brain_v42.models.brain import KnowledgeType, MutableKnowledgeType
from brain_v42.services.consolidation import (
    MERGEABLE_ENTITY_TYPES,
    ConsolidationEntityNotFoundError,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = structlog.get_logger(__name__)

_DECAY_ENTITY_TABLES: dict[str, sa.Table] = {
    "decision": decisions,
    "learning": learnings,
    "snippet": snippets,
    "runbook": runbooks,
    "adr": adrs,
    "plan": indexed_plans,
}


def register_decay_tools(
    mcp: FastMCP,
    session_factory: async_sessionmaker[AsyncSession],
    consolidation_job: Any | None = None,
) -> None:
    """Register decay-related MCP tools."""

    @mcp.tool(version="1.0", annotations=_READ_ANNOTATIONS)
    async def brain_decay_status() -> str:
        """Get freshness stats for all entity types.

        Returns count of fresh/stale/archived entities per type,
        plus deletion candidates (archived 180+ days, access_count=0).
        """
        scope = get_dream_project_scope()
        stats: dict[str, dict[str, int]] = {}
        deletion_candidates: dict[str, int] = {}

        async with session_factory() as session:
            for entity_type, table in _DECAY_ENTITY_TABLES.items():
                # Count by freshness_status
                stmt = sa.select(
                    table.c.freshness_status,
                    sa.func.count().label("cnt"),
                )
                if scope is not None:
                    stmt = stmt.where(table.c.project_key == scope.project_key)
                stmt = stmt.group_by(table.c.freshness_status)
                result = await session.execute(stmt)
                counts = {"fresh": 0, "stale": 0, "archived": 0}
                for row in result.mappings().all():
                    status = row["freshness_status"] or "fresh"
                    counts[status] = row["cnt"]
                stats[entity_type] = counts

                # Deletion candidates: archived 180+ days with access_count=0
                cutoff = datetime.now(tz=UTC).replace(hour=0, minute=0, second=0, microsecond=0)
                cutoff = cutoff - timedelta(days=180)

                del_stmt = (
                    sa.select(sa.func.count())
                    .select_from(table)
                    .where(
                        sa.and_(
                            table.c.freshness_status == "archived",
                            table.c.access_count == 0,
                            table.c.updated_at < cutoff,
                        )
                    )
                )
                if scope is not None:
                    del_stmt = del_stmt.where(table.c.project_key == scope.project_key)
                del_result = await session.execute(del_stmt)
                del_count = del_result.scalar_one()
                if del_count > 0:
                    deletion_candidates[entity_type] = del_count

        return format_decay_status(
            {
                "stats": stats,
                "deletion_candidates": deletion_candidates,
            }
        )

    @mcp.tool(version="1.0", annotations=_DESTRUCTIVE_ANNOTATIONS)
    async def brain_refresh_entity(
        entity_type: KnowledgeType,
        entity_id: str,
    ) -> str:
        """Force an entity back to 'fresh' status.

        Sets freshness_status='fresh' and last_accessed_at=NOW().

        Args:
            entity_type: One of: decision, learning, snippet, runbook, adr, plan
            entity_id: UUID of the entity to refresh
        """
        table = _DECAY_ENTITY_TABLES.get(entity_type)
        if table is None:
            return format_error(
                f"Unknown entity type: {entity_type}. Use: {', '.join(_DECAY_ENTITY_TABLES)}"
            )

        try:
            uid = UUID(entity_id)
        except ValueError:
            return format_error(f"Invalid UUID: {entity_id}")

        async with session_factory() as session:
            stmt = (
                sa.update(table)
                .where(table.c.id == uid)
                .values(
                    freshness_status="fresh",
                    last_accessed_at=datetime.now(tz=UTC),
                    # 043, vocabulaire fermé : ramener une entité à `fresh` par
                    # un geste délibéré EST la définition de `revive`. Sans la
                    # redéclarer, le trigger la nulle.
                    freshness_source="revive",
                )
                .returning(table.c.id)
            )
            result = await session.execute(stmt)
            row = result.one_or_none()
            await session.commit()

            if row is None:
                return format_error(f"{entity_type} {entity_id} not found")

            logger.info(
                "brain_refresh_entity",
                entity_type=entity_type,
                entity_id=entity_id,
            )
            return format_confirmation("Refreshed", "", id=str(entity_id), type=entity_type)

    # ── Consolidation tools ──────────────────────────────────────────────

    @mcp.tool(version="1.0", annotations=_READ_ANNOTATIONS)
    async def brain_consolidation_candidates(
        entity_type: str | None = None,
        limit: int = 20,
    ) -> str:
        """List quasi-duplicate entity pairs detected by embedding similarity.

        Args:
            entity_type: Filter to one type (decision/learning/snippet/runbook/adr). None = all.
            limit: Maximum pairs to return.
        """
        if consolidation_job is None:
            return format_error("Consolidation not configured")

        scope = get_dream_project_scope()
        if scope is None:
            candidates = await consolidation_job.find_candidates(
                entity_type=entity_type,
                limit=limit,
            )
        else:
            candidates = await consolidation_job.find_candidates(
                entity_type=entity_type,
                limit=limit,
                project_key=scope.project_key,
            )
        return format_consolidation_candidates(candidates)

    @mcp.tool(version="1.0", annotations=_DESTRUCTIVE_ANNOTATIONS)
    async def brain_merge_entities(
        entity_type: MutableKnowledgeType,
        source_id: str,
        target_id: str,
    ) -> str:
        """Merge two entities: keep target, archive source with merged_into pointer.

        The source entity is marked as archived with merged_into=target_id.
        Tags from source are added to target.

        Args:
            entity_type: Type of both entities (decision/learning/snippet/runbook/adr).
            source_id: UUID of entity to absorb (will be archived).
            target_id: UUID of entity to keep (will gain source's tags).
        """
        if entity_type not in MERGEABLE_ENTITY_TYPES:
            return format_error(f"Unknown entity type: {entity_type}")

        try:
            src_uid = UUID(source_id)
            tgt_uid = UUID(target_id)
        except ValueError as e:
            return format_error(f"Invalid UUID: {e}")

        if src_uid == tgt_uid:
            return format_error("Source and target must be different entities")

        if consolidation_job is None:
            return format_error("Consolidation not configured")

        scope = get_dream_project_scope()
        try:
            await consolidation_job.merge(
                entity_type,
                src_uid,
                tgt_uid,
                authorization=scope,
            )
        except ConsolidationEntityNotFoundError as exc:
            return format_error(str(exc))

        logger.info(
            "brain_merge_entities",
            entity_type=entity_type,
        )
        return format_confirmation(
            "Merged",
            "",
            id=str(target_id),
            type=entity_type,
            source_id=format_id(source_id),
        )
