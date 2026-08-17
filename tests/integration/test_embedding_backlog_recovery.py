"""PostgreSQL + fake HTTP drill for outage recovery and idempotent backfill."""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import MagicMock

import pytest
import sqlalchemy as sa
from aiohttp import web
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brain_v42.db.tables import feature_artifacts, features, learnings
from brain_v42.metrics.collector import MetricsCollector
from brain_v42.models.adr import ADRCreate
from brain_v42.models.decision import DecisionCreate, DecisionUpdate
from brain_v42.models.learning import LearningCreate
from brain_v42.models.runbook import RunbookCreate
from brain_v42.models.snippet import SnippetCreate
from brain_v42.repositories.pg_adr import PgADRRepo
from brain_v42.repositories.pg_decision import PgDecisionRepo
from brain_v42.repositories.pg_learning import PgLearningRepo
from brain_v42.repositories.pg_runbook import PgRunbookRepo
from brain_v42.repositories.pg_snippet import PgSnippetRepo
from brain_v42.services.adr_service import ADRService
from brain_v42.services.decision_service import DecisionService
from brain_v42.services.embedding_backfill import EmbeddingBackfillJob, persist_backfill_metrics
from brain_v42.services.feature_linker import FeatureLinker
from brain_v42.services.gpu_embedding_service import GPUEmbeddingService
from brain_v42.services.learning_service import LearningService
from brain_v42.services.runbook_service import RunbookService
from brain_v42.services.snippet_service import SnippetService

pytestmark = pytest.mark.integration

VECTOR = [0.1] * 1536


@pytest.fixture
async def fake_embedding_endpoint(aiohttp_server: Any):
    state = {"available": False, "query_calls": 0, "batch_calls": 0}

    async def embed_query(request: web.Request) -> web.Response:
        state["query_calls"] += 1
        if not state["available"]:
            return web.json_response({"error": "offline"}, status=503)
        await request.json()
        return web.json_response(VECTOR)

    async def embed_batch(request: web.Request) -> web.Response:
        state["batch_calls"] += 1
        if not state["available"]:
            return web.json_response({"error": "offline"}, status=503)
        payload = await request.json()
        return web.json_response([VECTOR for _text in payload["texts"]])

    app = web.Application()
    app.router.add_post("/embed/query", embed_query)
    app.router.add_post("/embed", embed_batch)
    server = await aiohttp_server(app)
    return state, str(server.make_url("/"))


async def test_five_types_survive_outage_and_backfill_is_idempotent(
    session_factory: async_sessionmaker[AsyncSession],
    fake_embedding_endpoint: tuple[dict[str, Any], str],
) -> None:
    state, base_url = fake_embedding_endpoint
    project_key = f"integ-embed-{uuid.uuid4().hex[:8]}"
    unique_term = f"backlog{uuid.uuid4().hex[:8]}"
    embedding_svc = GPUEmbeddingService(base_url=base_url, timeout=0.5, max_retries=0)
    repos = {
        "decision": PgDecisionRepo(session_factory),
        "learning": PgLearningRepo(session_factory),
        "snippet": PgSnippetRepo(session_factory),
        "runbook": PgRunbookRepo(session_factory),
        "adr": PgADRRepo(session_factory),
    }
    services = {
        "decision": DecisionService(repos["decision"], embedding_svc),
        "learning": LearningService(repos["learning"], embedding_svc),
        "snippet": SnippetService(repos["snippet"], embedding_svc),
        "runbook": RunbookService(repos["runbook"], embedding_svc),
        "adr": ADRService(repos["adr"], embedding_svc),
    }

    try:
        created = {
            "decision": await services["decision"].create(
                DecisionCreate(
                    title=f"Decision {unique_term}",
                    description="PostgreSQL first",
                    reasoning="Outages must not lose data",
                    project_key=project_key,
                )
            ),
            "learning": await services["learning"].create(
                LearningCreate(
                    topic="Durable learning",
                    insight="Commit before embedding",
                    project_key=project_key,
                )
            ),
            "snippet": await services["snippet"].create(
                SnippetCreate(
                    title="Durable snippet",
                    intention="Recover embeddings",
                    code="pass",
                    language="python",
                    project_key=project_key,
                )
            ),
            "runbook": await services["runbook"].create(
                RunbookCreate(
                    title="Recover embeddings",
                    description="Run the bounded worker",
                    trigger="Backlog is non-zero",
                    project_key=project_key,
                )
            ),
            "adr": await services["adr"].create(
                ADRCreate(
                    title="PostgreSQL-first embeddings",
                    context="GPU availability is variable",
                    decision="Persist before derived work",
                    consequences="A durable null-vector backlog exists",
                    project_key=project_key,
                )
            ),
        }
        feature_id = uuid.uuid4()
        async with session_factory() as session:
            await session.execute(
                features.insert().values(
                    id=feature_id,
                    project_key=project_key,
                    name=f"Feature {unique_term}",
                    description="AV1 real linker integration target",
                    status="building",
                    embedding=VECTOR,
                )
            )
            await session.commit()

        expected_links = {(entity_type, entity.id) for entity_type, entity in created.items()}
        assert all(entity.embedding is None for entity in created.values())

        metrics_engine = MagicMock()
        metrics_engine.sync_engine.pool.size.return_value = 5
        metrics_engine.sync_engine.pool.checkedout.return_value = 0
        metrics_engine.sync_engine.pool.checkedin.return_value = 1
        metrics_engine.sync_engine.pool.overflow.return_value = -4
        metrics_engine.sync_engine.pool._max_overflow = 10
        database_metrics = await MetricsCollector(
            engine=metrics_engine, session_factory=session_factory
        ).collect_db_stats()
        assert database_metrics["embedding_backlog"]["total"] >= 5
        assert database_metrics["embedding_backlog"]["by_entity_type"]["decision"]["count"] >= 1

        fts = await repos["decision"].search_fts(unique_term, project_key=project_key)
        assert [decision.id for decision, _score in fts] == [created["decision"].id]
        assert await repos["decision"].search_vector(VECTOR, project_key=project_key) == []

        state["available"] = True
        feature_linker = FeatureLinker(session_factory=session_factory)
        job = EmbeddingBackfillJob(
            session_factory=session_factory,
            repos=repos,
            embedding_svc=embedding_svc,
            feature_linker=feature_linker,
        )
        first = await job.run(project_key=project_key, batch_size=2, limit=5)
        assert sum(item.stored for item in first.by_entity_type.values()) == 5
        async with session_factory() as session:
            first_link_rows = (
                await session.execute(
                    sa.select(
                        feature_artifacts.c.artifact_type,
                        feature_artifacts.c.artifact_id,
                    ).where(feature_artifacts.c.feature_id == feature_id)
                )
            ).all()
        assert len(first_link_rows) == 5
        assert {(row.artifact_type, row.artifact_id) for row in first_link_rows} == expected_links
        assert await persist_backfill_metrics(session_factory, first) is True
        post_run_metrics = await MetricsCollector(
            engine=metrics_engine, session_factory=session_factory
        ).collect_db_stats()
        assert post_run_metrics["embedding_backlog"]["worker_last_24h"]["attempted"] >= 5
        assert post_run_metrics["embedding_backlog"]["worker_last_24h"]["stored"] >= 5

        enriched = {
            entity_type: await repo.get_by_id(created[entity_type].id)
            for entity_type, repo in repos.items()
        }
        assert all(
            entity is not None and entity.embedding is not None for entity in enriched.values()
        )
        timestamps = {
            entity_type: entity.updated_at
            for entity_type, entity in enriched.items()
            if entity is not None
        }
        batch_calls = state["batch_calls"]

        second = await job.run(project_key=project_key, batch_size=2, limit=5)
        assert sum(item.attempted for item in second.by_entity_type.values()) == 0
        assert state["batch_calls"] == batch_calls
        for entity_type, repo in repos.items():
            unchanged = await repo.get_by_id(created[entity_type].id)
            assert unchanged is not None
            assert unchanged.updated_at == timestamps[entity_type]

        async with session_factory() as session:
            second_link_rows = (
                await session.execute(
                    sa.select(
                        feature_artifacts.c.artifact_type,
                        feature_artifacts.c.artifact_id,
                    ).where(feature_artifacts.c.feature_id == feature_id)
                )
            ).all()
        assert len(second_link_rows) == 5
        assert {(row.artifact_type, row.artifact_id) for row in second_link_rows} == expected_links

        vector_results = await repos["decision"].search_vector(VECTOR, project_key=project_key)
        assert created["decision"].id in [decision.id for decision, _score in vector_results]
    finally:
        await embedding_svc.close()


async def test_zero_norm_learning_is_repaired_and_leaves_no_dedup_backlog(
    session_factory: async_sessionmaker[AsyncSession],
    fake_embedding_endpoint: tuple[dict[str, Any], str],
) -> None:
    """A non-comparable persisted vector is recoverable by the shared backfill."""
    state, base_url = fake_embedding_endpoint
    project_key = f"integ-zero-norm-{uuid.uuid4().hex[:8]}"
    embedding_svc = GPUEmbeddingService(base_url=base_url, timeout=0.5, max_retries=0)
    repo = PgLearningRepo(session_factory)
    service = LearningService(repo, embedding_svc)

    try:
        created = await service.create(
            LearningCreate(
                topic="Zero norm recovery",
                insight="A non-comparable embedding must re-enter the bounded backlog.",
                project_key=project_key,
            )
        )
        async with session_factory() as session:
            await session.execute(
                learnings.update()
                .where(learnings.c.id == created.id)
                .values(embedding=[0.0] * 1536)
            )
            await session.commit()

        state["available"] = True
        job = EmbeddingBackfillJob(
            session_factory=session_factory,
            repos={"learning": repo},
            embedding_svc=embedding_svc,
        )
        report = await job.run(
            entity_types=["learning"], batch_size=1, limit=1, project_key=project_key
        )

        assert report.by_entity_type["learning"].pending == 1
        assert report.by_entity_type["learning"].stored == 1
        repaired = await repo.get_by_id(created.id)
        assert repaired is not None
        assert repaired.embedding is not None
        assert sum(float(value) * float(value) for value in repaired.embedding) > 1e-12

        from scripts.ticket_extract import _corpus_backlog_stmt

        async with session_factory() as session:
            backlog = (await session.execute(_corpus_backlog_stmt(project_key))).one()
        assert backlog.missing_learning is False
    finally:
        await embedding_svc.close()


async def test_compare_and_set_rejects_update_delete_and_already_enriched_races(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    project_key = f"integ-embed-cas-{uuid.uuid4().hex[:8]}"
    repo = PgDecisionRepo(session_factory)

    async def create_pending(title: str):
        return await repo.create(
            DecisionCreate(
                title=title,
                description="CAS integration",
                reasoning="Protect derived writes",
                project_key=project_key,
            ),
            embedding=None,
        )

    updated = await create_pending("Concurrent update")
    updated_snapshot = next(
        row
        for row in await repo.list_embedding_backlog(limit=10, project_key=project_key)
        if row["id"] == updated.id
    )
    await repo.update(updated.id, DecisionUpdate(title="Changed after scan"), embedding=None)
    assert (
        await repo.set_embedding_if_current(
            updated.id,
            VECTOR,
            expected_updated_at=updated_snapshot["updated_at"],
        )
        is None
    )

    deleted = await create_pending("Concurrent delete")
    deleted_snapshot = next(
        row
        for row in await repo.list_embedding_backlog(limit=10, project_key=project_key)
        if row["id"] == deleted.id
    )
    await repo.delete(deleted.id)
    assert (
        await repo.set_embedding_if_current(
            deleted.id,
            VECTOR,
            expected_updated_at=deleted_snapshot["updated_at"],
        )
        is None
    )

    enriched = await create_pending("Already enriched")
    enriched_snapshot = next(
        row
        for row in await repo.list_embedding_backlog(limit=10, project_key=project_key)
        if row["id"] == enriched.id
    )
    first_store = await repo.set_embedding_if_current(
        enriched.id,
        VECTOR,
        expected_updated_at=enriched_snapshot["updated_at"],
    )
    assert first_store is not None
    assert (
        await repo.set_embedding_if_current(
            enriched.id,
            VECTOR,
            expected_updated_at=enriched_snapshot["updated_at"],
        )
        is None
    )
