"""Contracts for the bounded Neo4j -> PostgreSQL legacy graph import."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from brain_v42.services.legacy_graph_import import (
    LegacyGraphImportBlocked,
    LegacyGraphImporter,
)
from brain_v42.services.legacy_graph_snapshot import (
    LegacyGraphNode,
    LegacyGraphRelation,
    LegacyGraphSnapshot,
)


class FakeResult:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def mappings(self) -> FakeResult:
        return self

    def all(self) -> list[dict[str, Any]]:
        return self._rows


def _session_factory(results: list[FakeResult]) -> tuple[MagicMock, AsyncMock]:
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=results)
    context = AsyncMock()
    context.__aenter__ = AsyncMock(return_value=session)
    context.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=context), session


def _snapshot(*, truncated: bool = False) -> LegacyGraphSnapshot:
    entity_id = uuid4()
    return LegacyGraphSnapshot(
        nodes=(
            LegacyGraphNode(
                entity_type="decision",
                entity_key=str(entity_id),
                source_uuid=entity_id,
                project_key="brain-v42",
                scope_kind="project",
                display_label="Bounded title",
            ),
            LegacyGraphNode(
                entity_type="project",
                entity_key="brain-v42",
                source_uuid=None,
                project_key="brain-v42",
                scope_kind="project",
                display_label="Brain v42",
            ),
        ),
        relations=(
            LegacyGraphRelation(
                source_type="decision",
                source_key=str(entity_id),
                target_type="project",
                target_key="brain-v42",
                relation_type="BELONGS_TO",
                properties={},
            ),
        ),
        truncated_nodes=truncated,
    )


def test_importer_rejects_batches_above_the_asyncpg_bind_budget() -> None:
    with pytest.raises(ValueError, match="1000"):
        LegacyGraphImporter(MagicMock(), batch_size=1001)


@pytest.mark.asyncio
async def test_dry_run_never_opens_a_postgres_session() -> None:
    factory = MagicMock()
    importer = LegacyGraphImporter(factory)

    report = await importer.import_snapshot(_snapshot(), apply=False)

    assert report.applied is False
    assert report.candidate_entities == 2
    assert report.candidate_relations == 1
    assert report.imported_entities == 0
    assert report.imported_relations == 0
    factory.assert_not_called()


@pytest.mark.asyncio
async def test_dry_run_exposes_snapshot_truncation() -> None:
    report = await LegacyGraphImporter(MagicMock()).import_snapshot(
        _snapshot(truncated=True),
        apply=False,
    )

    assert report.truncated_nodes is True
    assert report.truncated_relations is False


@pytest.mark.asyncio
async def test_dry_run_reports_only_canonical_project_identities() -> None:
    snapshot = _snapshot()
    aliased = LegacyGraphSnapshot(
        nodes=(
            *snapshot.nodes,
            LegacyGraphNode(
                entity_type="project",
                entity_key="brain_v42",
                source_uuid=None,
                project_key="brain",
                scope_kind="project",
                display_label="Alias",
            ),
        ),
        relations=(
            *snapshot.relations,
            LegacyGraphRelation(
                source_type="project",
                source_key="brain",
                target_type="decision",
                target_key=snapshot.nodes[0].entity_key,
                relation_type="RELATED_TO",
                properties={},
            ),
        ),
    )

    report = await LegacyGraphImporter(MagicMock()).import_snapshot(aliased, apply=False)

    assert report.candidate_entities == 2
    assert report.canonical_project_keys == ("brain-v42",)


@pytest.mark.asyncio
async def test_apply_refuses_a_truncated_snapshot_before_opening_postgres() -> None:
    factory = MagicMock()
    importer = LegacyGraphImporter(factory)

    with pytest.raises(LegacyGraphImportBlocked, match="truncated"):
        await importer.import_snapshot(_snapshot(truncated=True), apply=True)

    factory.assert_not_called()


@pytest.mark.asyncio
async def test_apply_refuses_source_skips_before_opening_postgres() -> None:
    factory = MagicMock()
    snapshot = _snapshot()
    snapshot = LegacyGraphSnapshot(
        nodes=snapshot.nodes,
        relations=snapshot.relations,
        skipped_nodes=1,
    )

    with pytest.raises(LegacyGraphImportBlocked, match="skipped"):
        await LegacyGraphImporter(factory).import_snapshot(snapshot, apply=True)

    factory.assert_not_called()


@pytest.mark.asyncio
async def test_apply_inserts_idempotently_and_marks_initial_outbox_events_delivered() -> None:
    snapshot = _snapshot()
    decision_id = snapshot.nodes[0].source_uuid
    assert decision_id is not None
    project_registry_id = uuid4()
    relation_id = uuid4()
    entity_rows = [
        {
            "id": decision_id,
            "entity_type": "decision",
            "entity_key": str(decision_id),
            "revision": 1,
            "lifecycle": "active",
        },
        {
            "id": project_registry_id,
            "entity_type": "project",
            "entity_key": "brain-v42",
            "revision": 1,
            "lifecycle": "active",
        },
    ]
    factory, session = _session_factory(
        [
            FakeResult([]),
            FakeResult([{"id": decision_id}, {"id": project_registry_id}]),
            FakeResult(entity_rows),
            FakeResult([{"id": relation_id}]),
            FakeResult([{"event_id": uuid4()}, {"event_id": uuid4()}]),
            FakeResult([{"event_id": uuid4()}]),
        ]
    )
    importer = LegacyGraphImporter(factory, batch_size=100)

    report = await importer.import_snapshot(snapshot, apply=True)

    assert report.applied is True
    assert report.imported_entities == 2
    assert report.imported_relations == 1
    assert report.acknowledged_entities == 2
    assert report.acknowledged_relations == 1
    assert report.skipped_relations == 0
    session.commit.assert_awaited_once()
    session.rollback.assert_not_awaited()

    statements = [call.args[0] for call in session.execute.await_args_list]
    rendered = [
        str(statement.compile(dialect=__import__("sqlalchemy").dialects.postgresql.dialect()))
        for statement in statements
    ]
    assert "ON CONFLICT (project_key) DO NOTHING" in rendered[0]
    assert "ON CONFLICT DO NOTHING" in rendered[1]
    assert "legacy_neo4j" in str(statements[1].compile().params)
    assert "ON CONFLICT DO NOTHING" in rendered[3]
    assert "legacy_neo4j" in str(statements[3].compile().params)
    assert "delivered_at" in rendered[4]
    assert "ON CONFLICT DO NOTHING" in rendered[4]
    assert "delivered_at" in rendered[5]


@pytest.mark.asyncio
async def test_apply_does_not_acknowledge_post_snapshot_revisions() -> None:
    snapshot = _snapshot()
    decision_id = snapshot.nodes[0].source_uuid
    assert decision_id is not None
    project_registry_id = uuid4()
    entity_rows = [
        {
            "id": decision_id,
            "entity_type": "decision",
            "entity_key": str(decision_id),
            "revision": 2,
            "lifecycle": "active",
        },
        {
            "id": project_registry_id,
            "entity_type": "project",
            "entity_key": "brain-v42",
            "revision": 2,
            "lifecycle": "active",
        },
    ]
    factory, session = _session_factory(
        [
            FakeResult([]),
            FakeResult([]),
            FakeResult(entity_rows),
            FakeResult([]),
        ]
    )

    report = await LegacyGraphImporter(factory).import_snapshot(snapshot, apply=True)

    assert report.acknowledged_entities == 0
    assert report.acknowledged_relations == 0
    assert len(session.execute.await_args_list) == 4


@pytest.mark.asyncio
async def test_apply_leaves_existing_revision_one_events_pending_for_convergence() -> None:
    snapshot = _snapshot()
    decision_id = snapshot.nodes[0].source_uuid
    assert decision_id is not None
    project_registry_id = uuid4()
    entity_rows = [
        {
            "id": decision_id,
            "entity_type": "decision",
            "entity_key": str(decision_id),
            "revision": 1,
            "lifecycle": "active",
        },
        {
            "id": project_registry_id,
            "entity_type": "project",
            "entity_key": "brain-v42",
            "revision": 1,
            "lifecycle": "active",
        },
    ]
    factory, session = _session_factory(
        [
            FakeResult([]),
            FakeResult([]),
            FakeResult(entity_rows),
            FakeResult([]),
        ]
    )

    report = await LegacyGraphImporter(factory).import_snapshot(snapshot, apply=True)

    assert report.acknowledged_entities == 0
    assert report.acknowledged_relations == 0
    assert len(session.execute.await_args_list) == 4


@pytest.mark.asyncio
async def test_apply_rolls_back_when_an_endpoint_is_unresolved() -> None:
    snapshot = _snapshot()
    decision_id = snapshot.nodes[0].source_uuid
    assert decision_id is not None
    entity_rows = [
        {
            "id": decision_id,
            "entity_type": "decision",
            "entity_key": str(decision_id),
            "revision": 1,
            "lifecycle": "active",
        }
    ]
    factory, session = _session_factory(
        [
            FakeResult([]),
            FakeResult([]),
            FakeResult(entity_rows),
            FakeResult([{"event_id": uuid4()}]),
        ]
    )

    with pytest.raises(LegacyGraphImportBlocked, match="unresolved"):
        await LegacyGraphImporter(factory).import_snapshot(snapshot, apply=True)

    session.rollback.assert_awaited_once()
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_allow_skips_commits_only_resolvable_facts() -> None:
    snapshot = _snapshot()
    decision_id = snapshot.nodes[0].source_uuid
    assert decision_id is not None
    entity_rows = [
        {
            "id": decision_id,
            "entity_type": "decision",
            "entity_key": str(decision_id),
            "revision": 1,
            "lifecycle": "active",
        }
    ]
    factory, session = _session_factory(
        [
            FakeResult([]),
            FakeResult([]),
            FakeResult(entity_rows),
        ]
    )

    report = await LegacyGraphImporter(factory).import_snapshot(
        snapshot,
        apply=True,
        allow_skips=True,
    )

    assert report.imported_relations == 0
    assert report.skipped_relations == 1
    assert report.skipped_nodes == 1
    assert report.acknowledged_entities == 0
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_apply_rolls_back_the_whole_import_on_postgres_error() -> None:
    factory, session = _session_factory([FakeResult([])])
    session.execute = AsyncMock(side_effect=RuntimeError("database unavailable"))

    with pytest.raises(RuntimeError, match="database unavailable"):
        await LegacyGraphImporter(factory).import_snapshot(_snapshot(), apply=True)

    session.rollback.assert_awaited_once()
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_rolls_back_when_a_new_fact_lacks_its_delivered_event() -> None:
    snapshot = _snapshot()
    decision_id = snapshot.nodes[0].source_uuid
    assert decision_id is not None
    project_registry_id = uuid4()
    relation_id = uuid4()
    entity_rows = [
        {
            "id": decision_id,
            "entity_type": "decision",
            "entity_key": str(decision_id),
            "revision": 1,
            "lifecycle": "active",
        },
        {
            "id": project_registry_id,
            "entity_type": "project",
            "entity_key": "brain-v42",
            "revision": 1,
            "lifecycle": "active",
        },
    ]
    factory, session = _session_factory(
        [
            FakeResult([]),
            FakeResult([{"id": decision_id}, {"id": project_registry_id}]),
            FakeResult(entity_rows),
            FakeResult([{"id": relation_id}]),
            FakeResult([{"event_id": uuid4()}]),
        ]
    )

    with pytest.raises(LegacyGraphImportBlocked, match="outbox"):
        await LegacyGraphImporter(factory).import_snapshot(snapshot, apply=True)

    session.rollback.assert_awaited_once()
    session.commit.assert_not_awaited()
