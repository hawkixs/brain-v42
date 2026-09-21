"""Integration proof for atomic learning creation with declared claims."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator, Mapping
from datetime import UTC, datetime, timedelta
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
from brain_v42.mcp.tools.crud_tools import register_crud_tools
from brain_v42.models.adr import ADRCreate
from brain_v42.models.learning import LearningCreate, LearningUpdate
from brain_v42.repositories.pg_adr import PgADRRepo
from brain_v42.repositories.pg_learning import PgLearningRepo
from brain_v42.repositories.pg_project_context import PgProjectContextRepo
from brain_v42.services.adr_service import ADRService
from brain_v42.services.embedding_text import adr_embedding_text
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


class _MCP:
    """Keep the integration test on the registered tool closure without a transport dependency."""

    def __init__(self) -> None:
        self.registered: dict[str, Any] = {}

    def tool(self, **kwargs: Any) -> Any:
        def decorator(function: Any) -> Any:
            self.registered[function.__name__] = function
            return function

        return decorator


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


def _adr_service(session_factory: async_sessionmaker[AsyncSession]) -> ADRService:
    """Wire the real ADR path so its post-commit enrichment is observable."""
    return ADRService(
        PgADRRepo(session_factory),
        embedding_svc=_EmbeddingService(),
        project_context_repo=PgProjectContextRepo(session_factory),
    )


def _brain_update_tool(
    service: LearningService,
    registry: FactRegistry,
    session_factory: async_sessionmaker[AsyncSession],
) -> Any:
    """Register only the generic tool while its real learning service owns the database write."""
    mcp = _MCP()
    register_crud_tools(
        mcp,
        decision_svc=MagicMock(),
        learning_svc=service,
        snippet_svc=MagicMock(),
        runbook_svc=MagicMock(),
        adr_svc=MagicMock(),
        session_factory=session_factory,
        fact_registry=registry,
    )
    return mcp.registered["brain_update"]


async def _learning_with_claims(
    session_factory: async_sessionmaker[AsyncSession],
    registry: FactRegistry,
    *statements: str,
) -> tuple[LearningService, Any, list[UUID]]:
    """Create a separate project entry with active declarations for one replacement scenario."""
    project_key = f"claim-update-{uuid4().hex[:12]}"
    await _seed_project(session_factory, project_key)
    service = _service(session_factory)
    data = LearningCreate(
        topic=f"Claim update witness {uuid4()}",
        insight="The initial field value must survive failed claim replacements.",
        project_key=project_key,
    )
    resolved = await resolve_claim_inputs(registry, [_claim(statement) for statement in statements])
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
    return service, learning, claim_ids


async def _claim_rows(
    session_factory: async_sessionmaker[AsyncSession], learning_id: UUID
) -> list[Any]:
    """Read every occurrence, including retired ones, to prove append-only replacement semantics."""
    async with session_factory() as session:
        return list(
            (
                await session.execute(
                    sa.select(knowledge_claims)
                    .join(
                        brain_entities,
                        knowledge_claims.c.entity_ref_id == brain_entities.c.id,
                    )
                    .where(brain_entities.c.source_uuid == learning_id)
                    .order_by(knowledge_claims.c.seq)
                )
            ).mappings()
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


async def test_adr_and_claim_commit_together_then_enrich(
    engine: AsyncEngine, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """An ADR claim write must share the entry transaction before derived work starts."""
    registry = _registry()
    await register_fact_definitions(registry, session_factory)
    project_key = f"claim-adr-{uuid4().hex[:12]}"
    await _seed_project(session_factory, project_key)
    data = ADRCreate(
        title=f"Atomic ADR claims {uuid4()}",
        context="The ADR and its declaration must commit together.",
        decision="Use the caller-owned transaction.",
        consequences="No ADR can survive a failed claim batch.",
        project_key=project_key,
    )
    resolved = await resolve_claim_inputs(registry, [_claim("ADR declaration.")])
    service = _adr_service(session_factory)
    commits = 0

    def record_commit(connection: Any) -> None:
        nonlocal commits
        commits += 1

    event.listen(engine.sync_engine, "commit", record_commit)
    try:
        async with session_factory() as session, session.begin():
            adr = await service.create(data, session=session)
            claim_ids = await persist_claims(
                session,
                entry_id=adr.id,
                entity_type="adr",
                project_key=project_key,
                resolved=resolved,
                declared_by="integration-test",
                declared_at=datetime.now(UTC),
            )
    finally:
        event.remove(engine.sync_engine, "commit", record_commit)

    assert commits == 1
    assert len(claim_ids) == 1
    async with session_factory() as session:
        anchor_id = await session.scalar(
            sa.select(brain_entities.c.id).where(brain_entities.c.source_uuid == adr.id)
        )
        assert isinstance(anchor_id, UUID)
        assert (
            await session.scalar(
                sa.select(sa.func.count())
                .select_from(knowledge_claims)
                .where(knowledge_claims.c.entity_ref_id == anchor_id)
            )
            == 1
        )
        assert (
            await session.scalar(
                sa.select(brain_entities.c.entity_type).where(brain_entities.c.id == anchor_id)
            )
            == "adr"
        )

    enriched = await service.enrich_created(
        adr,
        data,
        adr_embedding_text(data.title, data.context, data.decision),
    )
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


async def test_update_empty_claims_retires_every_active_occurrence_under_matching_cas(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An empty replacement is the shared guarded algorithm, not an unguarded exception."""
    registry = _registry()
    await register_fact_definitions(registry, session_factory)
    service, learning, claim_ids = await _learning_with_claims(
        session_factory, registry, "First active claim.", "Second active claim."
    )

    result = await _brain_update_tool(service, registry, session_factory)(
        entity_type="learning",
        entity_id=str(learning.id),
        fields={"insight": "The fields and retiring claims share one transaction."},
        claims=[],
        expected_active_claim_ids=[str(claim_ids[1]), str(claim_ids[0])],
    )

    assert "kept:0" in result
    assert "created:0" in result
    assert "retired:2" in result
    rows = await _claim_rows(session_factory, learning.id)
    assert {row["id"] for row in rows} == set(claim_ids)
    assert all(row["retired_at"] is not None for row in rows)


async def test_update_empty_claims_with_stale_cas_changes_nothing(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A stale destructive request leaves both the entry field and claim lifecycle untouched."""
    registry = _registry()
    await register_fact_definitions(registry, session_factory)
    service, learning, claim_ids = await _learning_with_claims(
        session_factory, registry, "Active claim."
    )

    with pytest.raises(ToolError, match="claims_conflict"):
        await _brain_update_tool(service, registry, session_factory)(
            entity_type="learning",
            entity_id=str(learning.id),
            fields={"insight": "This field update must be rolled back."},
            claims=[],
            expected_active_claim_ids=[str(uuid4())],
        )

    rows = await _claim_rows(session_factory, learning.id)
    assert rows[0]["id"] == claim_ids[0]
    assert rows[0]["retired_at"] is None
    async with session_factory() as session:
        assert await session.scalar(
            sa.select(learnings.c.insight).where(learnings.c.id == learning.id)
        ) == ("The initial field value must survive failed claim replacements.")


async def test_update_replacement_keeps_creates_and_retires_in_one_transaction(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A complete replacement preserves same keys while changing only the two set differences."""
    registry = _registry()
    await register_fact_definitions(registry, session_factory)
    service, learning, claim_ids = await _learning_with_claims(
        session_factory, registry, "Kept claim.", "Dropped claim."
    )

    result = await _brain_update_tool(service, registry, session_factory)(
        entity_type="learning",
        entity_id=str(learning.id),
        fields={"insight": "Replacement completes with the field update."},
        claims=[_claim("Kept claim."), _claim("Created claim.")],
        expected_active_claim_ids=[str(claim_ids[1]), str(claim_ids[0])],
    )

    assert "kept:1" in result
    assert "created:1" in result
    assert "retired:1" in result
    rows = await _claim_rows(session_factory, learning.id)
    assert len(rows) == 3
    kept = next(row for row in rows if row["id"] == claim_ids[0])
    dropped = next(row for row in rows if row["id"] == claim_ids[1])
    created = next(row for row in rows if row["id"] not in set(claim_ids))
    assert kept["retired_at"] is None
    assert dropped["retired_at"] is not None
    assert created["retired_at"] is None


async def test_update_reassertion_without_replaces_is_refused(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An old key cannot become active again unless the caller names its predecessor."""
    registry = _registry()
    await register_fact_definitions(registry, session_factory)
    service, learning, claim_ids = await _learning_with_claims(
        session_factory, registry, "Retired key."
    )
    tool = _brain_update_tool(service, registry, session_factory)
    await tool(
        entity_type="learning",
        entity_id=str(learning.id),
        fields={"insight": "Retire before reassertion."},
        claims=[],
        expected_active_claim_ids=[str(claim_ids[0])],
    )

    with pytest.raises(ToolError, match="replacement_required"):
        await tool(
            entity_type="learning",
            entity_id=str(learning.id),
            fields={"insight": "Missing predecessor is refused."},
            claims=[_claim("Retired key.")],
            expected_active_claim_ids=[],
        )


async def test_update_reassertion_with_wrong_predecessor_is_refused(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Naming a different retired occurrence cannot fork a claim's supersession chain."""
    registry = _registry()
    await register_fact_definitions(registry, session_factory)
    service, learning, claim_ids = await _learning_with_claims(
        session_factory, registry, "Retired key."
    )
    tool = _brain_update_tool(service, registry, session_factory)
    await tool(
        entity_type="learning",
        entity_id=str(learning.id),
        fields={"insight": "Retire before reassertion."},
        claims=[],
        expected_active_claim_ids=[str(claim_ids[0])],
    )

    wrong = _claim("Retired key.")
    wrong["replaces"] = str(uuid4())
    with pytest.raises(ToolError, match="replacement_required"):
        await tool(
            entity_type="learning",
            entity_id=str(learning.id),
            fields={"insight": "Wrong predecessor is refused."},
            claims=[wrong],
            expected_active_claim_ids=[],
        )


async def test_update_reassertion_with_latest_predecessor_succeeds(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A correctly named retired occurrence becomes the immutable predecessor of the new row."""
    registry = _registry()
    await register_fact_definitions(registry, session_factory)
    service, learning, claim_ids = await _learning_with_claims(
        session_factory, registry, "Retired key."
    )
    tool = _brain_update_tool(service, registry, session_factory)
    await tool(
        entity_type="learning",
        entity_id=str(learning.id),
        fields={"insight": "Retire before reassertion."},
        claims=[],
        expected_active_claim_ids=[str(claim_ids[0])],
    )

    replacement = _claim("Retired key.")
    replacement["replaces"] = str(claim_ids[0])
    result = await tool(
        entity_type="learning",
        entity_id=str(learning.id),
        fields={"insight": "Correct predecessor succeeds."},
        claims=[replacement],
        expected_active_claim_ids=[],
    )

    assert "created:1" in result
    rows = await _claim_rows(session_factory, learning.id)
    assert len(rows) == 2
    assert rows[1]["replaces_id"] == claim_ids[0]
    assert rows[1]["retired_at"] is None


async def test_update_replacement_of_an_active_same_key_keeps_that_occurrence(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Keeping an active key avoids an insert that would violate the active-key unique index."""
    registry = _registry()
    await register_fact_definitions(registry, session_factory)
    service, learning, claim_ids = await _learning_with_claims(
        session_factory, registry, "Same key."
    )

    result = await _brain_update_tool(service, registry, session_factory)(
        entity_type="learning",
        entity_id=str(learning.id),
        fields={"insight": "The existing occurrence remains active."},
        claims=[_claim("Same key.")],
        expected_active_claim_ids=[str(claim_ids[0])],
    )

    assert "kept:1" in result
    assert "created:0" in result
    assert "retired:0" in result
    rows = await _claim_rows(session_factory, learning.id)
    assert len(rows) == 1
    assert rows[0]["id"] == claim_ids[0]


async def test_update_claim_failure_rolls_back_the_field_update_too(
    monkeypatch: pytest.MonkeyPatch, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """A claim insert failure cannot commit the preceding service update from the same transaction."""
    registry = _registry()
    await register_fact_definitions(registry, session_factory)
    service, learning, _claim_ids = await _learning_with_claims(session_factory, registry)

    async def fail_insert(*args: object, **kwargs: object) -> object:
        raise RuntimeError("forced replacement insert failure")

    monkeypatch.setattr(claim_writes, "insert_claim", fail_insert)
    with pytest.raises(RuntimeError, match="forced replacement insert failure"):
        await _brain_update_tool(service, registry, session_factory)(
            entity_type="learning",
            entity_id=str(learning.id),
            fields={"insight": "This update must roll back with the failed claim."},
            claims=[_claim("New declaration after rollback.")],
            expected_active_claim_ids=[],
        )

    async with session_factory() as session:
        assert await session.scalar(
            sa.select(learnings.c.insight).where(learnings.c.id == learning.id)
        ) == ("The initial field value must survive failed claim replacements.")
