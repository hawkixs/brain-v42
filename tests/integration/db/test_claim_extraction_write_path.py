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

from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import dataclass
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
    runbooks,
    snippets,
)
from brain_v42.facts.claim_extractor import ClaimCandidate
from brain_v42.facts.definitions_startup import register_fact_definitions
from brain_v42.facts.model import FactTarget, SourceIdentity
from brain_v42.facts.probes.alembic_head import AlembicHeadProbe
from brain_v42.facts.probes.alembic_head_shipped import AlembicHeadShippedProbe
from brain_v42.facts.registry import FactRegistry
from brain_v42.mcp.tools import claim_writes
from brain_v42.mcp.tools.brain_tools import register_tools
from brain_v42.mcp.tools.crud_tools import register_crud_tools
from brain_v42.models.decision import DecisionCreate
from brain_v42.models.learning import LearningCreate
from brain_v42.repositories.pg_adr import PgADRRepo
from brain_v42.repositories.pg_decision import PgDecisionRepo
from brain_v42.repositories.pg_learning import PgLearningRepo
from brain_v42.repositories.pg_project_context import PgProjectContextRepo
from brain_v42.repositories.pg_runbook import PgRunbookRepo
from brain_v42.repositories.pg_snippet import PgSnippetRepo
from brain_v42.services import claim_extraction_counters as counters
from brain_v42.services.adr_service import ADRService
from brain_v42.services.decision_service import DecisionService
from brain_v42.services.learning_service import LearningService
from brain_v42.services.runbook_service import RunbookService
from brain_v42.services.snippet_service import SnippetService
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
    """Two allowlisted descriptors, one of them disabled; `live_release_sha` is left unregistered.

    Nothing is ever measured, so the sources are never opened.
    """
    identity = SourceIdentity(
        system_identifier="1", database="brain_test", server_addr="127.0.0.1", server_port=5432
    )
    registry = FactRegistry(
        sources={
            FactTarget.PRODUCTION: lambda: None,  # type: ignore[dict-item,return-value]
            FactTarget.LIVE_RELEASE: lambda: None,  # type: ignore[dict-item,return-value]
        },
        expected={FactTarget.PRODUCTION: identity, FactTarget.LIVE_RELEASE: identity},
    )
    registry.register(AlembicHeadProbe())
    registry.register(AlembicHeadShippedProbe())
    registry.freeze()
    registry.disable("alembic_head_shipped", "definition_drift")
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


async def _tools(
    session_factory: async_sessionmaker[AsyncSession], *, guarded: bool = True
) -> dict[str, Any]:
    """Register the real tool closures over real services, extraction armed.

    Production wires the unknown-project guard, which already refuses a write
    with no project key. `guarded=False` removes it so the extractor's own
    `no_project_key` skip -- the second line of defence -- can be observed.
    """
    registry = _registry()
    await register_fact_definitions(registry, session_factory)
    project_guard = PgProjectContextRepo(session_factory) if guarded else None
    mcp = _MCP()
    register_tools(
        mcp,  # type: ignore[arg-type]
        decision_svc=DecisionService(
            PgDecisionRepo(session_factory),
            _EmbeddingService(),
            project_context_repo=project_guard,
        ),
        learning_svc=LearningService(
            PgLearningRepo(session_factory),
            embedding_svc=_EmbeddingService(),
            project_context_repo=project_guard,
        ),
        snippet_svc=SnippetService(
            PgSnippetRepo(session_factory),
            _EmbeddingService(),
            project_context_repo=project_guard,
        ),
        runbook_svc=RunbookService(
            PgRunbookRepo(session_factory),
            _EmbeddingService(),
            project_context_repo=project_guard,
        ),
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
    service = DecisionService(PgDecisionRepo(session_factory), _EmbeddingService())
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
    service = LearningService(PgLearningRepo(session_factory), embedding_svc=_EmbeddingService())
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


# ---------------------------------------------------------------------------
# Seven writers, one accounting contract
# ---------------------------------------------------------------------------

#: One paragraph of three plain assertions: a registered fact, a disabled fact and
#: a fact the test registry does not register. The 40-character SHA is a literal.
_THREE_FACTS = (
    "The production Alembic head is 058. "
    "The shipped Alembic head is 058. "
    f"The live release is {'a' * 40}."
)

type _Delta = dict[tuple[str, str, str], int]


@dataclass(frozen=True)
class _Writer:
    """How to drive one of the seven tools and where its NEW entity lands."""

    entity_type: str
    call: Callable[..., Awaitable[Any]]
    table: sa.Table
    title_column: str
    unscoped: bool = True


async def _write_learning(
    tools: dict[str, Any], sf: Any, pk: str | None, title: str, prose: str
) -> Any:
    return await tools["brain_learn"](topic=title, insight=prose, project_key=pk)


async def _write_decision(
    tools: dict[str, Any], sf: Any, pk: str | None, title: str, prose: str
) -> Any:
    return await tools["brain_log_decision"](
        title=title, context="c", decision_made="d", reasoning=prose, project_key=pk
    )


async def _write_adr(tools: dict[str, Any], sf: Any, pk: str | None, title: str, prose: str) -> Any:
    return await tools["brain_propose_adr"](
        title=title, context=prose, decision="d", consequences="c", project_key=pk
    )


async def _write_snippet(
    tools: dict[str, Any], sf: Any, pk: str | None, title: str, prose: str
) -> Any:
    return await tools["brain_save_snippet"](
        title=title, intention=prose, code="pass", language="python", project_key=pk
    )


async def _write_runbook(
    tools: dict[str, Any], sf: Any, pk: str | None, title: str, prose: str
) -> Any:
    return await tools["brain_create_runbook"](
        title=title,
        description=prose,
        project_key=pk,
        trigger="t",
        steps=[{"title": "s"}],
    )


async def _write_supersede(
    tools: dict[str, Any], sf: Any, pk: str | None, title: str, prose: str
) -> Any:
    old = await _old_decision(sf, pk)
    return await tools["brain_supersede_decision"](
        old_decision_id=str(old),
        title=title,
        context="c",
        decision_made="d",
        reasoning=prose,
        project_key=pk,
    )


async def _write_promote(
    tools: dict[str, Any], sf: Any, pk: str | None, title: str, prose: str
) -> Any:
    source = await _source_learning(sf, pk)
    return await tools["brain_promote_adr"](
        title=title,
        context="c",
        decision="d",
        consequences=prose,
        project_key=pk,
        source_learning_id=str(source),
    )


_WRITERS = {
    "learning": _Writer("learning", _write_learning, learnings, "topic"),
    "decision": _Writer("decision", _write_decision, decisions, "title"),
    "adr": _Writer("adr", _write_adr, adrs, "title", unscoped=False),
    "snippet": _Writer("snippet", _write_snippet, snippets, "title"),
    "runbook": _Writer("runbook", _write_runbook, runbooks, "title", unscoped=False),
    "supersede": _Writer("decision", _write_supersede, decisions, "title"),
    "promote": _Writer("adr", _write_promote, adrs, "title", unscoped=False),
}


async def _new_entity_id(sf: async_sessionmaker[AsyncSession], writer: _Writer, title: str) -> UUID:
    async with sf() as session:
        entity_id = await session.scalar(
            sa.select(writer.table.c.id).where(writer.table.c[writer.title_column] == title)
        )
    assert isinstance(entity_id, UUID), f"{writer.entity_type} {title!r} was not committed"
    return entity_id


def _delta(before: counters.ClaimExtractionSnapshot) -> _Delta:
    """Every counter that moved since `before`, as {(kind, entity, reason): increase}."""
    after = counters.snapshot()
    moved: _Delta = {}
    for kind, now, then in (
        ("skipped", after.skipped, before.skipped),
        ("failed", after.failed, before.failed),
    ):
        for (entity, reason), count in now.items():
            if count != then[(entity, reason)]:
                moved[(kind, entity, reason)] = count - then[(entity, reason)]
    return moved


@pytest.mark.parametrize("name", list(_WRITERS))
async def test_seven_knowledge_writers_extract_without_claims_argument(
    name: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Each writer emits one claim per eligible fact, from the NEW entity, and accounts for the rest."""
    writer = _WRITERS[name]
    tools = await _tools(session_factory)
    project_key = f"claim-seven-{uuid4().hex[:12]}"
    await _seed_project(session_factory, project_key)

    before = counters.snapshot()
    title = f"Seven {name} {uuid4()}"
    await writer.call(tools, session_factory, project_key, title, _THREE_FACTS)

    entity_id = await _new_entity_id(session_factory, writer, title)
    claims = await _claims_of(session_factory, entity_id)
    assert [(c["provenance"], c["fact_name"], c["declared_by"]) for c in claims] == [
        ("extracted", "alembic_head", "server:claim-extractor:v1")
    ]
    # Only the new entity owns a claim: neither the superseded decision nor the source learning.
    assert await _count_project_claims(session_factory, project_key) == 1
    assert _delta(before) == {
        ("skipped", writer.entity_type, "disabled_fact"): 1,
        ("skipped", writer.entity_type, "unregistered_fact"): 1,
    }

    if not writer.unscoped:
        return
    before = counters.snapshot()
    title = f"Seven unscoped {name} {uuid4()}"
    await writer.call(
        await _tools(session_factory, guarded=False), session_factory, None, title, _THREE_FACTS
    )
    assert (
        await _claims_of(session_factory, await _new_entity_id(session_factory, writer, title))
        == []
    )
    assert _delta(before) == {("skipped", writer.entity_type, "no_project_key"): 1}


async def test_full_claim_quota_skips_extraction_with_a_bounded_reason(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Ten explicit claims fill the entry cap, so the automatic candidate is skipped and counted."""
    tools = await _tools(session_factory)
    project_key = f"claim-quota-{uuid4().hex[:12]}"
    await _seed_project(session_factory, project_key)
    explicit = [
        {
            "statement": f"Explicit declaration {index}.",
            "fact_name": "alembic_head",
            "expected": {"path": "/revision", "op": "eq", "value": f"{index:03d}"},
        }
        for index in range(10)
    ]

    before = counters.snapshot()
    title = f"Quota {uuid4()}"
    await tools["brain_learn"](
        topic=title, insight=_THREE_FACTS, project_key=project_key, claims=explicit
    )

    learning_id = await _new_entity_id(session_factory, _WRITERS["learning"], title)
    claims = await _claims_of(session_factory, learning_id)
    assert sorted(c["provenance"] for c in claims) == ["declared"] * 10
    assert _delta(before) == {("skipped", "learning", "quota_full"): 3}


@pytest.mark.parametrize("stage", ["parser_error", "resolver_error", "persistence_error"])
@pytest.mark.parametrize("name", list(_WRITERS))
async def test_extraction_failure_commits_entry_without_claim(
    name: str,
    stage: str,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A parser, resolver or SQL failure costs only the automatic claim, once, under a fixed label."""
    writer = _WRITERS[name]
    tools = await _tools(session_factory)
    project_key = f"claim-failure-{uuid4().hex[:12]}"
    await _seed_project(session_factory, project_key)

    def boom(*args: object, **kwargs: object) -> object:
        raise RuntimeError("forced automatic failure")

    if stage == "parser_error":
        monkeypatch.setattr(claim_writes, "extract_candidates", boom)
    elif stage == "resolver_error":
        monkeypatch.setattr(claim_writes, "resolve_claim", boom)
    else:
        _fail_automatic_claim_insert(monkeypatch)

    before = counters.snapshot()
    title = f"Failure {name} {stage} {uuid4()}"
    await writer.call(tools, session_factory, project_key, title, _THREE_FACTS)

    entity_id = await _new_entity_id(session_factory, writer, title)
    assert await _claims_of(session_factory, entity_id) == []
    assert await _count_project_claims(session_factory, project_key) == 0
    failures = {key: n for key, n in _delta(before).items() if key[0] == "failed"}
    assert failures == {("failed", writer.entity_type, stage): 1}


# ---------------------------------------------------------------------------
# Explicit precedence over an automatic claim
# ---------------------------------------------------------------------------


def _explicit_head(value: str = "058", statement: str = _ASSERTION) -> dict[str, object]:
    """An explicit `alembic_head` claim; the defaults are content-identical to the extracted one."""
    return {
        "statement": statement,
        "fact_name": "alembic_head",
        "expected": {"path": "/revision", "op": "eq", "value": value},
    }


async def _learning_with_extracted_claim(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[dict[str, Any], UUID, Any]:
    """A learning whose only active claim is the automatic one, plus the update tool."""
    tools = await _tools(session_factory)
    project_key = f"claim-same-key-{uuid4().hex[:12]}"
    await _seed_project(session_factory, project_key)
    title = f"Same key {uuid4()}"
    await tools["brain_learn"](topic=title, insight=_ASSERTION, project_key=project_key)
    learning_id = await _new_entity_id(session_factory, _WRITERS["learning"], title)
    mcp = _MCP()
    register_crud_tools(
        mcp,  # type: ignore[arg-type]
        decision_svc=MagicMock(),
        learning_svc=LearningService(
            PgLearningRepo(session_factory),
            embedding_svc=_EmbeddingService(),
            project_context_repo=PgProjectContextRepo(session_factory),
        ),
        snippet_svc=MagicMock(),
        runbook_svc=MagicMock(),
        adr_svc=MagicMock(),
        session_factory=session_factory,
        fact_registry=_registry(),
    )
    return tools, learning_id, mcp.registered["brain_update"]


async def _topic(session_factory: async_sessionmaker[AsyncSession], learning_id: UUID) -> str:
    async with session_factory() as session:
        return str(
            await session.scalar(sa.select(learnings.c.topic).where(learnings.c.id == learning_id))
        )


@pytest.mark.parametrize("supply_predecessor", [False, True])
async def test_explicit_same_key_retires_extracted_and_inserts_declared_with_replaces_id(
    supply_predecessor: bool, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """An identical explicit claim replaces the automatic occurrence; the explicit one wins."""
    _tools_, learning_id, update = await _learning_with_extracted_claim(session_factory)
    [extracted] = await _claims_of(session_factory, learning_id)
    assert extracted["provenance"] == "extracted"
    explicit = _explicit_head()
    if supply_predecessor:
        explicit["replaces"] = str(extracted["id"])

    result = await update(
        entity_type="learning",
        entity_id=str(learning_id),
        fields={"topic": "Renamed with an explicit claim"},
        claims=[explicit],
        expected_active_claim_ids=[str(extracted["id"])],
    )

    assert "kept:0" in result and "created:1" in result and "retired:1" in result
    old, new = sorted(await _claims_of(session_factory, learning_id), key=lambda c: c["seq"])
    assert (old["id"], old["provenance"]) == (extracted["id"], "extracted")
    assert old["retired_at"] is not None
    assert new["retired_at"] is None
    assert (new["provenance"], new["claim_key"], new["replaces_id"]) == (
        "declared",
        old["claim_key"],
        old["id"],
    )


async def test_explicit_same_key_wrong_replaces_fails_without_mutation(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A conflicting predecessor is refused before anything is retired, inserted or edited."""
    _tools_, learning_id, update = await _learning_with_extracted_claim(session_factory)
    topic_before = await _topic(session_factory, learning_id)
    [extracted] = await _claims_of(session_factory, learning_id)
    explicit = _explicit_head()
    explicit["replaces"] = str(uuid4())

    with pytest.raises(ToolError, match="replacement_required"):
        await update(
            entity_type="learning",
            entity_id=str(learning_id),
            fields={"topic": "Must not be applied"},
            claims=[explicit],
            expected_active_claim_ids=[str(extracted["id"])],
        )

    assert await _topic(session_factory, learning_id) == topic_before
    assert await _claims_of(session_factory, learning_id) == [extracted]


async def test_explicit_claim_blocks_automatic_candidate_of_the_same_fact(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An explicit claim for a fact, new or already active, keeps the automatic one out."""
    tools = await _tools(session_factory)
    registry = _registry()
    project_key = f"claim-precedence-{uuid4().hex[:12]}"
    await _seed_project(session_factory, project_key)

    # (a) The same create carries an explicit claim for the fact its prose would extract:
    #     a DIFFERENT value, so nothing but precedence keeps the second claim out.
    before = counters.snapshot()
    title = f"Precedence create {uuid4()}"
    await tools["brain_learn"](
        topic=title,
        insight=_ASSERTION,
        project_key=project_key,
        claims=[_explicit_head(value="061", statement="Declared head.")],
    )
    claims = await _claims_of(
        session_factory, await _new_entity_id(session_factory, _WRITERS["learning"], title)
    )
    assert [(c["provenance"], c["claim_key"] is not None) for c in claims] == [("declared", True)]
    assert _delta(before) == {("skipped", "learning", "explicit_precedence"): 1}

    # (b) The entry already holds an active declared claim of IDENTICAL content; a later
    #     automatic candidate must neither duplicate it, retire it, nor fail as a conflict.
    title = f"Precedence existing {uuid4()}"
    await tools["brain_learn"](
        topic=title,
        insight="Nothing to extract.",
        project_key=project_key,
        claims=[_explicit_head()],
    )
    learning_id = await _new_entity_id(session_factory, _WRITERS["learning"], title)
    [declared] = await _claims_of(session_factory, learning_id)
    before = counters.snapshot()
    async with session_factory() as session, session.begin():
        outcomes = await claim_writes.persist_extracted_claims(
            session,
            registry,
            entry_id=learning_id,
            entity_type="learning",
            project_key=project_key,
            candidates=(
                ClaimCandidate(
                    statement=_ASSERTION,
                    fact_name="alembic_head",
                    expected={"path": "/revision", "op": "eq", "value": "058"},
                ),
            ),
        )
    assert outcomes == []
    assert await _claims_of(session_factory, learning_id) == [declared]
    assert declared["retired_at"] is None
    assert _delta(before) == {("skipped", "learning", "explicit_precedence"): 1}
