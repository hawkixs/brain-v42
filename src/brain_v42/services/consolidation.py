"""ConsolidationJob — detect near-duplicate entities via embedding similarity."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

import sqlalchemy as sa
import structlog
from fastmcp.exceptions import AuthorizationError

from brain_v42.db.tables import adrs, consolidation_log, decisions, learnings, runbooks, snippets

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from brain_v42.repositories.pg_consolidation_log import PgConsolidationLogRepo
    from brain_v42.services.graph_helpers import RelationAuthorization
    from brain_v42.services.graph_service import GraphService

_ENTITY_TABLES: dict[str, sa.Table] = {
    "decision": decisions,
    "learning": learnings,
    "snippet": snippets,
    "runbook": runbooks,
    "adr": adrs,
}
MERGEABLE_ENTITY_TYPES = frozenset(_ENTITY_TABLES)

logger = structlog.get_logger(__name__)


class ConsolidationEntityNotFoundError(LookupError):
    """One merge endpoint was absent from the authoritative transaction scope."""


class ConsolidationJob:
    """Detect near-duplicate entities using pgvector cosine similarity."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        consolidation_log_repo: PgConsolidationLogRepo,
        threshold: float = 0.92,
        graph: GraphService | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._log_repo = consolidation_log_repo
        self._threshold = threshold
        self._graph = graph

    async def merge(
        self,
        entity_type: str,
        source_id: UUID,
        target_id: UUID,
        *,
        authorization: RelationAuthorization | None = None,
    ) -> None:
        """Atomically merge PostgreSQL entities, then mirror ``MERGED_INTO``.

        PostgreSQL and its consolidation audit are authoritative and share one
        transaction. Neo4j is attempted only after that transaction commits.

        Args:
            entity_type: Type shared by the source and target entities.
            source_id: UUID of the entity being absorbed (archived).
            target_id: UUID of the entity that survives (target).
            authorization: Optional capability for a project-bounded relation.
        """
        table = _ENTITY_TABLES.get(entity_type)
        if table is None:
            raise ValueError(f"Unknown entity type: {entity_type}")
        if source_id == target_id:
            raise ValueError("Source and target must be different entities")

        project_key = authorization.project_key if authorization is not None else None
        async with self._session_factory() as session:
            async with session.begin():
                source_row, target_row = await self._lock_merge_pair(
                    session,
                    table,
                    entity_type,
                    source_id,
                    target_id,
                    project_key=project_key,
                )
                merged_tags = list(
                    dict.fromkeys([*(target_row["tags"] or []), *(source_row["tags"] or [])])
                )
                await self._update_merge_pair(
                    session,
                    table,
                    entity_type,
                    source_id,
                    target_id,
                    merged_tags,
                    project_key=project_key,
                )
                await self._log_repo.log_action_in_session(
                    session,
                    source_id=source_id,
                    target_id=target_id,
                    entity_type=entity_type,
                    similarity=1.0,
                    action="merged",
                )

        await self._write_graph_relation(
            entity_type,
            source_id,
            target_id,
            authorization=authorization,
        )

    async def _lock_merge_pair(
        self,
        session: AsyncSession,
        table: sa.Table,
        entity_type: str,
        source_id: UUID,
        target_id: UUID,
        *,
        project_key: str | None,
    ) -> tuple[Any, Any]:
        locked_ids = sorted((source_id, target_id), key=lambda entity_id: entity_id.int)
        statement = (
            sa.select(table)
            .where(table.c.id.in_(locked_ids))
            .order_by(table.c.id)
            .with_for_update()
        )
        if project_key is not None:
            statement = statement.where(table.c.project_key == project_key)

        result = await session.execute(statement)
        rows = {row["id"]: row for row in result.mappings().all()}
        source_row = rows.get(source_id)
        if source_row is None:
            raise ConsolidationEntityNotFoundError(f"Source {entity_type} {source_id} not found")
        target_row = rows.get(target_id)
        if target_row is None:
            raise ConsolidationEntityNotFoundError(f"Target {entity_type} {target_id} not found")
        return source_row, target_row

    async def _update_merge_pair(
        self,
        session: AsyncSession,
        table: sa.Table,
        entity_type: str,
        source_id: UUID,
        target_id: UUID,
        merged_tags: list[str],
        *,
        project_key: str | None,
    ) -> None:
        target_update = sa.update(table).where(table.c.id == target_id)
        source_update = sa.update(table).where(table.c.id == source_id)
        if project_key is not None:
            target_update = target_update.where(table.c.project_key == project_key)
            source_update = source_update.where(table.c.project_key == project_key)

        target_result = await session.execute(
            target_update.values(tags=merged_tags).returning(table.c.id)
        )
        if target_result.scalar_one_or_none() is None:
            raise ConsolidationEntityNotFoundError(f"Target {entity_type} {target_id} not found")
        source_result = await session.execute(
            source_update.values(
                merged_into=target_id,
                freshness_status="archived",
                # 043 : le trigger DATE la transition mais ne peut pas savoir
                # d'où elle vient, et il remet la source à NULL dès qu'un
                # écrivain ne la redéclare pas. Sans cette ligne, l'archivage
                # par fusion — le seul qui soit vraiment irréversible — se
                # présentait comme « provenance inconnue ».
                freshness_source="merge",
            ).returning(table.c.id)
        )
        if source_result.scalar_one_or_none() is None:
            raise ConsolidationEntityNotFoundError(f"Source {entity_type} {source_id} not found")

    async def _write_graph_relation(
        self,
        entity_type: str,
        source_id: UUID,
        target_id: UUID,
        *,
        authorization: RelationAuthorization | None,
    ) -> None:
        if self._graph is None:
            self._log_graph_degradation(entity_type, "graph_unavailable")
            return

        if authorization is not None:
            await authorization.revalidate_ids([source_id, target_id])

        try:
            if authorization is None:
                outcome = await self._graph.create_relation(
                    source_id,
                    target_id,
                    "MERGED_INTO",
                    secret_safe=True,
                )
            else:
                outcome = await self._graph.create_relation(
                    source_id,
                    target_id,
                    "MERGED_INTO",
                    project_key=authorization.project_key,
                    secret_safe=True,
                )
        except AuthorizationError:
            raise
        except Exception:
            self._log_graph_degradation(entity_type, "graph_error")
            return
        if outcome in {"error", "missing_node"}:
            self._log_graph_degradation(entity_type, outcome)

    @staticmethod
    def _log_graph_degradation(entity_type: str, reason: str) -> None:
        logger.warning(
            "consolidation_graph_write_degraded",
            entity_type=entity_type,
            reason=reason,
        )

    async def find_candidates(
        self,
        entity_type: str | None = None,
        limit: int = 20,
        *,
        project_key: str | None = None,
    ) -> list[dict[str, Any]]:
        """Find near-duplicate pairs for one or all entity types.

        Returns list of dicts: {entity_type, id_a, id_b, similarity, title_a, title_b}
        """
        types_to_check = [entity_type] if entity_type else list(_ENTITY_TABLES.keys())
        candidates: list[dict[str, Any]] = []

        for etype in types_to_check:
            table = _ENTITY_TABLES.get(etype)
            if table is None:
                continue

            if project_key is None:
                handled = await self._log_repo.get_handled_pairs(etype)
                async with self._session_factory() as session:
                    pairs = await self._find_pairs(session, table, etype)
            else:
                async with self._session_factory() as session:
                    handled = await self._get_scoped_handled_pairs(
                        session,
                        table,
                        etype,
                        project_key,
                    )
                    pairs = await self._find_pairs(
                        session,
                        table,
                        etype,
                        project_key=project_key,
                    )

            for pair in pairs:
                key = (UUID(pair["id_a"]), UUID(pair["id_b"]))
                if key in handled:
                    continue
                candidates.append(pair)
                if len(candidates) >= limit:
                    return candidates

        return candidates

    async def _get_scoped_handled_pairs(
        self,
        session: AsyncSession,
        table: sa.Table,
        entity_type: str,
        project_key: str,
    ) -> set[tuple[UUID, UUID]]:
        """Load handled pairs only when both endpoints still belong to the project."""
        source = table.alias("handled_source")
        target = table.alias("handled_target")
        stmt = (
            sa.select(
                consolidation_log.c.source_id,
                consolidation_log.c.target_id,
            )
            .select_from(
                consolidation_log.join(
                    source,
                    source.c.id == consolidation_log.c.source_id,
                ).join(
                    target,
                    target.c.id == consolidation_log.c.target_id,
                )
            )
            .where(
                sa.and_(
                    consolidation_log.c.entity_type == entity_type,
                    source.c.project_key == project_key,
                    target.c.project_key == project_key,
                )
            )
        )
        result = await session.execute(stmt)
        pairs: set[tuple[UUID, UUID]] = set()
        for row in result.mappings().all():
            pairs.add((row["source_id"], row["target_id"]))
            pairs.add((row["target_id"], row["source_id"]))
        return pairs

    async def _find_pairs(
        self,
        session: AsyncSession,
        table: sa.Table,
        entity_type: str,
        *,
        project_key: str | None = None,
    ) -> list[dict[str, Any]]:
        """Find pairs with embedding similarity > threshold using SQL self-join."""
        a = table.alias("a")
        b = table.alias("b")

        # Determine title column name (title for most, topic for learnings)
        title_col_a = a.c.title if "title" in table.c else a.c.topic
        title_col_b = b.c.title if "title" in table.c else b.c.topic

        similarity_expr = (sa.literal(1.0) - a.c.embedding.op("<=>")(b.c.embedding)).label(
            "similarity"
        )

        filters = [
            a.c.id < b.c.id,
            a.c.embedding.isnot(None),
            b.c.embedding.isnot(None),
            a.c.freshness_status != "archived",
            b.c.freshness_status != "archived",
            a.c.merged_into.is_(None),
            b.c.merged_into.is_(None),
            a.c.project_key.is_not_distinct_from(b.c.project_key),
            (sa.literal(1.0) - a.c.embedding.op("<=>")(b.c.embedding)) > self._threshold,
        ]
        if project_key is not None:
            filters.extend(
                (
                    a.c.project_key == project_key,
                    b.c.project_key == project_key,
                )
            )

        stmt = (
            sa.select(
                a.c.id.label("id_a"),
                b.c.id.label("id_b"),
                similarity_expr,
                title_col_a.label("title_a"),
                title_col_b.label("title_b"),
            )
            .where(sa.and_(*filters))
            .order_by(sa.desc("similarity"))
            .limit(50)
        )

        result = await session.execute(stmt)
        return [
            {
                "entity_type": entity_type,
                "id_a": str(row["id_a"]),
                "id_b": str(row["id_b"]),
                "similarity": round(float(row["similarity"]), 4),
                "title_a": row["title_a"],
                "title_b": row["title_b"],
            }
            for row in result.mappings().all()
        ]
