"""Disposable-PostgreSQL proof of ownership fences at actual MCP write boundaries."""

import asyncio
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from fastmcp import Client, FastMCP
from fastmcp.server.middleware import Middleware

from brain_v42.db.tables import (
    brain_entities,
    decisions,
    indexed_plans,
    knowledge_claim_verdicts,
    knowledge_claims,
    learnings,
    project_contexts,
)
from brain_v42.facts.definitions_startup import register_fact_definitions
from brain_v42.facts.verification import ClaimVerificationService
from brain_v42.mcp import tool_families
from brain_v42.mcp.business_errors import surface_business_errors
from brain_v42.mcp.tool_families import FamilyAuthorizationMiddleware
from brain_v42.mcp.tools.brain_tools import register_tools
from brain_v42.mcp.tools.claim_tools import register_claim_tools
from brain_v42.mcp.tools.crud_tools import register_crud_tools
from brain_v42.mcp.tools.decay_tools import register_decay_tools
from brain_v42.models.decision import DecisionCreate
from brain_v42.models.focus_slot import SlotAnchor
from brain_v42.models.learning import LearningCreate
from brain_v42.provenance import checked_project
from brain_v42.repositories.checked_project import CheckedProjectMismatch
from brain_v42.repositories.pg_decision import PgDecisionRepo
from brain_v42.repositories.pg_focus_slot import PgFocusSlotRepo
from brain_v42.repositories.pg_graph_ledger import PgGraphLedgerRepo
from brain_v42.repositories.pg_learning import PgLearningRepo
from brain_v42.repositories.pg_scope_lookup import ENTITY_TABLES, PgScopeLookup
from brain_v42.services.decision_service import DecisionService
from brain_v42.services.learning_service import LearningService
from tests.integration.db import test_delivery_focus_slots as _slots
from tests.integration.db.test_claim_verification import _Probe, _registry
from tests.unit.mcp.test_tool_families import _scoped_access

pytestmark = pytest.mark.integration

# Slot rows and their append-only history must stay off the shared migration head.
session_factory = _slots.session_factory


@pytest.fixture
def factory(session_factory):
    return session_factory


@pytest_asyncio.fixture
async def entries(factory):
    projects = [f"scope-{uuid4().hex[:16]}" for _ in range(3)]
    async with factory() as session, session.begin():
        await session.execute(
            sa.insert(project_contexts),
            [{"project_key": key, "name": key, "description": "scope fixture"} for key in projects],
        )
    repo = PgLearningRepo(factory)
    rows = [
        await repo.create(
            LearningCreate(
                topic=f"scope fixture {uuid4()}",
                insight="original",
                project_key=projects[0],
            )
        )
        for _ in range(2)
    ]
    return projects, rows


async def move(factory, table, uid, project):
    async with factory() as session, session.begin():
        await session.execute(sa.update(table).where(table.c.id == uid).values(project_key=project))


class AfterCheck(Middleware):
    def __init__(self, callback):
        self.callback = callback

    async def on_call_tool(self, context, call_next):
        if self.callback is not None and context.message.name in {
            "brain_update",
            "brain_refresh_entity",
            "brain_claim_verify",
        }:
            callback, self.callback = self.callback, None
            await callback()
        return await call_next(context)


class Relations:
    def __init__(self, factory, before=None):
        self.ledger = PgGraphLedgerRepo(factory)
        self.before = before

    async def create_relation(self, source, target, relation):
        if self.before is not None:
            await self.before()
        await self.ledger.stage_uuid_relation(source, target, relation)
        return "created"


async def app(
    factory,
    monkeypatch,
    projects,
    *,
    mode="enforce",
    scoped=True,
    hook=None,
    graph=None,
    registry=None,
    verifier=None,
):
    monkeypatch.setattr(
        tool_families,
        "get_access_token",
        lambda: _scoped_access("write", projects=projects if scoped else None),
    )
    mcp = FastMCP(mask_error_details=True)
    mcp.add_middleware(
        FamilyAuthorizationMiddleware(scope_mode=mode, row_project_resolver=PgScopeLookup(factory))
    )
    mcp.add_middleware(AfterCheck(hook))
    registry = registry or _registry(_Probe(f"scope_{uuid4().hex}"))
    # Mirror server startup: claims reference the persisted immutable catalogue.
    assert await register_fact_definitions(registry, factory)
    register_crud_tools(
        mcp,
        decision_svc=MagicMock(),
        learning_svc=LearningService(PgLearningRepo(factory), graph=graph),
        snippet_svc=MagicMock(),
        runbook_svc=MagicMock(),
        adr_svc=MagicMock(),
        session_factory=factory,
        fact_registry=registry,
    )
    register_decay_tools(mcp, factory)
    if verifier is not None:
        register_claim_tools(mcp, verifier)
        monkeypatch.setattr(
            "brain_v42.mcp.tools.claim_tools.get_current_actor", lambda: "integration-test"
        )
    await surface_business_errors(mcp)
    return mcp


async def update(client, uid, fields, **extra):
    return await client.call_tool(
        "brain_update",
        {
            "entity_type": "learning",
            "entity_id": str(uid),
            "fields": fields,
            **extra,
        },
        raise_on_error=False,
    )


def claim_payload(fact_name):
    return {
        "statement": "The lag remains below five.",
        "fact_name": fact_name,
        "expected": {"path": "/lag", "op": "lte", "value": 5},
        "validity_seconds": 60,
    }


@pytest.mark.parametrize(
    "mode,scoped", [("enforce", True), ("observe", True), ("off", True), ("enforce", False)]
)
@pytest.mark.parametrize("variant", ["fields", "empty", "claims", "relations"])
async def test_move_after_resolution_fences_each_update_path(
    factory, entries, monkeypatch, mode, scoped, variant
):
    projects, (row, target) = entries
    fields = {} if variant in {"empty", "claims", "relations"} else {"insight": "changed"}
    probe = _Probe(f"scope_{uuid4().hex}")
    registry = _registry(probe)
    extra = {}
    if variant == "claims":
        extra = {"claims": [claim_payload(probe.name)], "expected_active_claim_ids": []}
    if variant == "relations":
        extra = {"related_to": [{"id": str(target.id), "type": "RELATED_TO"}]}

    async def concurrent_move():
        await move(factory, learnings, row.id, projects[1])

    mcp = await app(
        factory,
        monkeypatch,
        projects[:1],
        mode=mode,
        scoped=scoped,
        hook=concurrent_move,
        graph=Relations(factory),
        registry=registry,
    )
    async with Client(mcp) as client:
        result = await update(client, row.id, fields, **extra)
    denied = scoped and mode == "enforce"
    assert result.is_error == denied, result.content
    if denied:
        assert "scope_denied" in str(result.content)
    persisted = await PgLearningRepo(factory).get_by_id(row.id)
    assert persisted.project_key == projects[1]
    assert persisted.insight == ("original" if denied or not fields else "changed")
    async with factory() as session:
        claims = (
            (
                await session.execute(
                    sa.select(knowledge_claims.c.project_key)
                    .join(
                        brain_entities,
                        brain_entities.c.id == knowledge_claims.c.entity_ref_id,
                    )
                    .where(brain_entities.c.source_uuid == row.id)
                )
            )
            .scalars()
            .all()
        )
        relations = await session.scalar(
            sa.text(
                "SELECT count(*) FROM entity_relations r JOIN brain_entities e ON e.id = r.source_entity_id "
                "JOIN brain_entities t ON t.id = r.target_entity_id WHERE r.relation_type = 'RELATED_TO' "
                "AND (e.source_uuid = :id OR t.source_uuid = :id)"
            ),
            {"id": row.id},
        )
    assert claims == ([projects[1]] if variant == "claims" and not denied else [])
    assert relations == (1 if variant == "relations" and not denied else 0)


@pytest.mark.parametrize("race", [None, "before_move", "after_move"])
async def test_authorized_move_advances_owner_only_after_commit(
    factory, entries, monkeypatch, race
):
    projects, (row, target) = entries
    probe = _Probe(f"scope_{uuid4().hex}")

    async def before_move():
        await move(factory, learnings, row.id, projects[1])

    async def before_relation():
        await move(factory, learnings, row.id, projects[2])

    mcp = await app(
        factory,
        monkeypatch,
        projects[:2],
        hook=before_move if race == "before_move" else None,
        graph=Relations(factory, before_relation if race == "after_move" else None),
        registry=_registry(probe),
    )
    async with Client(mcp) as client:
        result = await update(
            client,
            row.id,
            {"project_key": projects[1], "insight": "committed-in-scope"},
            claims=[claim_payload(probe.name)],
            expected_active_claim_ids=[],
            related_to=[{"id": str(target.id), "type": "RELATED_TO"}],
        )
    assert result.is_error == (race is not None), result.content
    persisted = await PgLearningRepo(factory).get_by_id(row.id)
    if race == "before_move":
        assert persisted.insight == "original"
    else:
        assert persisted.insight == "committed-in-scope"
    if race is not None:
        assert "scope_denied" in str(result.content)
    async with factory() as session:
        relation_count = await session.scalar(
            sa.text(
                "SELECT count(*) FROM entity_relations r JOIN brain_entities e ON e.id = r.source_entity_id "
                "JOIN brain_entities t ON t.id = r.target_entity_id WHERE r.relation_type = 'RELATED_TO' "
                "AND (e.source_uuid = :id OR t.source_uuid = :id)"
            ),
            {"id": row.id},
        )
    assert relation_count == (1 if race is None else 0)
    async with factory() as session:
        claim_projects = (
            (
                await session.execute(
                    sa.select(knowledge_claims.c.project_key)
                    .join(
                        brain_entities,
                        brain_entities.c.id == knowledge_claims.c.entity_ref_id,
                    )
                    .where(brain_entities.c.source_uuid == row.id)
                )
            )
            .scalars()
            .all()
        )
    assert claim_projects == ([] if race == "before_move" else [projects[1]])


async def test_plan_refresh_rechecks_owning_project(factory, entries, monkeypatch):
    projects, _ = entries
    async with factory() as session, session.begin():
        uid = (
            await session.execute(
                sa.insert(indexed_plans)
                .values(
                    file_path=f"scope/{uuid4()}.md",
                    title="scope fixture",
                    plan_type="plan",
                    project_key=projects[0],
                    content_hash="a" * 64,
                    freshness_status="stale",
                )
                .returning(indexed_plans.c.id)
            )
        ).scalar_one()

    async def concurrent_move():
        await move(factory, indexed_plans, uid, projects[1])

    mcp = await app(factory, monkeypatch, projects[:1], hook=concurrent_move)
    async with Client(mcp) as client:
        result = await client.call_tool(
            "brain_refresh_entity",
            {"entity_type": "plan", "entity_id": str(uid)},
            raise_on_error=False,
        )
    assert result.is_error and "scope_denied" in str(result.content)
    async with factory() as session:
        assert (
            await session.scalar(
                sa.select(indexed_plans.c.freshness_status).where(indexed_plans.c.id == uid)
            )
            == "stale"
        )


async def test_supersede_lock_precedes_replacement_insert(factory, entries):
    projects, _ = entries
    repo = PgDecisionRepo(factory)
    row = await repo.create(
        DecisionCreate(
            title="old", description="original", reasoning="scope", project_key=projects[0]
        )
    )
    await move(factory, decisions, row.id, projects[1])
    with checked_project(projects[0]), pytest.raises(CheckedProjectMismatch):
        await repo.supersede(
            row.id,
            DecisionCreate(
                title="replacement",
                description="replacement",
                reasoning="scope",
                project_key=projects[0],
            ),
        )
    async with factory() as session:
        assert (
            await session.scalar(
                sa.select(sa.func.count())
                .select_from(decisions)
                .where(decisions.c.title == "replacement", decisions.c.project_key == projects[0])
            )
            == 0
        )


async def test_same_project_superseded_decision_keeps_domain_refusal(factory, entries):
    from brain_v42.repositories.pg_decision import DecisionNotSupersedableError

    projects, _ = entries
    repo = PgDecisionRepo(factory)
    data = DecisionCreate(
        title="old", description="original", reasoning="scope", project_key=projects[0]
    )
    row = await repo.create(data)
    await repo.supersede(row.id, data)
    with checked_project(projects[0]), pytest.raises(DecisionNotSupersedableError):
        await repo.supersede(row.id, data)


@pytest.mark.parametrize("extraction_enabled", [False, True])
async def test_supersede_between_two_authorized_projects_creates_relation(
    factory, entries, monkeypatch, extraction_enabled
):
    from unittest.mock import AsyncMock

    projects, _ = entries
    repo = PgDecisionRepo(factory)
    old = await repo.create(
        DecisionCreate(
            title="old", description="original", reasoning="scope", project_key=projects[0]
        )
    )
    graph = Relations(factory)
    graph.upsert_node = AsyncMock()
    mcp = FastMCP(mask_error_details=True)
    monkeypatch.setattr(
        tool_families, "get_access_token", lambda: _scoped_access("write", projects=projects[:2])
    )
    mcp.add_middleware(
        FamilyAuthorizationMiddleware(
            scope_mode="enforce", row_project_resolver=PgScopeLookup(factory)
        )
    )
    register_tools(
        mcp,
        decision_svc=DecisionService(
            repo, MagicMock(embed=AsyncMock(return_value=[0.1] * 1536)), graph=graph
        ),
        learning_svc=MagicMock(),
        snippet_svc=MagicMock(),
        runbook_svc=MagicMock(),
        adr_svc=MagicMock(),
        project_context_svc=MagicMock(),
        brain_svc=MagicMock(),
        session_factory=factory,
        fact_registry=_registry(_Probe(f"scope_{uuid4().hex}")),
        extraction_enabled=extraction_enabled,
    )
    await surface_business_errors(mcp)
    async with Client(mcp) as client:
        result = await client.call_tool(
            "brain_supersede_decision",
            {
                "old_decision_id": str(old.id),
                "title": "replacement",
                "context": "scope",
                "decision_made": "replace",
                "reasoning": "scope",
                "project_key": projects[1],
            },
            raise_on_error=False,
        )
    assert not result.is_error, result.content
    assert "Decision superseded" in str(result.content)
    persisted = await repo.get_by_id(old.id)
    assert persisted.status == "superseded"
    replacement = await repo.get_by_id(persisted.superseded_by)
    assert replacement.project_key == projects[1]
    async with factory() as session:
        assert (
            await session.scalar(
                sa.text(
                    "SELECT count(*) FROM entity_relations r "
                    "JOIN brain_entities s ON s.id = r.source_entity_id "
                    "JOIN brain_entities t ON t.id = r.target_entity_id "
                    "WHERE r.relation_type = 'SUPERSEDES' "
                    "AND s.source_uuid = :new_id AND t.source_uuid = :old_id"
                ),
                {"new_id": replacement.id, "old_id": old.id},
            )
            == 1
        )


async def new_claim(client, factory, row, fact_name):
    result = await update(
        client,
        row.id,
        {},
        claims=[
            {
                "statement": "The lag remains below five.",
                "fact_name": fact_name,
                "expected": {"path": "/lag", "op": "lte", "value": 5},
                "validity_seconds": 60,
            }
        ],
        expected_active_claim_ids=[],
    )
    assert not result.is_error, result.content
    async with factory() as session:
        return await session.scalar(
            sa.select(knowledge_claims.c.id)
            .join(brain_entities, brain_entities.c.id == knowledge_claims.c.entity_ref_id)
            .where(brain_entities.c.source_uuid == row.id)
        )


@pytest.mark.parametrize("when", ["before_resolution", "after_resolution"])
async def test_claim_source_move_refuses_without_verdict(factory, entries, monkeypatch, when):
    projects, (row, _) = entries
    probe = _Probe(f"scope_{uuid4().hex}")
    registry = _registry(probe)
    verifier = ClaimVerificationService(registry, factory)
    initial = await app(factory, monkeypatch, projects[:1], registry=registry, verifier=verifier)
    async with Client(initial) as client:
        claim_id = await new_claim(client, factory, row, probe.name)

    async def concurrent_move():
        await move(factory, learnings, row.id, projects[1])

    if when == "before_resolution":
        await concurrent_move()
    mcp = await app(
        factory,
        monkeypatch,
        projects[:1],
        registry=registry,
        verifier=verifier,
        hook=concurrent_move if when == "after_resolution" else None,
    )
    async with Client(mcp) as client:
        result = await client.call_tool(
            "brain_claim_verify",
            {"claim_id": str(claim_id), "idempotency_key": "race"},
            raise_on_error=False,
        )
    assert result.is_error and "scope_denied" in str(result.content)
    async with factory() as session:
        assert (
            await session.scalar(
                sa.select(sa.func.count())
                .select_from(knowledge_claim_verdicts)
                .where(knowledge_claim_verdicts.c.claim_id == claim_id)
            )
            == 0
        )


async def test_claim_verification_and_replacement_do_not_invert_locks(
    factory, entries, monkeypatch
):
    projects, (row, _) = entries
    probe = _Probe(f"scope_{uuid4().hex}")
    registry = _registry(probe)
    mcp = await app(
        factory,
        monkeypatch,
        projects[:1],
        registry=registry,
        verifier=ClaimVerificationService(registry, factory),
    )
    async with Client(mcp) as client:
        claim_id = await new_claim(client, factory, row, probe.name)
        results = await asyncio.wait_for(
            asyncio.gather(
                client.call_tool(
                    "brain_claim_verify",
                    {"claim_id": str(claim_id), "idempotency_key": "concurrent"},
                    raise_on_error=False,
                ),
                update(
                    client,
                    row.id,
                    {"insight": "changed"},
                    claims=[],
                    expected_active_claim_ids=[str(claim_id)],
                ),
            ),
            timeout=10,
        )
    assert not results[1].is_error, results[1].content
    assert "deadlock" not in str(results[0].content).lower()


async def test_later_relation_refusal_preserves_prior_authorized_transaction(
    factory, entries, monkeypatch
):
    projects, (row, target) = entries
    probe = _Probe(f"scope_{uuid4().hex}")

    async def after_entity_transaction():
        await move(factory, learnings, row.id, projects[1])

    mcp = await app(
        factory,
        monkeypatch,
        projects[:1],
        registry=_registry(probe),
        graph=Relations(factory, after_entity_transaction),
    )
    async with Client(mcp) as client:
        result = await update(
            client,
            row.id,
            {"insight": "committed-in-scope"},
            claims=[claim_payload(probe.name)],
            expected_active_claim_ids=[],
            related_to=[{"id": str(target.id), "type": "RELATED_TO"}],
        )
    assert result.is_error and "scope_denied" in str(result.content)
    persisted = await PgLearningRepo(factory).get_by_id(row.id)
    assert (persisted.project_key, persisted.insight) == (projects[1], "committed-in-scope")
    async with factory() as session:
        claim_project = await session.scalar(
            sa.select(knowledge_claims.c.project_key)
            .join(
                brain_entities,
                brain_entities.c.id == knowledge_claims.c.entity_ref_id,
            )
            .where(brain_entities.c.source_uuid == row.id)
        )
        relations = await session.scalar(
            sa.text(
                "SELECT count(*) FROM entity_relations r JOIN brain_entities e ON e.id = r.source_entity_id "
                "JOIN brain_entities t ON t.id = r.target_entity_id WHERE r.relation_type = 'RELATED_TO' "
                "AND (e.source_uuid = :id OR t.source_uuid = :id)"
            ),
            {"id": row.id},
        )
    assert claim_project == projects[0]
    assert relations == 0


@pytest.mark.parametrize(
    "entity_type", ["decision", "learning", "snippet", "runbook", "adr", "plan"]
)
@pytest.mark.parametrize("in_scope", [True, False])
async def test_refresh_every_concrete_table(factory, entries, monkeypatch, entity_type, in_scope):
    projects, _ = entries
    table = ENTITY_TABLES[entity_type]
    payloads = {
        "decision": {"title": "scope fixture", "description": "original", "reasoning": "scope"},
        "learning": {"topic": "scope fixture", "insight": "original"},
        "snippet": {
            "title": "scope fixture",
            "intention": "original",
            "code": "pass",
            "language": "python",
        },
        "runbook": {"title": "scope fixture", "description": "original", "trigger": "manual"},
        "adr": {
            "number": 1,
            "title": "scope fixture",
            "context": "original",
            "decision": "scope",
            "consequences": "scope",
        },
        "plan": {
            "file_path": f"scope/{uuid4()}.md",
            "title": "scope fixture",
            "plan_type": "plan",
            "content_hash": "b" * 64,
        },
    }
    async with factory() as session, session.begin():
        uid = (
            await session.execute(
                sa.insert(table)
                .values(
                    **payloads[entity_type],
                    project_key=projects[0 if in_scope else 1],
                    freshness_status="stale",
                )
                .returning(table.c.id)
            )
        ).scalar_one()
    mcp = await app(factory, monkeypatch, projects[:1])
    async with Client(mcp) as client:
        result = await client.call_tool(
            "brain_refresh_entity",
            {"entity_type": entity_type, "entity_id": str(uid)},
            raise_on_error=False,
        )
    assert result.is_error is not in_scope, result.content
    async with factory() as session:
        status = await session.scalar(sa.select(table.c.freshness_status).where(table.c.id == uid))
    assert status == ("fresh" if in_scope else "stale")


@pytest.mark.parametrize(
    "tool",
    [
        "brain_session_resume",
        "brain_session_capture",
        "brain_session_checkpoint",
        "brain_session_heartbeat",
        "brain_session_bind",
        "brain_session_end",
        "brain_session_relay",
        "brain_session_abandon",
        "brain_slot_close",
    ],
)
@pytest.mark.parametrize("in_scope", [True, False])
async def test_lifecycle_reads_actual_persisted_owner(
    factory, entries, monkeypatch, tool, in_scope
):
    from unittest.mock import AsyncMock

    from fastmcp.exceptions import AuthorizationError

    from brain_v42.db.tables import brain_sessions
    from tests.unit.mcp.test_tool_families import _scope_context

    projects, _ = entries
    slot = tool == "brain_slot_close"
    project = projects[0 if in_scope else 1]
    if slot:
        anchor = SlotAnchor(kind="ticket", ticket_id=await _slots.ticket(factory, project))
        opened = await PgFocusSlotRepo(factory).open(
            project, "scope fixture", "scope fixture", [anchor]
        )
        uid = opened.slot.id
    else:
        async with factory() as session, session.begin():
            uid = (
                await session.execute(
                    sa.insert(brain_sessions)
                    .values(
                        client_key=str(uuid4()),
                        started_focus_revision=0,
                        project_key=project,
                    )
                    .returning(brain_sessions.c.id)
                )
            ).scalar_one()
    monkeypatch.setattr(
        tool_families, "get_access_token", lambda: _scoped_access("write", projects=projects[:1])
    )
    middleware = FamilyAuthorizationMiddleware(
        scope_mode="enforce", row_project_resolver=PgScopeLookup(factory)
    )
    context = _scope_context(tool, {"slot_id" if slot else "session_id": str(uid)})
    handler = AsyncMock()
    if in_scope:
        await middleware.on_call_tool(context, handler)
        handler.assert_awaited_once()
    else:
        with pytest.raises(AuthorizationError, match="^scope_denied$"):
            await middleware.on_call_tool(context, handler)
        handler.assert_not_awaited()
