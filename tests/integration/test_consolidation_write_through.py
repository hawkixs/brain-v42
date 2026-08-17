"""Runtime proofs for atomic PostgreSQL merge and immediate Neo4j write-through."""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brain_v42.db.tables import consolidation_log, decisions
from brain_v42.mcp.dream_project_authorization import (
    DreamProjectAudit,
    DreamProjectScope,
    PostgresDreamProjectResolver,
    bind_dream_project_scope,
)
from brain_v42.models.decision import DecisionCreate
from brain_v42.repositories.pg_consolidation_log import PgConsolidationLogRepo
from brain_v42.repositories.pg_decision import PgDecisionRepo
from brain_v42.services.consolidation import ConsolidationJob

pytestmark = pytest.mark.integration


class _MockMCP:
    def __init__(self) -> None:
        self.registered: dict[str, Any] = {}

    def tool(self, **_kwargs: Any) -> Any:
        def decorator(fn: Any) -> Any:
            self.registered[fn.__name__] = fn
            return fn

        return decorator


class _FailAfterRealUpdatesLogRepo(PgConsolidationLogRepo):
    """Failure injector that first proves both updates are visible in-transaction."""

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
        del entity_type, similarity, action
        result = await session.execute(
            sa.select(
                decisions.c.id,
                decisions.c.tags,
                decisions.c.merged_into,
                decisions.c.freshness_status,
            ).where(decisions.c.id.in_((source_id, target_id)))
        )
        rows = {row["id"]: row for row in result.mappings().all()}
        assert rows[source_id]["merged_into"] == target_id
        assert rows[source_id]["freshness_status"] == "archived"
        assert set(rows[target_id]["tags"] or []) == {"source", "target"}
        raise RuntimeError("injected audit failure")


async def _create_decision(
    repo: PgDecisionRepo,
    *,
    project_key: str,
    title: str,
    tags: list[str],
) -> UUID:
    decision = await repo.create(
        DecisionCreate(
            title=title,
            description=f"{title} description",
            reasoning="COR3 isolated runtime proof",
            project_key=project_key,
            tags=tags,
        )
    )
    return decision.id


def _merge_tool(
    session_factory: async_sessionmaker[AsyncSession],
    job: ConsolidationJob,
) -> Any:
    from brain_v42.mcp.tools.decay_tools import register_decay_tools

    mcp = _MockMCP()
    register_decay_tools(mcp, session_factory, consolidation_job=job)
    return mcp.registered["brain_merge_entities"]


async def _cleanup_postgres(
    session_factory: async_sessionmaker[AsyncSession],
    entity_ids: list[UUID],
) -> None:
    async with session_factory() as session:
        await session.execute(
            sa.delete(consolidation_log).where(
                sa.or_(
                    consolidation_log.c.source_id.in_(entity_ids),
                    consolidation_log.c.target_id.in_(entity_ids),
                )
            )
        )
        await session.execute(sa.delete(decisions).where(decisions.c.id.in_(entity_ids)))
        await session.commit()


async def _cleanup_neo4j(neo4j_driver: Any, entity_ids: list[UUID], project_key: str) -> None:
    async with neo4j_driver.session() as session:
        await session.run(
            "MATCH (n) WHERE n.id IN $ids OR n.project_key = $project_key DETACH DELETE n",
            {"ids": [str(entity_id) for entity_id in entity_ids], "project_key": project_key},
        )


async def _edge_count(neo4j_driver: Any, source_id: UUID, target_id: UUID) -> int:
    async with neo4j_driver.session() as session:
        result = await session.run(
            "MATCH (source {id: $source_id})-[relation:MERGED_INTO]->"
            "(target {id: $target_id}) RETURN count(relation) AS edge_count",
            {"source_id": str(source_id), "target_id": str(target_id)},
        )
        record = await result.single()
    return int(record["edge_count"])


async def _prepare_graph_nodes(
    graph_service: Any,
    neo4j_driver: Any,
    source_id: UUID,
    target_id: UUID,
    *,
    project_key: str,
    scoped: bool,
) -> None:
    await graph_service.upsert_node("Decision", source_id, {"title": "COR3 source"})
    await graph_service.upsert_node("Decision", target_id, {"title": "COR3 target"})
    if not scoped:
        return
    async with neo4j_driver.session() as session:
        await session.run(
            "MERGE (:Project {project_key: $project_key})",
            {"project_key": project_key},
        )
    await graph_service.link_to_project(source_id, project_key)
    await graph_service.link_to_project(target_id, project_key)


@pytest.mark.asyncio
async def test_real_postgres_audit_failure_rolls_back_both_entity_updates(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    project_key = f"integ-cor3-rollback-{uuid4().hex[:8]}"
    repo = PgDecisionRepo(session_factory)
    source_id = await _create_decision(
        repo,
        project_key=project_key,
        title="COR3 rollback source",
        tags=["source"],
    )
    target_id = await _create_decision(
        repo,
        project_key=project_key,
        title="COR3 rollback target",
        tags=["target"],
    )
    ids = [source_id, target_id]
    job = ConsolidationJob(
        session_factory,
        _FailAfterRealUpdatesLogRepo(session_factory),
    )

    try:
        with pytest.raises(RuntimeError, match="injected audit failure"):
            await job.merge("decision", source_id, target_id)

        async with session_factory() as observer:
            result = await observer.execute(
                sa.select(
                    decisions.c.id,
                    decisions.c.tags,
                    decisions.c.merged_into,
                    decisions.c.freshness_status,
                ).where(decisions.c.id.in_(ids))
            )
            rows = {row["id"]: row for row in result.mappings().all()}
            audit_count = (
                await observer.execute(
                    sa.select(sa.func.count())
                    .select_from(consolidation_log)
                    .where(consolidation_log.c.source_id == source_id)
                )
            ).scalar_one()

        assert rows[source_id]["tags"] == ["source"]
        assert rows[source_id]["merged_into"] is None
        assert rows[source_id]["freshness_status"] == "fresh"
        assert rows[target_id]["tags"] == ["target"]
        assert audit_count == 0
    finally:
        await _cleanup_postgres(session_factory, ids)


@pytest.mark.asyncio
async def test_admin_mcp_merge_writes_neo4j_edge_immediately(
    session_factory: async_sessionmaker[AsyncSession],
    graph_service: Any,
    neo4j_driver: Any,
) -> None:
    project_key = f"integ-cor3-admin-{uuid4().hex[:8]}"
    repo = PgDecisionRepo(session_factory)
    source_id = await _create_decision(
        repo,
        project_key=project_key,
        title="COR3 admin source",
        tags=["source"],
    )
    target_id = await _create_decision(
        repo,
        project_key=project_key,
        title="COR3 admin target",
        tags=["target"],
    )
    ids = [source_id, target_id]
    job = ConsolidationJob(
        session_factory,
        PgConsolidationLogRepo(session_factory),
        graph=graph_service,
    )
    merge = _merge_tool(session_factory, job)

    try:
        await _prepare_graph_nodes(
            graph_service,
            neo4j_driver,
            source_id,
            target_id,
            project_key=project_key,
            scoped=False,
        )

        result = await merge("decision", str(source_id), str(target_id))

        assert result.startswith("ok Merged")
        assert await _edge_count(neo4j_driver, source_id, target_id) == 1
        assert (await repo.get_by_id(source_id)).merged_into == target_id  # type: ignore[union-attr]
    finally:
        try:
            await _cleanup_neo4j(neo4j_driver, ids, project_key)
        finally:
            await _cleanup_postgres(session_factory, ids)


@pytest.mark.asyncio
async def test_scoped_mcp_merge_writes_project_bounded_edge_immediately(
    session_factory: async_sessionmaker[AsyncSession],
    graph_service: Any,
    neo4j_driver: Any,
) -> None:
    project_key = f"integ-cor3-scoped-{uuid4().hex[:8]}"
    repo = PgDecisionRepo(session_factory)
    source_id = await _create_decision(
        repo,
        project_key=project_key,
        title="COR3 scoped source",
        tags=["source"],
    )
    target_id = await _create_decision(
        repo,
        project_key=project_key,
        title="COR3 scoped target",
        tags=["target"],
    )
    ids = [source_id, target_id]
    job = ConsolidationJob(
        session_factory,
        PgConsolidationLogRepo(session_factory),
        graph=graph_service,
    )
    merge = _merge_tool(session_factory, job)
    scope = DreamProjectScope(
        project_key=project_key,
        resolver=PostgresDreamProjectResolver(session_factory),
        audit=DreamProjectAudit(principal="dream-codex-synth", phase="synth"),
        tool_name="brain_merge_entities",
    )

    try:
        await _prepare_graph_nodes(
            graph_service,
            neo4j_driver,
            source_id,
            target_id,
            project_key=project_key,
            scoped=True,
        )

        with bind_dream_project_scope(scope):
            result = await merge("decision", str(source_id), str(target_id))

        assert result.startswith("ok Merged")
        assert await _edge_count(neo4j_driver, source_id, target_id) == 1
        assert (await repo.get_by_id(source_id)).merged_into == target_id  # type: ignore[union-attr]
    finally:
        try:
            await _cleanup_neo4j(neo4j_driver, ids, project_key)
        finally:
            await _cleanup_postgres(session_factory, ids)
