"""Focused batching contracts for legacy graph PostgreSQL persistence."""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from brain_v42.services.legacy_graph_models import StoredEntity
from brain_v42.services.legacy_graph_snapshot import LegacyGraphNode, LegacyGraphRelation
from brain_v42.services.legacy_graph_store import PgLegacyGraphStore


@pytest.mark.asyncio
async def test_project_inserts_respect_the_configured_batch_size() -> None:
    session = AsyncMock()
    store = PgLegacyGraphStore(session, batch_size=1)
    nodes = [
        LegacyGraphNode("project", "alpha", None, "alpha", "project", "Alpha"),
        LegacyGraphNode("project", "beta", None, "beta", "project", "Beta"),
    ]

    await store.ensure_projects(nodes)

    assert session.execute.await_count == 2


@pytest.mark.asyncio
async def test_referenced_project_without_node_registers_its_canonical_entity() -> None:
    session = AsyncMock()
    session.execute.return_value = FakeResult()
    store = PgLegacyGraphStore(session, batch_size=10)
    nodes = [
        LegacyGraphNode(
            "decision",
            "decision",
            None,
            "test-pock",
            "project",
            "Decision",
        )
    ]

    await store.ensure_projects(nodes)

    assert session.execute.await_count == 2
    registration = session.execute.await_args_list[1].args[0]
    rendered = str(
        registration.compile(dialect=__import__("sqlalchemy").dialects.postgresql.dialect())
    )
    assert "register_referenced_project" in rendered
    assert "test-pock" in registration.compile().params.values()


@pytest.mark.asyncio
async def test_store_canonicalizes_known_project_aliases_before_persistence() -> None:
    session = AsyncMock()
    session.execute.return_value = FakeResult()
    store = PgLegacyGraphStore(session, batch_size=10)
    nodes = [
        LegacyGraphNode("project", "brain_v42", None, "brain_v42", "project", "Brain"),
        LegacyGraphNode("decision", "decision", None, "brain", "project", "Decision"),
        LegacyGraphNode(
            "project",
            "auto_discord",
            None,
            "auto_discord",
            "project",
            "Auto Discord",
        ),
    ]

    await store.ensure_projects(nodes)
    await store.insert_entities(nodes)

    project_params = session.execute.await_args_list[0].args[0].compile().params
    entity_params = session.execute.await_args_list[1].args[0].compile().params
    assert "brain-v42" in project_params.values()
    assert "brain" not in project_params.values()
    assert "brain_v42" not in project_params.values()
    assert "auto-discord" in project_params.values()
    assert "auto_discord" not in project_params.values()
    assert "brain-v42" in entity_params.values()
    assert "brain" not in entity_params.values()
    assert "brain_v42" not in entity_params.values()
    assert "auto-discord" in entity_params.values()
    assert "auto_discord" not in entity_params.values()


class FakeResult:
    def mappings(self) -> FakeResult:
        return self

    def all(self) -> list[dict]:
        return []


@pytest.mark.asyncio
async def test_related_to_is_canonicalized_by_registry_uuid_after_resolution() -> None:
    high_id = UUID(int=100)
    low_id = UUID(int=1)
    session = AsyncMock()
    session.execute.return_value = FakeResult()
    store = PgLegacyGraphStore(session, batch_size=10)
    entities = {
        ("decision", "source"): StoredEntity(high_id, "decision", "source", 1, "active"),
        ("learning", "target"): StoredEntity(low_id, "learning", "target", 1, "active"),
    }
    relation = LegacyGraphRelation(
        "decision",
        "source",
        "learning",
        "target",
        "RELATED_TO",
        {},
    )

    await store.insert_relations([relation], entities)

    statement = session.execute.await_args.args[0]
    params = statement.compile().params
    assert params["source_entity_id_m0"] == low_id
    assert params["target_entity_id_m0"] == high_id


@pytest.mark.asyncio
async def test_relation_alias_endpoints_resolve_to_the_canonical_project() -> None:
    decision_id = UUID(int=10)
    learning_id = UUID(int=20)
    project_id = UUID(int=30)
    other_project_id = UUID(int=40)
    session = AsyncMock()
    session.execute.return_value = FakeResult()
    store = PgLegacyGraphStore(session, batch_size=10)
    entities = {
        ("decision", "decision"): StoredEntity(decision_id, "decision", "decision", 1, "active"),
        ("learning", "learning"): StoredEntity(learning_id, "learning", "learning", 1, "active"),
        ("project", "brain-v42"): StoredEntity(project_id, "project", "brain-v42", 1, "active"),
        ("project", "red-monitor"): StoredEntity(
            other_project_id, "project", "red-monitor", 1, "active"
        ),
    }
    relations = [
        LegacyGraphRelation("decision", "decision", "project", "brain_v42", "BELONGS_TO", {}),
        LegacyGraphRelation("project", "brain", "project", "red-monitor", "DEPENDS_ON", {}),
    ]

    inserted, skipped = await store.insert_relations(relations, entities)

    assert inserted == []
    assert skipped == 0
    params = session.execute.await_args.args[0].compile().params
    assert project_id in params.values()


@pytest.mark.asyncio
async def test_store_skips_relations_outside_the_canonical_shape_matrix() -> None:
    decision_id = UUID(int=10)
    project_id = UUID(int=20)
    session = AsyncMock()
    session.execute.return_value = FakeResult()
    store = PgLegacyGraphStore(session, batch_size=10)
    entities = {
        ("decision", "decision"): StoredEntity(decision_id, "decision", "decision", 1, "active"),
        ("project", "brain-v42"): StoredEntity(project_id, "project", "brain-v42", 1, "active"),
    }
    relation = LegacyGraphRelation(
        "decision",
        "decision",
        "project",
        "brain-v42",
        "DEPENDS_ON",
        {},
    )

    inserted, skipped = await store.insert_relations([relation], entities)

    assert inserted == []
    assert skipped == 1
    session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_store_keeps_lineage_with_an_archived_endpoint() -> None:
    source_id = UUID(int=10)
    target_id = UUID(int=20)
    session = AsyncMock()
    session.execute.return_value = FakeResult()
    store = PgLegacyGraphStore(session, batch_size=10)
    entities = {
        ("feature", "source"): StoredEntity(source_id, "feature", "source", 2, "archived"),
        ("feature", "target"): StoredEntity(target_id, "feature", "target", 1, "active"),
    }
    relation = LegacyGraphRelation(
        "feature",
        "source",
        "feature",
        "target",
        "MERGED_INTO",
        {},
    )

    inserted, skipped = await store.insert_relations([relation], entities)

    assert inserted == []
    assert skipped == 0
    statement = session.execute.await_args.args[0]
    assert "MERGED_INTO" in statement.compile().params.values()


@pytest.mark.asyncio
async def test_store_rejects_lineage_with_a_deleted_endpoint() -> None:
    source_id = UUID(int=10)
    target_id = UUID(int=20)
    session = AsyncMock()
    store = PgLegacyGraphStore(session, batch_size=10)
    entities = {
        ("feature", "source"): StoredEntity(source_id, "feature", "source", 2, "deleted"),
        ("feature", "target"): StoredEntity(target_id, "feature", "target", 1, "active"),
    }
    relation = LegacyGraphRelation(
        "feature",
        "source",
        "feature",
        "target",
        "MERGED_INTO",
        {},
    )

    inserted, skipped = await store.insert_relations([relation], entities)

    assert inserted == []
    assert skipped == 1
    session.execute.assert_not_awaited()
