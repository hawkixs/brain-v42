"""COR1 integration: plan search usage refreshes the canonical parent."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brain_v42.db.tables import access_log, indexed_plan_chunks, indexed_plans
from brain_v42.models.indexed_plan import IndexedPlanCreate
from brain_v42.models.indexed_plan_chunk import IndexedPlanChunkCreate
from brain_v42.repositories.pg_access_log import PgAccessLogRepo
from brain_v42.repositories.pg_indexed_plan_repo import PgIndexedPlanRepo
from brain_v42.services.access_logger import AccessLogger
from brain_v42.services.brain_service import BrainService
from brain_v42.services.decay import DecayCalculator
from brain_v42.services.decay_flusher import DecayFlusher
from brain_v42.services.indexed_plan_search_service import IndexedPlanSearchService
from brain_v42.services.search.hybrid import HybridSearcher

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


class _EmptySearchService:
    async def semantic_search(self, *_args, **_kwargs):  # noqa: ANN002, ANN003
        return []

    async def search(self, *_args, **_kwargs):  # noqa: ANN002, ANN003
        return []


class _FixedEmbeddingService:
    def __init__(self, embedding: list[float]) -> None:
        self._embedding = embedding

    async def embed(self, _query: str) -> list[float]:
        return self._embedding


def _plan_payload(*, marker: str, project_key: str, title: str) -> IndexedPlanCreate:
    return IndexedPlanCreate(
        file_path=f"/tmp/cor1-{marker}.md",
        title=title,
        plan_type="plan",
        project_key=project_key,
        content_hash=(marker * 64)[:64],
        content=f"# {title}",
        status="active",
        chunk_count=1,
        word_count=3,
    )


def _plan_chunk(*, content: str, project_key: str) -> IndexedPlanChunkCreate:
    return IndexedPlanChunkCreate(
        section_title="Evidence",
        section_path="evidence",
        content=content,
        section_order=0,
        word_count=3,
        project_key=project_key,
        plan_type="plan",
    )


async def _delete_plans(
    session_factory: async_sessionmaker[AsyncSession],
    plan_ids: list,
) -> None:
    if not plan_ids:
        return
    async with session_factory() as session:
        await session.execute(
            sa.delete(access_log).where(
                access_log.c.entity_type == "plan",
                access_log.c.entity_id.in_(plan_ids),
            )
        )
        await session.execute(sa.delete(indexed_plans).where(indexed_plans.c.id.in_(plan_ids)))
        await session.commit()


async def test_duplicate_plan_hits_refresh_parent_without_touching_chunks(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Search -> queue -> access_log -> flusher restores one stale parent."""
    marker = uuid4().hex
    project_key = f"integ-cor1-{marker[:12]}"
    plan_data = IndexedPlanCreate(
        file_path=f"/tmp/cor1-{marker}.md",
        title="COR1 plan usage evidence",
        plan_type="plan",
        project_key=project_key,
        content_hash=marker * 2,
        content="# COR1\n\n## First\nusageproof repeated\n\n## Second\nusageproof repeated",
        status="active",
        chunk_count=2,
        word_count=6,
    )
    chunks = [
        IndexedPlanChunkCreate(
            section_title=f"Section {index}",
            section_path=f"section-{index}",
            content=f"usageproof repeated evidence section {index}",
            section_order=index,
            word_count=5,
            project_key=project_key,
            plan_type="plan",
        )
        for index in range(2)
    ]
    old_created_at = datetime.now(tz=UTC) - timedelta(days=730)
    plan_id = None

    try:
        async with session_factory() as session:
            repo = PgIndexedPlanRepo(session)
            plan_id = await repo.upsert_plan_with_chunks(
                plan_data,
                [0.1] * 1536,
                chunks,
                [[0.1] * 1536 for _chunk in chunks],
            )
            await session.execute(
                sa.update(indexed_plans)
                .where(indexed_plans.c.id == plan_id)
                .values(
                    created_at=old_created_at,
                    access_count=0,
                    last_accessed_at=None,
                    freshness_status="stale",
                )
            )
            await session.commit()

        access_logger = AccessLogger(session_factory)
        decay_calculator = DecayCalculator()
        empty = _EmptySearchService()
        brain = BrainService(
            decision_svc=empty,
            learning_svc=empty,
            snippet_svc=empty,
            runbook_svc=empty,
            adr_svc=empty,
            embedding_svc=None,
            min_score=0.0,
            hybrid_searcher=HybridSearcher(),
            plan_search_svc=IndexedPlanSearchService(session_factory),
            access_logger=access_logger,
            decay_calculator=decay_calculator,
        )

        response = await brain.search(
            "usageproof",
            types=["plan"],
            project_key=project_key,
            limit=10,
        )

        assert len(response.results) == 2
        assert {result.parent_id for result in response.results} == {plan_id}
        assert access_logger._queue.qsize() == 1
        before_multiplier = response.results[0].item["_decay_multiplier"]

        await access_logger._flush_batch()
        async with session_factory() as session:
            queued = await session.scalar(
                sa.select(sa.func.count())
                .select_from(access_log)
                .where(
                    access_log.c.entity_type == "plan",
                    access_log.c.entity_id == plan_id,
                )
            )
        assert queued == 1

        access_repo = PgAccessLogRepo(session_factory)
        flusher = DecayFlusher(session_factory, access_repo, decay_calculator)
        await flusher._flush()

        refreshed_response = await brain.search(
            "usageproof",
            types=["plan"],
            project_key=project_key,
            limit=10,
        )

        async with session_factory() as session:
            parent = (
                await session.execute(
                    sa.select(
                        indexed_plans.c.access_count,
                        indexed_plans.c.last_accessed_at,
                        indexed_plans.c.freshness_status,
                    ).where(indexed_plans.c.id == plan_id)
                )
            ).one()
            chunk_counts = (
                await session.scalars(
                    sa.select(indexed_plan_chunks.c.access_count)
                    .where(indexed_plan_chunks.c.plan_id == plan_id)
                    .order_by(indexed_plan_chunks.c.section_order)
                )
            ).all()
            residual = await session.scalar(
                sa.select(sa.func.count())
                .select_from(access_log)
                .where(
                    access_log.c.entity_type == "plan",
                    access_log.c.entity_id == plan_id,
                )
            )

        assert parent.access_count == 1
        assert parent.last_accessed_at is not None
        assert parent.last_accessed_at > old_created_at
        assert parent.freshness_status == "fresh"
        assert chunk_counts == [0, 0]
        assert residual == 0
        assert refreshed_response.results[0].item["_decay_multiplier"] > before_multiplier
    finally:
        if plan_id is not None:
            async with session_factory() as session:
                await session.execute(
                    sa.delete(access_log).where(
                        access_log.c.entity_type == "plan",
                        access_log.c.entity_id == plan_id,
                    )
                )
                await session.execute(sa.delete(indexed_plans).where(indexed_plans.c.id == plan_id))
                await session.commit()


async def test_project_scoped_search_rejects_chunk_owned_by_foreign_parent(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A forged chunk project key cannot expose or refresh its foreign parent."""
    marker = uuid4().hex
    owned_project = f"integ-cor1-owned-{marker[:10]}"
    foreign_project = f"integ-cor1-foreign-{marker[:10]}"
    proof_word = f"ownershipproof{marker[:12]}"
    foreign_plan_id = None

    try:
        async with session_factory() as session:
            repo = PgIndexedPlanRepo(session)
            foreign_plan_id = await repo.upsert_plan_with_chunks(
                _plan_payload(
                    marker=f"foreign-{marker}",
                    project_key=foreign_project,
                    title="Foreign COR1 parent",
                ),
                [0.1] * 1536,
                [_plan_chunk(content=proof_word, project_key=foreign_project)],
                [[0.1] * 1536],
            )
            await session.execute(
                sa.update(indexed_plan_chunks)
                .where(indexed_plan_chunks.c.plan_id == foreign_plan_id)
                .values(project_key=owned_project)
            )
            await session.execute(
                sa.update(indexed_plans)
                .where(indexed_plans.c.id == foreign_plan_id)
                .values(
                    access_count=0,
                    last_accessed_at=None,
                    freshness_status="stale",
                )
            )
            await session.commit()

        access_logger = AccessLogger(session_factory)
        decay_calculator = DecayCalculator()
        empty = _EmptySearchService()
        brain = BrainService(
            decision_svc=empty,
            learning_svc=empty,
            snippet_svc=empty,
            runbook_svc=empty,
            adr_svc=empty,
            embedding_svc=None,
            min_score=0.0,
            hybrid_searcher=HybridSearcher(),
            plan_search_svc=IndexedPlanSearchService(session_factory),
            access_logger=access_logger,
            decay_calculator=decay_calculator,
        )

        response = await brain.search(
            proof_word,
            types=["plan"],
            project_key=owned_project,
            limit=1,
        )
        assert response.results == []
        assert access_logger._queue.empty()

        await access_logger._flush_batch()
        flusher = DecayFlusher(
            session_factory,
            PgAccessLogRepo(session_factory),
            decay_calculator,
        )
        await flusher._flush()

        async with session_factory() as session:
            parent = (
                await session.execute(
                    sa.select(
                        indexed_plans.c.access_count,
                        indexed_plans.c.last_accessed_at,
                        indexed_plans.c.freshness_status,
                    ).where(indexed_plans.c.id == foreign_plan_id)
                )
            ).one()
            usage_count = await session.scalar(
                sa.select(sa.func.count())
                .select_from(access_log)
                .where(
                    access_log.c.entity_type == "plan",
                    access_log.c.entity_id == foreign_plan_id,
                )
            )

        assert parent.access_count == 0
        assert parent.last_accessed_at is None
        assert parent.freshness_status == "stale"
        assert usage_count == 0
    finally:
        await _delete_plans(
            session_factory,
            [foreign_plan_id] if foreign_plan_id is not None else [],
        )


async def test_archived_parent_filter_runs_before_limit_and_can_be_opted_out(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A top-ranked archive cannot consume the only SQL result slot by default."""
    marker = uuid4().hex
    project_key = f"integ-cor1-archive-{marker[:10]}"
    query_embedding = [1.0] + [0.0] * 1535
    lower_ranked_embedding = [0.0, 1.0] + [0.0] * 1534
    archived_plan_id = None
    fresh_plan_id = None

    try:
        async with session_factory() as session:
            repo = PgIndexedPlanRepo(session)
            archived_plan_id = await repo.upsert_plan_with_chunks(
                _plan_payload(
                    marker=f"archived-{marker}",
                    project_key=project_key,
                    title="Top ranked archived COR1 plan",
                ),
                query_embedding,
                [_plan_chunk(content="archive ranking proof", project_key=project_key)],
                [query_embedding],
            )
            fresh_plan_id = await repo.upsert_plan_with_chunks(
                _plan_payload(
                    marker=f"fresh-{marker}",
                    project_key=project_key,
                    title="Lower ranked fresh COR1 plan",
                ),
                lower_ranked_embedding,
                [_plan_chunk(content="fresh ranking proof", project_key=project_key)],
                [lower_ranked_embedding],
            )
            await session.execute(
                sa.update(indexed_plans)
                .where(indexed_plans.c.id == archived_plan_id)
                .values(freshness_status="archived")
            )
            await session.commit()

        empty = _EmptySearchService()
        brain = BrainService(
            decision_svc=empty,
            learning_svc=empty,
            snippet_svc=empty,
            runbook_svc=empty,
            adr_svc=empty,
            embedding_svc=_FixedEmbeddingService(query_embedding),
            min_score=0.0,
            plan_search_svc=IndexedPlanSearchService(session_factory),
        )

        default_response = await brain.search(
            "ranking proof",
            types=["plan"],
            project_key=project_key,
            limit=1,
        )
        archived_response = await brain.search(
            "ranking proof",
            types=["plan"],
            project_key=project_key,
            limit=1,
            include_archived=True,
        )

        assert [result.parent_id for result in default_response.results] == [fresh_plan_id]
        assert [result.parent_id for result in archived_response.results] == [archived_plan_id]
    finally:
        await _delete_plans(
            session_factory,
            [plan_id for plan_id in (archived_plan_id, fresh_plan_id) if plan_id is not None],
        )
