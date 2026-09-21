"""Integration proof for atomic learning creation with declared claims."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from brain_v42.db.tables import (
    _EMBEDDING_DIM,
    brain_entities,
    knowledge_claims,
    learnings,
    project_contexts,
)
from brain_v42.facts.definitions_startup import register_fact_definitions
from brain_v42.facts.model import FactTarget, SourceIdentity
from brain_v42.facts.probe import SourceSession
from brain_v42.facts.registry import FactRegistry
from brain_v42.mcp.tools import claim_writes
from brain_v42.mcp.tools.claim_writes import persist_claims, resolve_claim_inputs
from brain_v42.models.learning import LearningCreate, LearningUpdate
from brain_v42.repositories.pg_learning import PgLearningRepo
from brain_v42.repositories.pg_project_context import PgProjectContextRepo
from brain_v42.services.learning_service import LearningService
from tests.integration.conftest import _get_integration_db_url_or_skip
from tests.integration.disposable_db import fresh_head_database

pytestmark = pytest.mark.integration


class _Probe:
    """Small registered fact whose descriptor is persisted with the disposable database."""

    name = "claim_write_lag"
    definition_version = 1
    target = FactTarget.PRODUCTION
    ttl = timedelta(seconds=15)
    timeout = timedelta(seconds=3)
    briefing = False
    policies: Mapping[str, int] = {"late_after_seconds": 300}
    value_schema = {"lag_seconds": "int"}

    async def measure(self, source: SourceSession) -> Mapping[str, object]:
        raise AssertionError("claim resolution must not measure a fact")


class _EmbeddingService:
    """Return a valid deterministic vector so the post-commit path is observable."""

    async def embed(self, text: str) -> list[float]:
        return [0.125] * _EMBEDDING_DIM


# ---------------------------------------------------------------------------
# This module owns its database, and that is not a preference.
#
# `tests/integration/db/` shares ONE disposable database for the whole session
# (`migration_database_url`, scope="session"). The tests below COMMIT claim rows,
# and `knowledge_claims` is append-only: migration 055 installs a BEFORE DELETE
# trigger that refuses every deletion. So these rows cannot be cleaned up in a
# `finally` — not because someone forgot, but because the schema forbids it.
#
# Left in the shared database they break every migration-downgrade test below
# 055, because those downgrades must transit through it and 055's fail-closed
# fence fires on the rows: measured 2026-09-21, 19 failures across the 051, 052
# and 054 fences, each reporting `cannot downgrade 055: knowledge_claims holds
# 2 row(s)`. The fence was right; the leak was here.
#
# Shadowing `migration_database_url` and `engine` IN THIS MODULE gives this file its
# own database. The scope stays `session` to match the fixtures they shadow — a
# module scope raises `ScopeMismatch`, because a session-scoped fixture consumes
# `engine`. Module-local definitions are only visible here, so "session" buys a
# second database, not a shared one. `session_factory` is function-scoped and
# follows `engine` without being named, and the parent's autouse environment
# rebind is function-scoped and follows too.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def migration_database_url() -> Iterator[str]:
    """A database for this file alone, thrown away with its un-deletable rows."""
    shared_url = _get_integration_db_url_or_skip()
    with fresh_head_database(shared_url, prefix="brain_claims") as disposable_url:
        yield disposable_url


@pytest_asyncio.fixture(scope="session")
async def engine(migration_database_url: str) -> AsyncIterator[AsyncEngine]:
    """Bind to this module's database rather than the directory's shared one."""
    disposable_engine = create_async_engine(migration_database_url, poolclass=NullPool, echo=False)
    try:
        yield disposable_engine
    finally:
        await disposable_engine.dispose()


def _registry() -> FactRegistry:
    """Build a closed descriptor registry; the source factory is never invoked for declarations."""
    registry = FactRegistry(
        sources={FactTarget.PRODUCTION: lambda: None},  # type: ignore[arg-type]
        expected={
            FactTarget.PRODUCTION: SourceIdentity(
                system_identifier="1",
                database="brain_test",
                server_addr="127.0.0.1",
                server_port=5432,
            )
        },
    )
    registry.register(_Probe())
    registry.freeze()
    return registry


def _claim(statement: str) -> dict[str, object]:
    """Create one independently specified declaration against the registered scalar schema."""
    return {
        "statement": statement,
        "fact_name": "claim_write_lag",
        "expected": {"path": "/lag_seconds", "op": "lte", "value": 300},
    }


async def _seed_project(
    session_factory: async_sessionmaker[AsyncSession], project_key: str
) -> None:
    """Insert the project guard prerequisite outside the operation under test."""
    async with session_factory() as session, session.begin():
        await session.execute(
            sa.insert(project_contexts).values(
                project_key=project_key,
                name=project_key,
                description="claim write path integration fixture",
            )
        )


def _service(session_factory: async_sessionmaker[AsyncSession]) -> LearningService:
    """Wire the real repository, project guard, and deterministic embedding enrichment."""
    return LearningService(
        PgLearningRepo(session_factory),
        embedding_svc=_EmbeddingService(),
        project_context_repo=PgProjectContextRepo(session_factory),
    )


async def test_learning_update_in_caller_transaction_rolls_back_with_the_caller(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A service update must not commit a caller-owned transaction on its behalf."""
    project_key = f"claim-update-rollback-{uuid4().hex[:12]}"
    await _seed_project(session_factory, project_key)
    original_insight = "The caller owns the transaction boundary."
    learning = await _service(session_factory).create(
        LearningCreate(
            topic=f"Caller rollback witness {uuid4()}",
            insight=original_insight,
            project_key=project_key,
        )
    )
    service = LearningService(PgLearningRepo(session_factory))

    async with session_factory() as session:
        await session.begin()
        updated = await service.update(
            learning.id,
            LearningUpdate(insight="This update must be rolled back."),
            session=session,
        )

        assert updated is not None
        assert updated.insight == "This update must be rolled back."
        await session.rollback()

    async with session_factory() as session:
        insight = await session.scalar(
            sa.select(learnings.c.insight).where(learnings.c.id == learning.id)
        )

    assert insight == original_insight


async def test_learning_and_two_claims_commit_together_then_enrich(
    engine: AsyncEngine, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """A separate claim commit would leave an entry behind when its declaration batch failed."""
    registry = _registry()
    await register_fact_definitions(registry, session_factory)
    project_key = f"claim-write-{uuid4().hex[:12]}"
    await _seed_project(session_factory, project_key)
    data = LearningCreate(
        topic="Atomic declared claims",
        insight="The learning and each occurrence commit in one PostgreSQL transaction.",
        project_key=project_key,
    )
    resolved = await resolve_claim_inputs(
        registry, [_claim("First declaration."), _claim("Second declaration.")]
    )
    service = _service(session_factory)
    commits = 0

    def record_commit(connection: Any) -> None:
        nonlocal commits
        commits += 1

    event.listen(engine.sync_engine, "commit", record_commit)
    try:
        async with session_factory() as session, session.begin():
            learning = await service.create(data, session=session)
            claim_ids = await persist_claims(
                session,
                entry_id=learning.id,
                entity_type="learning",
                project_key=project_key,
                resolved=resolved,
                declared_by="integration-test",
                declared_at=datetime.now(UTC),
            )
    finally:
        event.remove(engine.sync_engine, "commit", record_commit)

    assert commits == 1
    assert len(claim_ids) == 2
    async with session_factory() as session:
        anchor_id = await session.scalar(
            sa.select(brain_entities.c.id).where(brain_entities.c.source_uuid == learning.id)
        )
        assert isinstance(anchor_id, UUID)
        assert (
            await session.scalar(
                sa.select(sa.func.count())
                .select_from(knowledge_claims)
                .where(knowledge_claims.c.entity_ref_id == anchor_id)
            )
            == 2
        )

    enriched = await service.enrich_created(learning, data)
    assert enriched.embedding is not None


async def test_claim_persistence_failure_rolls_back_the_learning_too(
    monkeypatch: pytest.MonkeyPatch, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """A second occurrence failure must roll back the source row rather than strand an unclaimed entry."""
    registry = _registry()
    await register_fact_definitions(registry, session_factory)
    project_key = f"claim-write-rollback-{uuid4().hex[:12]}"
    await _seed_project(session_factory, project_key)
    topic = f"Rollback witness {uuid4()}"
    data = LearningCreate(
        topic=topic,
        insight="The transaction must not commit an entry after a claim failure.",
        project_key=project_key,
    )
    resolved = await resolve_claim_inputs(
        registry, [_claim("First declaration."), _claim("Second declaration.")]
    )
    original_insert_claim = claim_writes.insert_claim
    insert_count = 0

    async def fail_on_second_insert(*args: object, **kwargs: object) -> object:
        nonlocal insert_count
        insert_count += 1
        if insert_count == 2:
            raise RuntimeError("forced claim persistence failure")
        return await original_insert_claim(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(claim_writes, "insert_claim", fail_on_second_insert)
    with pytest.raises(RuntimeError, match="forced claim persistence failure"):
        async with session_factory() as session, session.begin():
            learning = await _service(session_factory).create(data, session=session)
            await persist_claims(
                session,
                entry_id=learning.id,
                entity_type="learning",
                project_key=project_key,
                resolved=resolved,
                declared_by="integration-test",
                declared_at=datetime.now(UTC),
            )

    async with session_factory() as session:
        assert (
            await session.scalar(
                sa.select(sa.func.count()).select_from(learnings).where(learnings.c.topic == topic)
            )
            == 0
        )
