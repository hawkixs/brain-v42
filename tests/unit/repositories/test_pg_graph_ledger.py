"""Unit contracts for the PostgreSQL graph relation ledger."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest


def test_related_to_canonicalizes_endpoint_order() -> None:
    from brain_v42.repositories.pg_graph_ledger import canonicalize_relation_endpoints

    high = UUID("ffffffff-ffff-ffff-ffff-ffffffffffff")
    low = UUID("00000000-0000-0000-0000-000000000001")

    assert canonicalize_relation_endpoints(high, low, "RELATED_TO") == (low, high)


def test_directed_relation_preserves_endpoint_order() -> None:
    from brain_v42.repositories.pg_graph_ledger import canonicalize_relation_endpoints

    source = UUID("ffffffff-ffff-ffff-ffff-ffffffffffff")
    target = UUID("00000000-0000-0000-0000-000000000001")

    assert canonicalize_relation_endpoints(source, target, "IMPLEMENTS") == (source, target)


def test_relation_properties_are_allowlisted_and_bounded() -> None:
    from brain_v42.repositories.pg_graph_ledger import sanitize_relation_properties

    cleaned = sanitize_relation_properties(
        {
            "similarity": 0.87,
            "model": "embed-v1",
            "method": "cosine",
            "token": "must-not-land",
            "password": "must-not-land",
            "title": "private title",
            "content": "private body",
            "embedding": [0.1] * 100,
            "unknown": "ignored",
        }
    )

    assert cleaned == {"similarity": 0.87, "model": "embed-v1", "method": "cosine"}


@pytest.mark.parametrize(
    ("source_type", "target_type", "relation_type"),
    [
        ("decision", "project", "DEPENDS_ON"),
        ("learning", "domain", "RELATED_TO"),
        ("project", "decision", "IMPLEMENTS"),
        ("decision", "learning", "BELONGS_TO"),
        ("decision", "adr", "SUPERSEDES"),
    ],
)
def test_relation_shape_matrix_rejects_semantically_invalid_endpoints(
    source_type: str,
    target_type: str,
    relation_type: str,
) -> None:
    from brain_v42.repositories.pg_graph_ledger import validate_relation_shape

    with pytest.raises(ValueError, match="invalid relation shape"):
        validate_relation_shape(source_type, target_type, relation_type)


@pytest.mark.parametrize(
    ("source_type", "target_type", "relation_type"),
    [
        ("decision", "project", "BELONGS_TO"),
        ("learning", "domain", "BELONGS_TO_DOMAIN"),
        ("project", "project", "DEPENDS_ON"),
        ("decision", "learning", "IMPLEMENTS"),
        ("decision", "decision", "SUPERSEDES"),
    ],
)
def test_relation_shape_matrix_accepts_canonical_endpoint_pairs(
    source_type: str,
    target_type: str,
    relation_type: str,
) -> None:
    from brain_v42.repositories.pg_graph_ledger import validate_relation_shape

    validate_relation_shape(source_type, target_type, relation_type)


def _session_factory_with_rows(*rows: object) -> tuple[MagicMock, AsyncMock]:
    session = AsyncMock()
    results = []
    for row in rows:
        result = MagicMock()
        result.mappings.return_value.one_or_none.return_value = row
        results.append(result)
    if results:
        session.execute.side_effect = results
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=session)
    context.__aexit__ = AsyncMock(return_value=None)
    factory = MagicMock(return_value=context)
    return factory, session


@pytest.mark.asyncio
async def test_stage_uuid_relation_commits_fact_and_outbox_together() -> None:
    from brain_v42.repositories.pg_graph_ledger import PgGraphLedgerRepo

    relation_id = uuid4()
    event_id = uuid4()
    source_id = uuid4()
    target_id = uuid4()
    endpoints = {
        "source_entity_id": uuid4(),
        "source_entity_type": "learning",
        "source_entity_key": str(source_id),
        "target_entity_id": uuid4(),
        "target_entity_type": "decision",
        "target_entity_key": str(target_id),
    }
    relation = {"id": relation_id, "revision": 1}
    outbox = {"event_id": event_id}
    factory, session = _session_factory_with_rows(endpoints, relation, outbox)
    repo = PgGraphLedgerRepo(factory)

    event = await repo.stage_uuid_relation(
        source_id,
        target_id,
        "IMPLEMENTS",
        props={"similarity": 0.8, "token": "redacted"},
        origin="explicit",
        confidence=0.8,
    )

    assert event.event_id == event_id
    assert event.relation_id == relation_id
    assert event.operation == "upsert_relation"
    assert event.properties == {"similarity": 0.8}
    session.commit.assert_awaited_once()
    assert session.execute.await_count == 3
    endpoint_query = str(session.execute.await_args_list[0].args[0]).lower()
    assert "source.lifecycle = 'active'" in endpoint_query
    assert "target.lifecycle = 'active'" in endpoint_query
    assert "order by id" in endpoint_query
    assert "for update" in endpoint_query


@pytest.mark.asyncio
async def test_unknown_endpoint_rolls_back_without_outbox() -> None:
    from brain_v42.repositories.pg_graph_ledger import PgGraphLedgerRepo, UnknownGraphEndpoint

    factory, session = _session_factory_with_rows(None)
    repo = PgGraphLedgerRepo(factory)

    with pytest.raises(UnknownGraphEndpoint):
        await repo.stage_uuid_relation(uuid4(), uuid4(), "RELATED_TO")

    session.rollback.assert_awaited_once()
    session.commit.assert_not_awaited()
    assert session.execute.await_count == 1


@pytest.mark.asyncio
async def test_stage_uuid_relation_rejects_cross_project_scope_under_endpoint_locks() -> None:
    from brain_v42.repositories.pg_graph_ledger import PgGraphLedgerRepo

    endpoints = {
        "source_entity_id": uuid4(),
        "source_entity_type": "learning",
        "source_entity_key": str(uuid4()),
        "source_project_key": "brain-v42",
        "target_entity_id": uuid4(),
        "target_entity_type": "decision",
        "target_entity_key": str(uuid4()),
        "target_project_key": "another-project",
    }
    factory, session = _session_factory_with_rows(endpoints)
    repo = PgGraphLedgerRepo(factory)

    with pytest.raises(ValueError, match="authorized project"):
        await repo.stage_uuid_relation(
            uuid4(),
            uuid4(),
            "IMPLEMENTS",
            project_key="brain-v42",
        )

    endpoint_query = str(session.execute.await_args_list[0].args[0]).lower()
    assert "source.project_key as source_project_key" in " ".join(endpoint_query.split())
    assert "target.project_key as target_project_key" in " ".join(endpoint_query.split())
    assert "for update" in endpoint_query
    session.rollback.assert_awaited_once()
    session.commit.assert_not_awaited()
    assert session.execute.await_count == 1


@pytest.mark.asyncio
async def test_domain_membership_rejects_stale_project_scope_under_endpoint_locks() -> None:
    from brain_v42.repositories.pg_graph_ledger import PgGraphLedgerRepo

    endpoints = {
        "source_entity_id": uuid4(),
        "source_entity_type": "learning",
        "source_entity_key": str(uuid4()),
        "source_project_key": "moved-project",
        "target_entity_id": uuid4(),
        "target_entity_type": "domain",
        "target_entity_key": "infra",
    }
    factory, session = _session_factory_with_rows(endpoints)
    repo = PgGraphLedgerRepo(factory)

    with pytest.raises(ValueError, match="authorized project"):
        await repo.stage_domain_membership(
            uuid4(),
            "infra",
            project_key="brain-v42",
        )

    endpoint_query = " ".join(str(session.execute.await_args.args[0]).lower().split())
    assert "source.project_key as source_project_key" in endpoint_query
    assert "for update" in endpoint_query
    session.rollback.assert_awaited_once()
    session.commit.assert_not_awaited()
    assert session.execute.await_count == 1


@pytest.mark.asyncio
async def test_project_membership_requires_the_source_current_project_under_lock() -> None:
    from brain_v42.repositories.pg_graph_ledger import PgGraphLedgerRepo

    endpoints = {
        "source_entity_id": uuid4(),
        "source_entity_type": "learning",
        "source_entity_key": str(uuid4()),
        "source_project_key": "moved-project",
        "target_entity_id": uuid4(),
        "target_entity_type": "project",
        "target_entity_key": "brain-v42",
    }
    factory, session = _session_factory_with_rows(endpoints)
    repo = PgGraphLedgerRepo(factory)

    with pytest.raises(ValueError, match="authorized project"):
        await repo.stage_project_membership(uuid4(), "brain-v42")

    session.rollback.assert_awaited_once()
    session.commit.assert_not_awaited()
    assert session.execute.await_count == 1


@pytest.mark.asyncio
async def test_identical_stage_preserves_revision_provenance_and_outbox_identity() -> None:
    from brain_v42.repositories.pg_graph_ledger import PgGraphLedgerRepo

    source_id, target_id = uuid4(), uuid4()
    endpoints = {
        "source_entity_id": uuid4(),
        "source_entity_type": "learning",
        "source_entity_key": str(source_id),
        "target_entity_id": uuid4(),
        "target_entity_type": "decision",
        "target_entity_key": str(target_id),
    }
    factory, session = _session_factory_with_rows(
        endpoints,
        {"id": uuid4(), "revision": 3},
        {"event_id": uuid4()},
    )
    repo = PgGraphLedgerRepo(factory)

    await repo.stage_uuid_relation(
        source_id,
        target_id,
        "RELATED_TO",
        props={"similarity": 0.9},
        origin="auto_linker",
        confidence=0.9,
    )

    relation_sql = " ".join(str(session.execute.await_args_list[1].args[0]).lower().split())
    outbox_sql = " ".join(str(session.execute.await_args_list[2].args[0]).lower().split())
    assert "case when" in relation_sql
    assert "is distinct from" in relation_sql
    assert "origin = entity_relations.origin" in relation_sql
    assert "on conflict" in outbox_sql
    assert "operation = graph_outbox.operation" in outbox_sql


@pytest.mark.asyncio
async def test_mark_failed_stores_only_a_bounded_error_code() -> None:
    from brain_v42.repositories.pg_graph_ledger import (
        GraphOutboxEvent,
        PgGraphLedgerRepo,
        ProjectionClaim,
    )

    factory, session = _session_factory_with_results(_fencing_result())
    repo = PgGraphLedgerRepo(factory)
    raw_error = "neo4j://user:secret@host payload=private"
    claim = ProjectionClaim(
        event=GraphOutboxEvent(event_id=uuid4(), operation="upsert_entity"),
        owner_id="worker-safe",
        lease_generation=3,
        claim_version=2,
        leased_until=datetime.now(UTC) + timedelta(seconds=30),
    )

    await repo.mark_failed(claim, raw_error, max_attempts=10)

    _statement, params = session.execute.await_args.args
    assert params["error_code"] == "projection_failed"
    assert raw_error not in repr(params)
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_mark_delivered_supersedes_older_terminal_revisions() -> None:
    from brain_v42.repositories.pg_graph_ledger import (
        GraphOutboxEvent,
        PgGraphLedgerRepo,
        ProjectionClaim,
    )

    factory, session = _session_factory_with_results(_fencing_result(scalar=True))
    repo = PgGraphLedgerRepo(factory)
    event_id = uuid4()
    claim = ProjectionClaim(
        event=GraphOutboxEvent(event_id=event_id, operation="upsert_entity"),
        owner_id="worker-ack",
        lease_generation=4,
        claim_version=3,
        leased_until=datetime.now(UTC) + timedelta(seconds=30),
    )

    await repo.mark_delivered(claim)

    statement, params = session.execute.await_args.args
    sql = " ".join(str(statement).lower().split())
    assert params["event_id"] == event_id
    assert params["owner_id"] == "worker-ack"
    assert params["lease_generation"] == 4
    assert params["claim_version"] == 3
    assert "aggregate_revision" in sql
    assert "max_attempts" in sql
    assert "superseded" in sql
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_schema_readiness_uses_ledger_shape_so_future_heads_remain_ready() -> None:
    from brain_v42.repositories.pg_graph_ledger import PgGraphLedgerRepo

    result = MagicMock()
    result.scalar_one.return_value = True
    session = AsyncMock()
    session.execute.return_value = result
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=session)
    context.__aexit__ = AsyncMock(return_value=None)
    repo = PgGraphLedgerRepo(MagicMock(return_value=context))

    await repo.assert_schema_ready()

    statement = " ".join(str(session.execute.await_args.args[0]).lower().split())
    assert "alembic_version" not in statement
    for table_name in (
        "projects",
        "project_aliases",
        "brain_entities",
        "entity_relations",
        "graph_outbox",
        "graph_projection_leases",
    ):
        assert f"public.{table_name}" in statement


@pytest.mark.asyncio
async def test_schema_readiness_fails_closed() -> None:
    from brain_v42.repositories.pg_graph_ledger import PgGraphLedgerRepo

    result = MagicMock()
    result.scalar_one.return_value = False
    session = AsyncMock()
    session.execute.return_value = result
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=session)
    context.__aexit__ = AsyncMock(return_value=None)
    repo = PgGraphLedgerRepo(MagicMock(return_value=context))

    with pytest.raises(RuntimeError, match="migration 035"):
        await repo.assert_schema_ready()


@pytest.mark.asyncio
async def test_full_projection_requeue_resets_only_current_aggregate_revisions() -> None:
    from brain_v42.repositories.pg_graph_ledger import PgGraphLedgerRepo

    entity_result = MagicMock()
    entity_result.mappings.return_value.all.return_value = [{"id": 1}, {"id": 2}]
    relation_result = MagicMock()
    relation_result.mappings.return_value.all.return_value = [{"id": 3}]
    session = AsyncMock()
    session.execute.side_effect = [entity_result, relation_result]
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=session)
    context.__aexit__ = AsyncMock(return_value=None)
    repo = PgGraphLedgerRepo(MagicMock(return_value=context))

    report = await repo.requeue_full_projection()

    assert report.entity_events == 2
    assert report.relation_events == 1
    for call in session.execute.await_args_list:
        statement = " ".join(str(call.args[0]).lower().split())
        assert "on conflict" in statement
        assert "delivered_at" in statement
        assert "attempt_count" in statement
        assert "lease_generation" in statement
        assert "returning graph_outbox.id" in statement
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_projection_inventory_reports_rebuild_scope_without_writes() -> None:
    from brain_v42.repositories.pg_graph_ledger import PgGraphLedgerRepo

    result = MagicMock()
    result.mappings.return_value.one.return_value = {
        "entity_count": 12,
        "relation_count": 34,
        "pending_count": 5,
    }
    session = AsyncMock()
    session.execute.return_value = result
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=session)
    context.__aexit__ = AsyncMock(return_value=None)
    repo = PgGraphLedgerRepo(MagicMock(return_value=context))

    inventory = await repo.projection_inventory()

    assert inventory.entity_count == 12
    assert inventory.relation_count == 34
    assert inventory.pending_count == 5
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_claim_pending_uses_skip_locked() -> None:
    from brain_v42.repositories.pg_graph_ledger import PgGraphLedgerRepo, ProjectionLeadership

    result = MagicMock()
    result.mappings.return_value.all.return_value = []
    session = AsyncMock()
    session.execute.return_value = result
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=session)
    context.__aexit__ = AsyncMock(return_value=None)
    repo = PgGraphLedgerRepo(MagicMock(return_value=context))

    leadership = ProjectionLeadership(
        "worker-1",
        1,
        datetime.now(UTC) + timedelta(seconds=30),
        True,
    )
    events = await repo.claim_pending(leadership, limit=25, lease_seconds=30)

    assert events == []
    statement = str(session.execute.await_args.args[0]).upper()
    assert "SKIP LOCKED" in statement
    assert "FOR UPDATE" in statement
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_claim_pending_serializes_revisions_per_aggregate() -> None:
    """A later revision must never overtake an older in-flight revision."""
    from brain_v42.repositories.pg_graph_ledger import PgGraphLedgerRepo, ProjectionLeadership

    result = MagicMock()
    result.mappings.return_value.all.return_value = []
    session = AsyncMock()
    session.execute.return_value = result
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=session)
    context.__aexit__ = AsyncMock(return_value=None)
    repo = PgGraphLedgerRepo(MagicMock(return_value=context))

    leadership = ProjectionLeadership(
        "worker-ordered",
        1,
        datetime.now(UTC) + timedelta(seconds=30),
        True,
    )
    await repo.claim_pending(leadership, limit=25, lease_seconds=30)

    statement = " ".join(str(session.execute.await_args.args[0]).upper().split())
    assert "NOT EXISTS" in statement
    assert "EARLIER.AGGREGATE_REVISION < PENDING.AGGREGATE_REVISION" in statement
    assert "EARLIER.ENTITY_ID = PENDING.ENTITY_ID" in statement
    assert "EARLIER.RELATION_ID = PENDING.RELATION_ID" in statement
    assert statement.count("LAST_ERROR_CODE IS DISTINCT FROM 'MAX_ATTEMPTS'") == 3
    assert "EXHAUSTED.ATTEMPT_COUNT >= :MAX_ATTEMPTS" in statement
    assert "LAST_ERROR_CODE = 'MAX_ATTEMPTS'" in statement
    assert "ORDER BY PENDING.AVAILABLE_AT, PENDING.ID" in statement
    assert "ORDER BY LEASED.ID" in statement


@pytest.mark.asyncio
async def test_claim_pending_maps_entity_events_without_dropping_them() -> None:
    from brain_v42.repositories.pg_graph_ledger import PgGraphLedgerRepo, ProjectionLeadership

    event_id = uuid4()
    entity_id = uuid4()
    source_id = uuid4()
    row = {
        "event_id": event_id,
        "operation": "upsert_entity",
        "aggregate_revision": 2,
        "owner_id": "worker-entity",
        "lease_generation": 1,
        "claim_version": 1,
        "leased_until": datetime.now(UTC) + timedelta(seconds=30),
        "entity_id": entity_id,
        "entity_type": "learning",
        "entity_key": str(source_id),
        "source_uuid": source_id,
        "project_key": "brain-v42",
        "display_label": "Durable graph facts",
        "lifecycle": "active",
        "relation_id": None,
        "source_type": None,
        "source_key": None,
        "target_type": None,
        "target_key": None,
        "relation_type": None,
        "properties": None,
    }
    result = MagicMock()
    result.mappings.return_value.all.return_value = [row]
    session = AsyncMock()
    session.execute.return_value = result
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=session)
    context.__aexit__ = AsyncMock(return_value=None)
    repo = PgGraphLedgerRepo(MagicMock(return_value=context))

    leadership = ProjectionLeadership(
        "worker-entity",
        1,
        datetime.now(UTC) + timedelta(seconds=30),
        True,
    )
    claims = await repo.claim_pending(leadership, limit=10, lease_seconds=30)

    assert len(claims) == 1
    event = claims[0].event
    assert event.event_id == event_id
    assert event.operation == "upsert_entity"
    assert event.entity_id == entity_id
    assert event.entity_type == "learning"
    assert event.source_uuid == source_id
    statement = str(session.execute.await_args.args[0]).upper()
    assert "LEFT JOIN BRAIN_ENTITIES" in statement


@pytest.mark.asyncio
async def test_claim_pending_maps_current_relation_lifecycle() -> None:
    from brain_v42.repositories.pg_graph_ledger import PgGraphLedgerRepo, ProjectionLeadership

    row = {
        "event_id": uuid4(),
        "operation": "upsert_relation",
        "aggregate_revision": 3,
        "owner_id": "worker",
        "lease_generation": 1,
        "claim_version": 1,
        "leased_until": datetime.now(UTC) + timedelta(seconds=30),
        "entity_id": None,
        "entity_type": None,
        "entity_key": None,
        "source_uuid": None,
        "project_key": None,
        "display_label": None,
        "lifecycle": None,
        "relation_id": uuid4(),
        "source_type": "decision",
        "source_key": str(uuid4()),
        "target_type": "learning",
        "target_key": str(uuid4()),
        "relation_type": "RELATED_TO",
        "relation_lifecycle": "deleted",
        "properties": {},
    }
    result = MagicMock()
    result.mappings.return_value.all.return_value = [row]
    session = AsyncMock()
    session.execute.return_value = result
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=session)
    context.__aexit__ = AsyncMock(return_value=None)
    repo = PgGraphLedgerRepo(MagicMock(return_value=context))

    leadership = ProjectionLeadership(
        "worker",
        1,
        datetime.now(UTC) + timedelta(seconds=30),
        True,
    )
    event = (await repo.claim_pending(leadership, limit=1, lease_seconds=30))[0].event

    assert event.relation_lifecycle == "deleted"
    statement = str(session.execute.await_args.args[0]).upper()
    assert "RELATION.LIFECYCLE AS RELATION_LIFECYCLE" in statement


def _fencing_result(
    *,
    row: object | None = None,
    rows: list[object] | None = None,
    scalar: object | None = None,
) -> MagicMock:
    result = MagicMock()
    result.mappings.return_value.one_or_none.return_value = row
    result.mappings.return_value.all.return_value = rows or []
    result.scalar_one_or_none.return_value = scalar
    return result


def _session_factory_with_results(*results: MagicMock) -> tuple[MagicMock, AsyncMock]:
    session = AsyncMock()
    session.execute.side_effect = list(results)
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=session)
    context.__aexit__ = AsyncMock(return_value=None)
    return MagicMock(return_value=context), session


def test_projection_fencing_coordinates_are_explicit_values() -> None:
    from brain_v42.repositories.pg_graph_ledger import (
        GraphOutboxEvent,
        ProjectionClaim,
        ProjectionLeadership,
    )

    lease_until = datetime.now(UTC) + timedelta(seconds=30)
    event = GraphOutboxEvent(
        event_id=uuid4(),
        operation="upsert_entity",
        aggregate_revision=7,
    )
    leadership = ProjectionLeadership(
        owner_id="projector-a",
        generation=11,
        lease_until=lease_until,
        armed=False,
    )
    claim = ProjectionClaim(
        event=event,
        owner_id=leadership.owner_id,
        lease_generation=leadership.generation,
        claim_version=3,
        leased_until=lease_until,
    )

    assert leadership.owner_id == "projector-a"
    assert leadership.generation == 11
    assert leadership.armed is False
    assert claim.event.aggregate_revision == 7
    assert claim.owner_id == leadership.owner_id
    assert claim.lease_generation == leadership.generation
    assert claim.claim_version == 3


@pytest.mark.asyncio
async def test_acquire_leadership_returns_a_new_unarmed_generation() -> None:
    from brain_v42.repositories.pg_graph_ledger import (
        PgGraphLedgerRepo,
        ProjectionLeadership,
    )

    lease_until = datetime.now(UTC) + timedelta(seconds=30)
    result = _fencing_result(
        row={
            "owner_id": "projector-a",
            "generation": 12,
            "lease_until": lease_until,
            "armed": False,
        }
    )
    factory, session = _session_factory_with_results(result)
    repo = PgGraphLedgerRepo(factory)

    leadership = await repo.acquire_leadership("projector-a", lease_seconds=30)

    assert leadership == ProjectionLeadership(
        owner_id="projector-a",
        generation=12,
        lease_until=lease_until,
        armed=False,
    )
    statement, params = session.execute.await_args.args
    sql = " ".join(str(statement).lower().split())
    assert "graph_projection_leases" in sql
    assert "generation + 1" in sql
    assert "locked.neo4j_armed_generation = locked.generation" in sql
    assert "leased_until" in sql and "clock_timestamp()" in sql
    assert "neo4j_armed_generation" in sql
    assert params["owner_id"] == "projector-a"
    assert params["lease_seconds"] == 30
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_arm_leadership_is_a_live_owner_generation_cas() -> None:
    from brain_v42.repositories.pg_graph_ledger import (
        PgGraphLedgerRepo,
        ProjectionLeadership,
    )

    lease_until = datetime.now(UTC) + timedelta(seconds=30)
    result = _fencing_result(row={"generation": 12}, scalar=12)
    factory, session = _session_factory_with_results(result)
    repo = PgGraphLedgerRepo(factory)
    leadership = ProjectionLeadership("projector-a", 12, lease_until, False)

    armed = await repo.arm_leadership(leadership)

    assert armed is True
    statement, params = session.execute.await_args.args
    sql = " ".join(str(statement).lower().split())
    assert "neo4j_armed_generation" in sql
    assert "owner = :owner_id" in sql
    assert "generation = :generation" in sql
    assert "leased_until" in sql and "clock_timestamp()" in sql
    assert params["owner_id"] == leadership.owner_id
    assert params["generation"] == leadership.generation
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_release_leadership_preserves_arm_state_and_allows_unarmed_handover() -> None:
    from brain_v42.repositories.pg_graph_ledger import (
        PgGraphLedgerRepo,
        ProjectionLeadership,
    )

    lease_until = datetime.now(UTC) + timedelta(seconds=30)
    result = _fencing_result(scalar=12)
    factory, session = _session_factory_with_results(result)
    repo = PgGraphLedgerRepo(factory)
    leadership = ProjectionLeadership("projector-a", 12, lease_until, True)

    released = await repo.release_leadership(leadership)

    assert released is True
    statement, params = session.execute.await_args.args
    sql = " ".join(str(statement).lower().split())
    assert "owner = null" in sql
    assert "leased_until = null" in sql
    assert "neo4j_armed_generation" not in sql
    assert "leased_until > clock_timestamp()" in sql
    assert params["owner_id"] == "projector-a"
    assert params["generation"] == 12
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_claim_pending_requires_armed_live_leadership_and_reclaims_old_generation() -> None:
    from brain_v42.repositories.pg_graph_ledger import (
        PgGraphLedgerRepo,
        ProjectionClaim,
        ProjectionLeadership,
    )

    lease_until = datetime.now(UTC) + timedelta(seconds=30)
    entity_id = uuid4()
    event_id = uuid4()
    row = {
        "event_id": event_id,
        "operation": "upsert_entity",
        "aggregate_revision": 8,
        "owner_id": "projector-b",
        "lease_generation": 13,
        "claim_version": 2,
        "leased_until": lease_until,
        "entity_id": entity_id,
        "entity_type": "learning",
        "entity_key": str(entity_id),
        "source_uuid": entity_id,
        "project_key": "brain-v42",
        "display_label": "Fenced projection",
        "lifecycle": "active",
        "relation_id": None,
        "source_type": None,
        "source_key": None,
        "target_type": None,
        "target_key": None,
        "relation_type": None,
        "relation_lifecycle": None,
        "properties": None,
    }
    result = _fencing_result(rows=[row])
    factory, session = _session_factory_with_results(result)
    repo = PgGraphLedgerRepo(factory)
    leadership = ProjectionLeadership("projector-b", 13, lease_until, True)

    claims = await repo.claim_pending(
        leadership,
        limit=10,
        lease_seconds=30,
        max_attempts=5,
    )

    assert claims == [
        ProjectionClaim(
            event=claims[0].event,
            owner_id="projector-b",
            lease_generation=13,
            claim_version=2,
            leased_until=lease_until,
        )
    ]
    assert claims[0].event.event_id == event_id
    assert claims[0].event.aggregate_revision == 8
    statement, params = session.execute.await_args.args
    sql = " ".join(str(statement).lower().split())
    assert "graph_projection_leases" in sql
    assert "neo4j_armed_generation" in sql
    assert "leased_until" in sql and "clock_timestamp()" in sql
    assert "pending.lease_generation is distinct from leader.generation" in sql
    assert "claim_version" in sql and "+ 1" in sql
    assert "lease_generation" in sql
    assert params["owner_id"] == leadership.owner_id
    assert params["generation"] == leadership.generation
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_renew_claim_atomically_checks_and_extends_both_live_leases() -> None:
    from brain_v42.repositories.pg_graph_ledger import (
        GraphOutboxEvent,
        PgGraphLedgerRepo,
        ProjectionClaim,
    )

    old_until = datetime.now(UTC) + timedelta(seconds=10)
    renewed_until = datetime.now(UTC) + timedelta(seconds=40)
    claim = ProjectionClaim(
        event=GraphOutboxEvent(
            event_id=uuid4(),
            operation="upsert_entity",
            aggregate_revision=4,
        ),
        owner_id="projector-b",
        lease_generation=13,
        claim_version=5,
        leased_until=old_until,
    )
    result = _fencing_result(row={"leased_until": renewed_until})
    factory, session = _session_factory_with_results(result)
    repo = PgGraphLedgerRepo(factory)

    renewed = await repo.renew_claim(claim, lease_seconds=30)

    assert renewed == ProjectionClaim(
        event=claim.event,
        owner_id=claim.owner_id,
        lease_generation=claim.lease_generation,
        claim_version=claim.claim_version,
        leased_until=renewed_until,
    )
    assert session.execute.await_count == 1
    statement, params = session.execute.await_args.args
    sql = " ".join(str(statement).lower().split())
    assert "graph_projection_leases" in sql
    assert "graph_outbox" in sql
    assert "neo4j_armed_generation" in sql
    assert "leased_until" in sql
    assert sql.count("clock_timestamp()") >= 2
    assert "claim_version = :claim_version" in sql
    assert "lease_generation = :generation" in sql
    assert params["event_id"] == claim.event.event_id
    assert params["owner_id"] == claim.owner_id
    assert params["generation"] == claim.lease_generation
    assert params["claim_version"] == claim.claim_version
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_stale_claim_renewal_rolls_back_the_leadership_extension() -> None:
    from brain_v42.repositories.pg_graph_ledger import (
        GraphOutboxEvent,
        PgGraphLedgerRepo,
        ProjectionClaim,
    )

    claim = ProjectionClaim(
        event=GraphOutboxEvent(
            event_id=uuid4(),
            operation="upsert_entity",
            aggregate_revision=4,
        ),
        owner_id="projector-b",
        lease_generation=13,
        claim_version=5,
        leased_until=datetime.now(UTC) + timedelta(seconds=10),
    )
    result = _fencing_result(row=None)
    factory, session = _session_factory_with_results(result)
    repo = PgGraphLedgerRepo(factory)

    renewed = await repo.renew_claim(claim, lease_seconds=30)

    assert renewed is None
    session.rollback.assert_awaited_once()
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_stale_delivery_ack_is_rejected_by_owner_generation_claim_and_leases() -> None:
    from brain_v42.repositories.pg_graph_ledger import (
        GraphOutboxEvent,
        PgGraphLedgerRepo,
        ProjectionClaim,
    )

    claim = ProjectionClaim(
        event=GraphOutboxEvent(
            event_id=uuid4(),
            operation="upsert_entity",
            aggregate_revision=4,
        ),
        owner_id="projector-a",
        lease_generation=12,
        claim_version=3,
        leased_until=datetime.now(UTC) + timedelta(seconds=30),
    )
    result = _fencing_result(row=None, scalar=None)
    factory, session = _session_factory_with_results(result)
    repo = PgGraphLedgerRepo(factory)

    delivered = await repo.mark_delivered(claim)

    assert delivered is False
    statement, params = session.execute.await_args.args
    sql = " ".join(str(statement).lower().split())
    assert "graph_projection_leases" in sql
    assert "graph_outbox" in sql
    assert "neo4j_armed_generation" in sql
    assert "owner = :worker_id" in sql
    assert "lease_generation = :generation" in sql
    assert "claim_version = :claim_version" in sql
    assert "leased_until" in sql
    assert sql.count("clock_timestamp()") >= 2
    assert sql.count("for update") == 2
    assert params["event_id"] == claim.event.event_id
    assert params["owner_id"] == claim.owner_id
    assert params["generation"] == claim.lease_generation
    assert params["claim_version"] == claim.claim_version
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_stale_failure_cannot_release_or_increment_a_successor_claim() -> None:
    from brain_v42.repositories.pg_graph_ledger import (
        GraphOutboxEvent,
        PgGraphLedgerRepo,
        ProjectionClaim,
    )

    claim = ProjectionClaim(
        event=GraphOutboxEvent(
            event_id=uuid4(),
            operation="upsert_relation",
            aggregate_revision=6,
        ),
        owner_id="projector-a",
        lease_generation=12,
        claim_version=3,
        leased_until=datetime.now(UTC) + timedelta(seconds=30),
    )
    result = _fencing_result(row=None, scalar=None)
    factory, session = _session_factory_with_results(result)
    repo = PgGraphLedgerRepo(factory)
    raw_error = "neo4j://user:secret@host payload=private"

    failed = await repo.mark_failed(
        claim,
        raw_error,
        max_attempts=10,
    )

    assert failed is False
    statement, params = session.execute.await_args.args
    sql = " ".join(str(statement).lower().split())
    assert "graph_projection_leases" in sql
    assert "graph_outbox" in sql
    assert "neo4j_armed_generation" in sql
    assert "owner = :worker_id" in sql
    assert "lease_generation = :generation" in sql
    assert "claim_version = :claim_version" in sql
    assert "leased_until" in sql
    assert sql.count("clock_timestamp()") >= 2
    assert sql.count("for update") == 1
    assert params["event_id"] == claim.event.event_id
    assert params["owner_id"] == claim.owner_id
    assert params["generation"] == claim.lease_generation
    assert params["claim_version"] == claim.claim_version
    assert params["error_code"] == "projection_failed"
    assert raw_error not in repr(params)
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_list_active_classification_orphans_uses_canonical_filters() -> None:
    from brain_v42.repositories.pg_graph_ledger import PgGraphLedgerRepo

    source_uuid = uuid4()
    result = MagicMock()
    result.mappings.return_value.all.return_value = [
        {"source_uuid": source_uuid, "entity_type": "learning"}
    ]
    factory, session = _session_factory_with_results(result)
    repo = PgGraphLedgerRepo(factory)

    rows = await repo.list_active_classification_orphans(
        limit=7,
        project_key="brain-v42",
    )

    assert rows == [{"source_uuid": source_uuid, "entity_type": "learning"}]
    statement, params = session.execute.await_args.args
    sql = " ".join(str(statement).lower().split())
    assert "cast(:project_key as varchar) is null" in sql
    assert "candidate.lifecycle = 'active'" in sql
    assert "candidate.source_uuid is not null" in sql
    assert "relation.relation_type = 'related_to'" in sql
    assert "relation.source_entity_id = candidate.id" in sql
    assert "relation.target_entity_id = candidate.id" in sql
    assert "domain_relation.relation_type = 'belongs_to_domain'" in sql
    assert "domain_relation.source_entity_id = candidate.id" in sql
    assert "order by candidate.created_at, candidate.id" in sql
    assert "limit :limit" in sql
    assert params == {"limit": 7, "project_key": "brain-v42"}
    session.commit.assert_not_awaited()
