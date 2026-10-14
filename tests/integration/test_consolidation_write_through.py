"""Runtime proofs for atomic PostgreSQL merge and immediate Neo4j write-through."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
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
from brain_v42.repositories import pg_decision
from brain_v42.repositories.pg_consolidation_log import PgConsolidationLogRepo
from brain_v42.repositories.pg_decision import PgDecisionRepo
from brain_v42.services import consolidation
from brain_v42.services.consolidation import ConsolidationJob

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def merge_project(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[str]:
    """Remove only this test's rows so concurrent shared-database runs stay isolated."""
    project_key = f"integ-merge-supersede-{uuid4().hex[:12]}"
    try:
        yield project_key
    finally:
        async with session_factory() as session:
            ids = list(
                (
                    await session.execute(
                        sa.select(decisions.c.id).where(
                            decisions.c.project_key.in_([project_key, f"{project_key}-foreign"])
                        )
                    )
                )
                .scalars()
                .all()
            )
        await _cleanup_postgres(session_factory, ids)


@pytest.mark.asyncio
async def test_real_merge_flattens_two_children_and_audits_each(
    session_factory: async_sessionmaker[AsyncSession],
    merge_project: str,
) -> None:
    repo = PgDecisionRepo(session_factory)
    source, target, first, second = [
        await _create_decision(repo, project_key=merge_project, title=title, tags=[])
        for title in ("Source", "Target", "First child", "Second child")
    ]
    job = ConsolidationJob(session_factory, PgConsolidationLogRepo(session_factory))
    await job.merge("decision", first, source)
    await job.merge("decision", second, source)

    await job.merge("decision", source, target)

    async with session_factory() as observer:
        rows = (
            (
                await observer.execute(
                    sa.select(decisions).where(decisions.c.project_key == merge_project)
                )
            )
            .mappings()
            .all()
        )
        assert {row["merged_into"] for row in rows if row["id"] != target} == {target}
        parent = decisions.alias("parent")
        chains = (
            await observer.execute(
                sa.select(sa.func.count())
                .select_from(decisions.join(parent, decisions.c.merged_into == parent.c.id))
                .where(decisions.c.project_key == merge_project, parent.c.merged_into.is_not(None))
            )
        ).scalar_one()
        assert chains == 0
        audits = (
            (
                await observer.execute(
                    sa.select(consolidation_log).where(
                        consolidation_log.c.source_id.in_([first, second]),
                        consolidation_log.c.target_id == target,
                        consolidation_log.c.action == "repointed",
                    )
                )
            )
            .mappings()
            .all()
        )
        assert {row["source_id"] for row in audits} == {first, second}


@pytest.mark.asyncio
async def test_real_merge_rereads_child_committed_after_lock_snapshot(
    session_factory: async_sessionmaker[AsyncSession],
    merge_project: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A child committed while the lock SELECT waits is absent from its snapshot.

    merged_into has a foreign key, so the child's writer holds FOR KEY SHARE on
    the source until it commits, and the merge's FOR UPDATE waits for it. The
    lock statement's snapshot predates that commit: the child is not in the
    locked set. Holding the writer open until the merge is seen waiting makes
    that interleaving deterministic, and it is the only one the foreign key
    leaves open (once the source is locked, no new child can commit).
    """
    repo = PgDecisionRepo(session_factory)
    source, target, child = [
        await _create_decision(repo, project_key=merge_project, title=title, tags=[])
        for title in ("Source", "Target", "Late child")
    ]
    execute = AsyncSession.execute
    lock_passes = 0
    rereads = 0

    async def count_passes(session: AsyncSession, statement: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal lock_passes, rereads
        sql = str(statement)
        if sql.startswith("SELECT") and "FOR UPDATE" in sql:
            lock_passes += 1
        elif sql.startswith("SELECT") and "NOT IN" in sql:
            rereads += 1
        return await execute(session, statement, *args, **kwargs)

    async def wait_for_lock_waiter(observer: AsyncSession, writer_pid: int) -> None:
        # brain_test is shared: only a backend blocked by THIS writer counts.
        for _ in range(200):
            waiting = await execute(
                observer,
                sa.text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE :writer = ANY(pg_blocking_pids(pid))"
                ),
                {"writer": writer_pid},
            )
            if waiting.scalar_one() > 0:
                return
            await asyncio.sleep(0.05)
        raise AssertionError("the merge never waited for the child's writer")

    with monkeypatch.context() as patch:
        patch.setattr(AsyncSession, "execute", count_passes)
        async with session_factory() as writer, session_factory() as observer:
            writer_pid = (await execute(writer, sa.text("SELECT pg_backend_pid()"))).scalar_one()
            await execute(
                writer,
                sa.update(decisions)
                .where(decisions.c.id == child)
                .values(
                    merged_into=source,
                    freshness_status="archived",
                    freshness_source="merge",
                ),
            )
            merge = asyncio.create_task(
                ConsolidationJob(session_factory, PgConsolidationLogRepo(session_factory)).merge(
                    "decision", source, target
                )
            )
            try:
                await wait_for_lock_waiter(observer, writer_pid)
            finally:
                await writer.commit()
            await asyncio.wait_for(merge, timeout=30)

    assert lock_passes == 2
    assert rereads == 2
    async with session_factory() as observer:
        assert (
            await observer.execute(
                sa.select(decisions.c.merged_into).where(decisions.c.id == child)
            )
        ).scalar_one() == target
        parent = decisions.alias("parent")
        assert (
            await observer.execute(
                sa.select(sa.func.count())
                .select_from(decisions.join(parent, decisions.c.merged_into == parent.c.id))
                .where(decisions.c.project_key == merge_project, parent.c.merged_into.is_not(None))
            )
        ).scalar_one() == 0
        assert (
            await observer.execute(
                sa.select(sa.func.count())
                .select_from(consolidation_log)
                .where(
                    consolidation_log.c.source_id == child,
                    consolidation_log.c.target_id == target,
                    consolidation_log.c.action == "repointed",
                )
            )
        ).scalar_one() == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["source", "target"])
async def test_real_merged_endpoint_rejection_preserves_rows_and_audit(
    session_factory: async_sessionmaker[AsyncSession],
    merge_project: str,
    endpoint: str,
) -> None:
    repo = PgDecisionRepo(session_factory)
    source, target, survivor = [
        await _create_decision(repo, project_key=merge_project, title=title, tags=[])
        for title in ("Source", "Target", "Survivor")
    ]
    job = ConsolidationJob(session_factory, PgConsolidationLogRepo(session_factory))
    await job.merge("decision", source if endpoint == "source" else target, survivor)
    before = [item.model_dump() for item in await repo.list_all(project_key=merge_project)]

    with pytest.raises(consolidation.ConsolidationEndpointMergedError):
        await job.merge("decision", source, target)

    after = [item.model_dump() for item in await repo.list_all(project_key=merge_project)]
    assert after == before
    async with session_factory() as observer:
        assert (
            await observer.execute(
                sa.select(sa.func.count())
                .select_from(consolidation_log)
                .where(consolidation_log.c.source_id.in_([source, target]))
            )
        ).scalar_one() == 1


@pytest.mark.asyncio
async def test_real_child_audit_failure_rolls_back_the_entire_merge(
    session_factory: async_sessionmaker[AsyncSession],
    merge_project: str,
) -> None:
    repo = PgDecisionRepo(session_factory)
    source, target, child = [
        await _create_decision(repo, project_key=merge_project, title=title, tags=[])
        for title in ("Source", "Target", "Child")
    ]
    log_repo = PgConsolidationLogRepo(session_factory)
    await ConsolidationJob(session_factory, log_repo).merge("decision", child, source)
    before = [item.model_dump() for item in await repo.list_all(project_key=merge_project)]

    class FailChildAudit(PgConsolidationLogRepo):
        """Fail after the child audit to prove it shares the entity transaction."""

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
            await super().log_action_in_session(
                session,
                source_id=source_id,
                target_id=target_id,
                entity_type=entity_type,
                similarity=similarity,
                action=action,
            )
            if action == "repointed":
                raise RuntimeError("injected child audit failure")

    job = ConsolidationJob(session_factory, FailChildAudit(session_factory))
    with pytest.raises(RuntimeError, match="injected child audit failure"):
        await job.merge("decision", source, target)

    assert [item.model_dump() for item in await repo.list_all(project_key=merge_project)] == before
    async with session_factory() as observer:
        assert (
            await observer.execute(
                sa.select(sa.func.count())
                .select_from(consolidation_log)
                .where(
                    consolidation_log.c.source_id.in_([source, child]),
                    consolidation_log.c.target_id == target,
                )
            )
        ).scalar_one() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_old", ["missing", "superseded"])
async def test_real_supersede_rejection_rolls_back_replacement(
    session_factory: async_sessionmaker[AsyncSession],
    merge_project: str,
    invalid_old: str,
) -> None:
    repo = PgDecisionRepo(session_factory)
    old_id = uuid4()
    old_project = merge_project
    if invalid_old != "missing":
        old_id = await _create_decision(repo, project_key=old_project, title="Old", tags=[])
        if invalid_old == "superseded":
            async with session_factory() as session:
                await session.execute(
                    sa.update(decisions).where(decisions.c.id == old_id).values(status="superseded")
                )
                await session.commit()
    before = await repo.get_by_id(old_id)
    data = DecisionCreate(
        title="Replacement", description="Context", reasoning="Reason", project_key=merge_project
    )

    with pytest.raises(pg_decision.DecisionNotSupersedableError) as raised:
        await repo.supersede(old_id, data)

    assert type(raised.value) is pg_decision.DecisionNotSupersedableError
    assert str(raised.value) == f"Decision {old_id} does not exist or is already superseded"
    assert await repo.get_by_id(old_id) == before
    async with session_factory() as observer:
        assert (
            await observer.execute(
                sa.select(sa.func.count())
                .select_from(decisions)
                .where(decisions.c.project_key == merge_project, decisions.c.title == "Replacement")
            )
        ).scalar_one() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("old_scope", "replacement_scope"),
    [("project", "foreign"), ("project", "unscoped"), ("unscoped", "project")],
)
async def test_real_cross_project_supersede_refusal_writes_nothing(
    session_factory: async_sessionmaker[AsyncSession],
    merge_project: str,
    old_scope: str,
    replacement_scope: str,
) -> None:
    """NULL is a project boundary too; every refused replacement must disappear."""
    repo = PgDecisionRepo(session_factory)
    projects = {"project": merge_project, "foreign": f"{merge_project}-foreign", "unscoped": None}
    old_title = f"{merge_project} old"
    replacement_title = f"{merge_project} replacement"
    try:
        old = await repo.create(
            DecisionCreate(
                title=old_title,
                description="Private old context",
                reasoning="Project isolation",
                project_key=projects[old_scope],
            )
        )
        data = DecisionCreate(
            title=replacement_title,
            description="Replacement context",
            reasoning="Project isolation",
            project_key=projects[replacement_scope],
        )
        with pytest.raises(pg_decision.DecisionCrossProjectSupersedeError) as raised:
            await repo.supersede(old.id, data)

        assert (
            str(raised.value)
            == "The old decision belongs to a different project scope; supersede it from its own project (same project_key)"
        )
        assert await repo.get_by_id(old.id) == old
        async with session_factory() as observer:
            assert (
                await observer.execute(
                    sa.select(sa.func.count())
                    .select_from(decisions)
                    .where(decisions.c.title == replacement_title)
                )
            ).scalar_one() == 0
    finally:
        # Unique titles also find unscoped rows and any replacement left by a regression.
        async with session_factory() as observer:
            ids = list(
                (
                    await observer.execute(
                        sa.select(decisions.c.id).where(
                            decisions.c.title.in_([old_title, replacement_title])
                        )
                    )
                )
                .scalars()
                .all()
            )
        await _cleanup_postgres(session_factory, ids)


@pytest.mark.asyncio
async def test_real_caller_owned_cross_project_supersede_rolls_back_savepoint(
    session_factory: async_sessionmaker[AsyncSession],
    merge_project: str,
) -> None:
    """Catching a refusal and committing the caller's transaction cannot leave a replacement."""
    repo = PgDecisionRepo(session_factory)
    old_id = await _create_decision(repo, project_key=merge_project, title="Old", tags=[])
    before = await repo.get_by_id(old_id)
    data = DecisionCreate(
        title="Caller replacement",
        description="Context",
        reasoning="Project isolation",
        project_key=f"{merge_project}-foreign",
    )

    async with session_factory() as session, session.begin():
        with pytest.raises(pg_decision.DecisionCrossProjectSupersedeError):
            await repo.supersede(old_id, data, session=session)
        assert (
            await session.execute(
                sa.select(sa.func.count())
                .select_from(decisions)
                .where(decisions.c.project_key == data.project_key)
            )
        ).scalar_one() == 0
        assert await repo.get_by_id(old_id, session=session) == before

    assert await repo.get_by_id(old_id) == before
    assert await repo.list_all(project_key=data.project_key) == []


@pytest.mark.asyncio
async def test_real_unscoped_supersede_updates_old_decision(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """NULL-safe equality permits supersession when neither decision has a project."""
    repo = PgDecisionRepo(session_factory)
    marker = uuid4().hex
    titles = [f"Unscoped old {marker}", f"Unscoped replacement {marker}"]
    try:
        old = await repo.create(
            DecisionCreate(title=titles[0], description="Context", reasoning="Unscoped proof")
        )
        replacement = await repo.supersede(
            old.id,
            DecisionCreate(title=titles[1], description="Context", reasoning="Unscoped proof"),
        )

        updated = await repo.get_by_id(old.id)
        assert updated is not None
        assert updated.status == "superseded"
        assert updated.superseded_by == replacement.id
        assert updated.project_key is None
        assert replacement.project_key is None
        assert await repo.get_by_id(replacement.id) == replacement
    finally:
        async with session_factory() as observer:
            ids = list(
                (
                    await observer.execute(
                        sa.select(decisions.c.id).where(decisions.c.title.in_(titles))
                    )
                )
                .scalars()
                .all()
            )
        await _cleanup_postgres(session_factory, ids)


@pytest.mark.asyncio
async def test_real_valid_supersede_updates_exactly_one_old_decision(
    session_factory: async_sessionmaker[AsyncSession],
    merge_project: str,
) -> None:
    repo = PgDecisionRepo(session_factory)
    old_id = await _create_decision(repo, project_key=merge_project, title="Old", tags=[])
    data = DecisionCreate(
        title="Replacement", description="Context", reasoning="Reason", project_key=merge_project
    )

    replacement = await repo.supersede(old_id, data)

    old = await repo.get_by_id(old_id)
    assert old is not None
    assert old.status == "superseded"
    assert old.superseded_by == replacement.id
    assert replacement.project_key == merge_project


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
