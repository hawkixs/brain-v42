"""Integration: BrainService.search returns plan chunks."""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brain_v42.models.indexed_plan import IndexedPlanCreate
from brain_v42.models.indexed_plan_chunk import IndexedPlanChunkCreate
from brain_v42.repositories.pg_indexed_plan_repo import PgIndexedPlanRepo
from brain_v42.services.brain_service import BrainService
from brain_v42.services.indexed_plan_search_service import IndexedPlanSearchService
from brain_v42.services.search.hybrid import HybridSearcher

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# brain_service fixture (plan-only, no embedding svc needed for FTS)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def brain_service(
    session_factory: async_sessionmaker[AsyncSession],
) -> BrainService:
    """BrainService wired with only IndexedPlanSearchService for plan search tests.

    Other services use stub objects (duck-typed with empty search/semantic_search).
    No embedding service is provided (None) — plan FTS does not require embeddings.
    """

    class _StubSvc:
        """Stub for services not under test — returns empty results."""

        async def search(self, *args, **kwargs):  # noqa: ANN002, ANN003
            return []

        async def semantic_search(self, *args, **kwargs):  # noqa: ANN002, ANN003
            return []

    plan_svc = IndexedPlanSearchService(session_factory)

    return BrainService(
        decision_svc=_StubSvc(),
        learning_svc=_StubSvc(),
        snippet_svc=_StubSvc(),
        runbook_svc=_StubSvc(),
        adr_svc=_StubSvc(),
        embedding_svc=None,
        min_score=0.0,  # no threshold — FTS scores can be very small
        hybrid_searcher=HybridSearcher(),  # enables FTS path (no reranker needed)
        plan_search_svc=plan_svc,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_brain_search_returns_plan_chunks(brain_service, db_session):
    repo = PgIndexedPlanRepo(db_session)
    plan_embed = [0.10] * 1536
    chunk_embed = [0.11] * 1536

    plan = IndexedPlanCreate(
        file_path="/tmp/search-plan.md",
        title="Neo4j Knowledge Graph",
        plan_type="plan",
        project_key="brain-v42",
        content_hash="h" * 64,
        content="# Neo4j Knowledge Graph\n\n## Traversals\n\n" + ("traversal words " * 40),
        chunk_count=1,
        word_count=80,
    )
    chunk = IndexedPlanChunkCreate(
        section_title="Traversals",
        section_path="Traversals",
        content="## Traversals\n\n" + ("traversal words " * 40),
        section_order=0,
        word_count=80,
        project_key="brain-v42",
        plan_type="plan",
        tags=["neo4j"],
    )
    plan_id = await repo.upsert_plan_with_chunks(plan, plan_embed, [chunk], [chunk_embed])

    try:
        response = await brain_service.search(
            query="traversals",
            types=["plan"],
            project_key="brain-v42",
            limit=5,
        )

        plan_results = [r for r in response.results if r.type == "plan"]
        assert len(plan_results) >= 1
        assert plan_results[0].title == "Traversals"
        assert plan_results[0].parent_id is not None
    finally:
        await repo.delete(plan_id)


async def test_brain_search_filters_drafts_by_default(brain_service, db_session):
    repo = PgIndexedPlanRepo(db_session)
    created_ids = []

    try:
        for status, title in [("draft", "Draft Plan"), ("active", "Active Plan")]:
            plan = IndexedPlanCreate(
                file_path=f"/tmp/{status}_filter.md",
                title=title,
                plan_type="plan",
                project_key="brain-v42",
                content_hash=(status[0] * 64),
                content=f"# {title}\n\n## Body\n\n" + ("keyword " * 60),
                status=status,
                chunk_count=1,
                word_count=60,
            )
            chunk = IndexedPlanChunkCreate(
                section_title="Body",
                section_path="Body",
                content="## Body\n\n" + ("keyword " * 60),
                section_order=0,
                word_count=60,
                project_key="brain-v42",
                plan_type="plan",
                status=status,
            )
            pid = await repo.upsert_plan_with_chunks(plan, [0.1] * 1536, [chunk], [[0.1] * 1536])
            created_ids.append(pid)

        response = await brain_service.search(
            query="keyword", types=["plan"], project_key="brain-v42", limit=10
        )
        plan_hits = [r for r in response.results if r.type == "plan" and r.title == "Body"]
        # Only one match expected — drafts excluded
        assert len(plan_hits) == 1
    finally:
        for pid in created_ids:
            await repo.delete(pid)


async def test_brain_search_filters_by_project_key(brain_service, db_session):
    repo = PgIndexedPlanRepo(db_session)
    created_ids = []

    try:
        for pk, title in [("brain-v42", "A"), ("other-project", "B")]:
            plan = IndexedPlanCreate(
                file_path=f"/tmp/{pk}_scope.md",
                title=f"Plan {title}",
                plan_type="plan",
                project_key=pk,
                content_hash=pk[:8] * 8,
                content=f"# {title}\n\n## Sec\n\n" + ("alpha " * 60),
                chunk_count=1,
                word_count=60,
            )
            chunk = IndexedPlanChunkCreate(
                section_title="Sec",
                section_path="Sec",
                content="## Sec\n\n" + ("alpha " * 60),
                section_order=0,
                word_count=60,
                project_key=pk,
                plan_type="plan",
            )
            pid = await repo.upsert_plan_with_chunks(plan, [0.1] * 1536, [chunk], [[0.1] * 1536])
            created_ids.append(pid)

        response = await brain_service.search(
            query="alpha", types=["plan"], project_key="brain-v42", limit=10
        )
        project_keys = {r.project_key for r in response.results if r.type == "plan"}
        assert "brain-v42" in project_keys
        assert "other-project" not in project_keys
    finally:
        for pid in created_ids:
            await repo.delete(pid)
