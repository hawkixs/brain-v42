"""Integration proof that supersession and promotion extract claims in one transaction.

`brain_supersede_decision` and `brain_promote_adr` create a NEW entity while also
mutating another row (the superseded decision, or the source learning plus the
`dream_promotions` audit). Their service/repository methods used to own private
transactions, so an automatic claim could never share the commit. These tests
pin that the new entity, its sibling writes and its automatic claim commit
together; that a failed authoritative write leaves nothing behind; and that a
failed automatic claim costs only the claim.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from fastmcp.exceptions import ToolError
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
    adrs,
    brain_entities,
    decisions,
    dream_promotions,
    knowledge_claims,
    learnings,
    project_contexts,
)
from brain_v42.facts.definitions_startup import register_fact_definitions
from brain_v42.facts.model import FactTarget, SourceIdentity
from brain_v42.facts.probes.alembic_head import AlembicHeadProbe
from brain_v42.facts.registry import FactRegistry
from brain_v42.mcp.tools import claim_writes
from brain_v42.mcp.tools.brain_tools import register_tools
from brain_v42.models.decision import DecisionCreate
from brain_v42.models.learning import LearningCreate
from brain_v42.repositories.pg_adr import PgADRRepo
from brain_v42.repositories.pg_decision import PgDecisionRepo
from brain_v42.repositories.pg_learning import PgLearningRepo
from brain_v42.repositories.pg_project_context import PgProjectContextRepo
from brain_v42.services.adr_service import ADRService
from brain_v42.services.decision_service import DecisionService
from brain_v42.services.learning_service import LearningService
from tests.integration.conftest import _get_integration_db_url_or_skip
from tests.integration.disposable_db import fresh_head_database

pytestmark = pytest.mark.integration

#: Selected prose that the closed rule table turns into one `alembic_head` claim.
_ASSERTION = "The production Alembic head is 058."


# ---------------------------------------------------------------------------
# This module owns its database: it COMMITS claim rows, `knowledge_claims` is
# append-only (migration 055 refuses every DELETE), and a row left in the
# directory-wide migration database would trip 055's downgrade fence in every
# later migration test. See the identical block in `test_claim_write_path.py`.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def migration_database_url() -> Iterator[str]:
    """A database for this file alone, thrown away with its un-deletable rows."""
    shared_url = _get_integration_db_url_or_skip()
    with fresh_head_database(shared_url, prefix="brain_claim_extraction") as disposable_url:
        yield disposable_url


@pytest_asyncio.fixture(scope="session")
async def engine(migration_database_url: str) -> AsyncIterator[AsyncEngine]:
    """Bind to this module's database rather than the directory's shared one."""
    disposable_engine = create_async_engine(migration_database_url, poolclass=NullPool, echo=False)
    try:
        yield disposable_engine
    finally:
        await disposable_engine.dispose()


class _EmbeddingService:
    """Return a valid deterministic vector so the pre- and post-commit paths are observable."""

    async def embed(self, text: str) -> list[float]:
        return [0.125] * _EMBEDDING_DIM


class _MCP:
    """Keep the test on the registered tool closures without a transport dependency."""

    def __init__(self) -> None:
        self.registered: dict[str, Any] = {}

    def tool(self, **kwargs: Any) -> Any:
        def decorator(function: Any) -> Any:
            self.registered[function.__name__] = function
            return function

        return decorator


def _registry() -> FactRegistry:
    """The real `alembic_head` descriptor, which the extractor allowlists; never measured."""
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
    registry.register(AlembicHeadProbe())
    registry.freeze()
    return registry


async def _seed_project(
    session_factory: async_sessionmaker[AsyncSession], project_key: str
) -> None:
    async with session_factory() as session, session.begin():
        await session.execute(
            sa.insert(project_contexts).values(
                project_key=project_key,
                name=project_key,
                description="claim extraction write path fixture",
            )
        )


async def _tools(session_factory: async_sessionmaker[AsyncSession]) -> dict[str, Any]:
    """Register the real supersede/promote closures over real services, extraction armed."""
    registry = _registry()
    await register_fact_definitions(registry, session_factory)
    project_guard = PgProjectContextRepo(session_factory)
    mcp = _MCP()
    register_tools(
        mcp,  # type: ignore[arg-type]
        decision_svc=DecisionService(
            PgDecisionRepo(session_factory),
            _EmbeddingService(),
            project_context_repo=project_guard,
        ),
        learning_svc=MagicMock(),
        snippet_svc=MagicMock(),
        runbook_svc=MagicMock(),
        adr_svc=ADRService(
            PgADRRepo(session_factory),
            embedding_svc=_EmbeddingService(),
            project_context_repo=project_guard,
        ),
        project_context_svc=MagicMock(),
        brain_svc=MagicMock(),
        fact_registry=registry,
        session_factory=session_factory,
        extraction_enabled=True,
    )
    return mcp.registered


async def _old_decision(
    session_factory: async_sessionmaker[AsyncSession], project_key: str
) -> UUID:
    """An existing decision in an already-seeded project."""
    service = DecisionService(
        PgDecisionRepo(session_factory),
        _EmbeddingService(),
        project_context_repo=PgProjectContextRepo(session_factory),
    )
    old = await service.create(
        DecisionCreate(
            title=f"Old decision {uuid4()}",
            description="Context: before\n\nDecision: the old way",
            reasoning="It was the old way.",
            project_key=project_key,
        )
    )
    return old.id


async def _source_learning(
    session_factory: async_sessionmaker[AsyncSession], project_key: str
) -> UUID:
    """An existing promotion source learning in an already-seeded project."""
    service = LearningService(
        PgLearningRepo(session_factory),
        embedding_svc=_EmbeddingService(),
        project_context_repo=PgProjectContextRepo(session_factory),
    )
    learning = await service.create(
        LearningCreate(
            topic=f"Promotion source {uuid4()}",
            insight="A mature learning that is graduated into an ADR.",
            project_key=project_key,
        )
    )
    return learning.id


async def _claims_of(
    session_factory: async_sessionmaker[AsyncSession], entity_id: UUID
) -> list[Any]:
    async with session_factory() as session:
        return list(
            (
                await session.execute(
                    sa.select(knowledge_claims)
                    .join(
                        brain_entities,
                        knowledge_claims.c.entity_ref_id == brain_entities.c.id,
                    )
                    .where(brain_entities.c.source_uuid == entity_id)
                )
            ).mappings()
        )


async def _count_project_claims(
    session_factory: async_sessionmaker[AsyncSession], project_key: str
) -> int:
    async with session_factory() as session:
        return int(
            await session.scalar(
                sa.select(sa.func.count())
                .select_from(knowledge_claims)
                .where(knowledge_claims.c.project_key == project_key)
            )
            or 0
        )


async def _decision_row(
    session_factory: async_sessionmaker[AsyncSession], decision_id: UUID
) -> Any:
    async with session_factory() as session:
        return (
            (await session.execute(sa.select(decisions).where(decisions.c.id == decision_id)))
            .mappings()
            .one_or_none()
        )


async def _decisions_in(
    session_factory: async_sessionmaker[AsyncSession], project_key: str
) -> list[Any]:
    async with session_factory() as session:
        return list(
            (
                await session.execute(
                    sa.select(decisions)
                    .where(decisions.c.project_key == project_key)
                    .order_by(decisions.c.created_at)
                )
            ).mappings()
        )


async def _adrs_in(
    session_factory: async_sessionmaker[AsyncSession], project_key: str
) -> list[Any]:
    async with session_factory() as session:
        return list(
            (await session.execute(sa.select(adrs).where(adrs.c.project_key == project_key)))
            .mappings()
            .all()
        )


def _fail_automatic_claim_insert(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the automatic insert fail with a real server error that poisons the transaction.

    A bare `RuntimeError` would not prove the savepoint: PostgreSQL aborts the
    whole transaction after a statement error, so a missing savepoint would make
    the entry's own COMMIT fail. `1/0` is that error.
    """

    async def poisoned_insert(session: AsyncSession, **kwargs: object) -> object:
        await session.execute(sa.text("SELECT 1/0"))
        raise AssertionError("unreachable: the statement above always fails")

    monkeypatch.setattr(claim_writes, "insert_claim", poisoned_insert)


async def test_supersede_decision_extracts_from_new_decision_atomically(
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Supersession, the new decision and its claim commit once; failures stay in their class."""
    tools = await _tools(session_factory)
    supersede = tools["brain_supersede_decision"]
    project_key = f"claim-supersede-{uuid4().hex[:12]}"
    await _seed_project(session_factory, project_key)
    old_id = await _old_decision(session_factory, project_key)

    def call(old: UUID, title: str) -> Any:
        return supersede(
            old_decision_id=str(old),
            title=title,
            context="The previous approach was replaced.",
            decision_made="Adopt the new approach.",
            reasoning=_ASSERTION,
            project_key=project_key,
        )

    # 1. Success: one COMMIT carries the new decision, the status flip and the claim.
    commits: list[int] = []

    def record_commit(connection: Any) -> None:
        commits.append(1)

    event.listen(engine.sync_engine, "commit", record_commit)
    try:
        await call(old_id, "Supersede success")
    finally:
        event.remove(engine.sync_engine, "commit", record_commit)

    assert len(commits) == 1
    new = next(
        row for row in await _decisions_in(session_factory, project_key) if row["id"] != old_id
    )
    old = await _decision_row(session_factory, old_id)
    assert old["status"] == "superseded" and old["superseded_by"] == new["id"]
    assert new["embedding"] is not None  # embedded BEFORE the transaction, as before
    new_claims = await _claims_of(session_factory, new["id"])
    assert [(c["provenance"], c["fact_name"], c["retired_at"]) for c in new_claims] == [
        ("extracted", "alembic_head", None)
    ]
    assert await _claims_of(session_factory, old_id) == []

    # 2. Authoritative failure: the status UPDATE fails -> no new decision, no claim.
    second_old = await _old_decision(session_factory, project_key)
    claims_before = await _count_project_claims(session_factory, project_key)
    decisions_before = len(await _decisions_in(session_factory, project_key))

    def break_status_update(
        conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool
    ) -> tuple[str, Any]:
        if statement.lstrip().upper().startswith("UPDATE DECISIONS SET"):
            statement = statement.replace("UPDATE decisions", "UPDATE decisions_missing", 1)
        return statement, parameters

    event.listen(engine.sync_engine, "before_cursor_execute", break_status_update, retval=True)
    try:
        with pytest.raises(Exception, match="decisions_missing"):
            await call(second_old, "Supersede authoritative failure")
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", break_status_update)

    assert len(await _decisions_in(session_factory, project_key)) == decisions_before
    assert (await _decision_row(session_factory, second_old))["status"] != "superseded"
    assert await _count_project_claims(session_factory, project_key) == claims_before

    # 3. Automatic failure: the claim savepoint rolls back, supersession stays complete.
    _fail_automatic_claim_insert(monkeypatch)
    await call(second_old, "Supersede automatic failure")

    after = [
        row
        for row in await _decisions_in(session_factory, project_key)
        if row["title"] == "Supersede automatic failure"
    ]
    assert len(after) == 1
    assert (await _decision_row(session_factory, second_old))["superseded_by"] == after[0]["id"]
    assert (await _decision_row(session_factory, second_old))["status"] == "superseded"
    assert await _claims_of(session_factory, after[0]["id"]) == []
    assert await _count_project_claims(session_factory, project_key) == claims_before


async def test_promote_adr_extracts_from_new_adr_with_promotion_audit_atomically(
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR, source stamp, audit row and claim share one transaction; failures stay in their class."""
    tools = await _tools(session_factory)
    promote = tools["brain_promote_adr"]
    project_key = f"claim-promote-{uuid4().hex[:12]}"
    await _seed_project(session_factory, project_key)
    source_id = await _source_learning(session_factory, project_key)

    def call(source: UUID, title: str) -> Any:
        return promote(
            title=title,
            context="Forces at play.",
            decision="Graduate the learning.",
            consequences=_ASSERTION,
            project_key=project_key,
            source_learning_id=str(source),
        )

    # 1. Success: one COMMIT carries the ADR, the stamp, the audit row and the claim,
    #    and it precedes the post-commit enrichment (which commits the embedding itself).
    events: list[str] = []
    original_enrich = ADRService.enrich_created

    async def traced_enrich(self: ADRService, *args: Any, **kwargs: Any) -> Any:
        events.append("enrich")
        return await original_enrich(self, *args, **kwargs)

    def record_commit(connection: Any) -> None:
        events.append("commit")

    monkeypatch.setattr(ADRService, "enrich_created", traced_enrich)
    event.listen(engine.sync_engine, "commit", record_commit)
    try:
        confirmation = await call(source_id, "Promotion success")
    finally:
        event.remove(engine.sync_engine, "commit", record_commit)
        monkeypatch.setattr(ADRService, "enrich_created", original_enrich)

    assert "accepted" in confirmation
    assert events == ["commit", "enrich", "commit"]
    [adr] = await _adrs_in(session_factory, project_key)
    assert adr["status"] == "accepted" and adr["decided_at"] is not None
    assert adr["embedding"] is not None  # enriched after the commit, as before
    async with session_factory() as session:
        stamp = await session.scalar(
            sa.select(learnings.c.metadata).where(learnings.c.id == source_id)
        )
        audit = (
            (
                await session.execute(
                    sa.select(dream_promotions).where(
                        dream_promotions.c.source_learning_id == source_id
                    )
                )
            )
            .mappings()
            .one()
        )
    assert stamp["target_entity_id"] == str(adr["id"])
    assert audit["target_adr_id"] == adr["id"]
    assert [
        (c["provenance"], c["fact_name"]) for c in await _claims_of(session_factory, adr["id"])
    ] == [("extracted", "alembic_head")]

    # 2. Authoritative failure: a duplicate promotion fails at the audit insert, after
    #    the ADR insert and the stamp -> neither survives, and no claim is written.
    claims_before = await _count_project_claims(session_factory, project_key)
    with pytest.raises(ToolError, match="already materialized"):
        await call(source_id, "Promotion duplicate")
    assert len(await _adrs_in(session_factory, project_key)) == 1
    assert await _count_project_claims(session_factory, project_key) == claims_before
    async with session_factory() as session:
        assert (
            await session.scalar(
                sa.select(sa.func.count())
                .select_from(dream_promotions)
                .where(dream_promotions.c.source_learning_id == source_id)
            )
            == 1
        )

    # 3. Automatic failure: the claim savepoint rolls back, the promotion stays complete.
    second_source = await _source_learning(session_factory, project_key)
    _fail_automatic_claim_insert(monkeypatch)
    await call(second_source, "Promotion automatic failure")

    promoted = [
        row
        for row in await _adrs_in(session_factory, project_key)
        if row["title"] == "Promotion automatic failure"
    ]
    assert len(promoted) == 1 and promoted[0]["status"] == "accepted"
    async with session_factory() as session:
        assert (
            await session.scalar(
                sa.select(dream_promotions.c.target_adr_id).where(
                    dream_promotions.c.source_learning_id == second_source
                )
            )
            == promoted[0]["id"]
        )
    assert await _claims_of(session_factory, promoted[0]["id"]) == []
    assert await _count_project_claims(session_factory, project_key) == claims_before
