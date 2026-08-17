"""Concurrency coverage for migration 033 relation lifecycle triggers."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from brain_v42.repositories.pg_graph_ledger import PgGraphLedgerRepo

pytestmark = pytest.mark.integration


async def test_concurrent_endpoint_reactivation_restores_relation(
    engine: AsyncEngine,
) -> None:
    suffix = uuid4().hex[:12]
    project_key = f"integ-graph-{suffix}"
    first_name = f"relation-first-{suffix}"
    second_name = f"relation-second-{suffix}"

    async with engine.begin() as connection:
        await connection.execute(
            sa.text(
                """
                INSERT INTO project_contexts (project_key, name, description)
                VALUES (:project_key, :project_key, 'relation lifecycle test')
                """
            ),
            {"project_key": project_key},
        )
        first_id = (
            await connection.execute(
                sa.text(
                    """
                    INSERT INTO features (project_key, name, description)
                    VALUES (:project_key, :name, 'first endpoint')
                    RETURNING id
                    """
                ),
                {"project_key": project_key, "name": first_name},
            )
        ).scalar_one()
        second_id = (
            await connection.execute(
                sa.text(
                    """
                    INSERT INTO features (project_key, name, description)
                    VALUES (:project_key, :name, 'second endpoint')
                    RETURNING id
                    """
                ),
                {"project_key": project_key, "name": second_name},
            )
        ).scalar_one()
        await connection.execute(
            sa.text(
                """
                INSERT INTO entity_relations (
                    source_entity_id,
                    target_entity_id,
                    relation_type,
                    origin,
                    confidence
                ) VALUES (:first_id, :second_id, 'RELATED_TO', 'integration', 1.0)
                """
            ),
            {"first_id": first_id, "second_id": second_id},
        )
        await connection.execute(
            sa.text("UPDATE features SET status = 'archived' WHERE id = :id"),
            {"id": first_id},
        )
        await connection.execute(
            sa.text("UPDATE features SET status = 'archived' WHERE id = :id"),
            {"id": second_id},
        )

    async with engine.connect() as first_connection, engine.connect() as second_connection:
        first_transaction = await first_connection.begin()
        second_transaction = await second_connection.begin()
        await first_connection.execute(
            sa.text("UPDATE features SET status = 'planned' WHERE id = :id"),
            {"id": first_id},
        )
        second_update = asyncio.create_task(
            second_connection.execute(
                sa.text("UPDATE features SET status = 'planned' WHERE id = :id"),
                {"id": second_id},
            )
        )
        await asyncio.sleep(0.1)
        await first_transaction.commit()
        await asyncio.wait_for(second_update, timeout=5)
        await second_transaction.commit()

    async with engine.connect() as connection:
        lifecycle = (
            await connection.execute(
                sa.text(
                    """
                    SELECT lifecycle
                    FROM entity_relations
                    WHERE source_entity_id = :first_id
                      AND target_entity_id = :second_id
                      AND relation_type = 'RELATED_TO'
                    """
                ),
                {"first_id": first_id, "second_id": second_id},
            )
        ).scalar_one()

    assert lifecycle == "active"


async def test_relation_stage_locks_endpoints_against_concurrent_archive(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    suffix = uuid4().hex[:12]
    project_key = f"integ-lock-{suffix}"
    async with session_factory.begin() as session:
        await session.execute(
            sa.text(
                """
                INSERT INTO project_contexts (project_key, name, description)
                VALUES (:project_key, :project_key, 'endpoint lock test')
                """
            ),
            {"project_key": project_key},
        )
        first_id = (
            await session.execute(
                sa.text(
                    """
                    INSERT INTO features (project_key, name, description)
                    VALUES (:project_key, :name, 'first endpoint')
                    RETURNING id
                    """
                ),
                {"project_key": project_key, "name": f"first-{suffix}"},
            )
        ).scalar_one()
        second_id = (
            await session.execute(
                sa.text(
                    """
                    INSERT INTO features (project_key, name, description)
                    VALUES (:project_key, :name, 'second endpoint')
                    RETURNING id
                    """
                ),
                {"project_key": project_key, "name": f"second-{suffix}"},
            )
        ).scalar_one()

    repo = PgGraphLedgerRepo(session_factory)
    async with session_factory() as lock_session, session_factory() as archive_session:
        await lock_session.begin()
        await archive_session.begin()
        await repo._resolve_uuid_endpoints(lock_session, first_id, second_id)
        archive = asyncio.create_task(
            archive_session.execute(
                sa.text("UPDATE features SET status = 'archived' WHERE id = :id"),
                {"id": first_id},
            )
        )
        await asyncio.sleep(0.1)
        assert not archive.done()
        await lock_session.commit()
        await asyncio.wait_for(archive, timeout=5)
        await archive_session.commit()


async def test_identical_relation_stage_is_revision_and_provenance_idempotent(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    suffix = uuid4().hex[:12]
    project_key = f"integ-idem-{suffix}"
    async with session_factory.begin() as session:
        await session.execute(
            sa.text(
                """
                INSERT INTO project_contexts (project_key, name, description)
                VALUES (:project_key, :project_key, 'idempotent stage test')
                """
            ),
            {"project_key": project_key},
        )
        ids = []
        for position in ("first", "second"):
            ids.append(
                (
                    await session.execute(
                        sa.text(
                            """
                            INSERT INTO features (project_key, name, description)
                            VALUES (:project_key, :name, 'idempotent endpoint')
                            RETURNING id
                            """
                        ),
                        {"project_key": project_key, "name": f"{position}-{suffix}"},
                    )
                ).scalar_one()
            )

    repo = PgGraphLedgerRepo(session_factory)
    first_event = await repo.stage_uuid_relation(
        ids[0],
        ids[1],
        "RELATED_TO",
        props={"similarity": 0.91},
        origin="auto_linker",
        confidence=0.91,
    )
    second_event = await repo.stage_uuid_relation(
        ids[0],
        ids[1],
        "RELATED_TO",
        props={"similarity": 0.91},
        origin="explicit",
        confidence=0.91,
    )

    async with session_factory() as session:
        row = (
            (
                await session.execute(
                    sa.text(
                        """
                    SELECT relation.revision,
                           relation.origin,
                           COUNT(outbox.id) AS event_count
                    FROM entity_relations AS relation
                    JOIN graph_outbox AS outbox ON outbox.relation_id = relation.id
                    WHERE relation.id = :relation_id
                    GROUP BY relation.revision, relation.origin
                    """
                    ),
                    {"relation_id": first_event.relation_id},
                )
            )
            .mappings()
            .one()
        )

    assert second_event.event_id == first_event.event_id
    assert row == {"revision": 1, "origin": "auto_linker", "event_count": 1}


async def test_full_projection_requeue_covers_every_current_aggregate(
    engine: AsyncEngine,
) -> None:
    async with engine.connect() as connection:
        transaction = await connection.begin()
        isolated_factory = async_sessionmaker(
            connection,
            class_=AsyncSession,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        repo = PgGraphLedgerRepo(isolated_factory)
        try:
            before = await repo.projection_inventory()

            report = await repo.requeue_full_projection()
            after = await repo.projection_inventory()

            assert report.entity_events == before.entity_count
            assert report.relation_events == before.relation_count
            assert after.entity_count == before.entity_count
            assert after.relation_count == before.relation_count
            assert after.pending_count >= report.entity_events + report.relation_events
        finally:
            await transaction.rollback()


async def test_explicit_relation_delete_accepts_an_archived_endpoint(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    suffix = uuid4().hex[:12]
    project_key = f"integ-delete-{suffix}"
    async with session_factory.begin() as session:
        await session.execute(
            sa.text(
                """
                INSERT INTO project_contexts (project_key, name, description)
                VALUES (:project_key, :project_key, 'archived endpoint delete test')
                """
            ),
            {"project_key": project_key},
        )
        ids = []
        for position in ("first", "second"):
            ids.append(
                (
                    await session.execute(
                        sa.text(
                            """
                            INSERT INTO features (project_key, name, description)
                            VALUES (:project_key, :name, 'relation delete endpoint')
                            RETURNING id
                            """
                        ),
                        {"project_key": project_key, "name": f"{position}-{suffix}"},
                    )
                ).scalar_one()
            )

    repo = PgGraphLedgerRepo(session_factory)
    relation = await repo.stage_uuid_relation(ids[0], ids[1], "RELATED_TO")
    async with session_factory.begin() as session:
        await session.execute(
            sa.text("UPDATE features SET status = 'archived' WHERE id = :id"),
            {"id": ids[0]},
        )

    deleted = await repo.stage_uuid_relation_delete(ids[0], ids[1], "RELATED_TO")

    async with session_factory() as session:
        lifecycle = (
            await session.execute(
                sa.text("SELECT lifecycle FROM entity_relations WHERE id = :id"),
                {"id": relation.relation_id},
            )
        ).scalar_one()
    assert deleted.relation_id == relation.relation_id
    assert lifecycle == "deleted"


async def test_merged_source_and_lineage_remain_projectable(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    suffix = uuid4().hex[:12]
    project_key = f"integ-merge-{suffix}"
    async with session_factory.begin() as session:
        await session.execute(
            sa.text(
                """
                INSERT INTO project_contexts (project_key, name, description)
                VALUES (:project_key, :project_key, 'merge lineage test')
                """
            ),
            {"project_key": project_key},
        )
        source_id = (
            await session.execute(
                sa.text(
                    """
                    INSERT INTO features (project_key, name, description)
                    VALUES (:project_key, :name, 'merged source') RETURNING id
                    """
                ),
                {"project_key": project_key, "name": f"source-{suffix}"},
            )
        ).scalar_one()
        target_id = (
            await session.execute(
                sa.text(
                    """
                    INSERT INTO features (project_key, name, description)
                    VALUES (:project_key, :name, 'merge target') RETURNING id
                    """
                ),
                {"project_key": project_key, "name": f"target-{suffix}"},
            )
        ).scalar_one()
        await session.execute(
            sa.text("UPDATE features SET merged_into = :target WHERE id = :source"),
            {"source": source_id, "target": target_id},
        )

    async with session_factory() as session:
        row = (
            (
                await session.execute(
                    sa.text(
                        """
                    SELECT source.lifecycle AS source_lifecycle,
                           relation.lifecycle AS relation_lifecycle,
                           entity_event.operation AS entity_operation,
                           relation_event.operation AS relation_operation
                    FROM brain_entities AS source
                    JOIN entity_relations AS relation
                      ON relation.source_entity_id = source.id
                     AND relation.relation_type = 'MERGED_INTO'
                    JOIN graph_outbox AS entity_event
                      ON entity_event.entity_id = source.id
                     AND entity_event.aggregate_revision = source.revision
                    JOIN graph_outbox AS relation_event
                      ON relation_event.relation_id = relation.id
                     AND relation_event.aggregate_revision = relation.revision
                    WHERE source.source_uuid = :source_id
                    """
                    ),
                    {"source_id": source_id},
                )
            )
            .mappings()
            .one()
        )

    assert row == {
        "source_lifecycle": "archived",
        "relation_lifecycle": "active",
        "entity_operation": "upsert_entity",
        "relation_operation": "upsert_relation",
    }


async def test_project_context_key_is_immutable_after_creation(
    engine: AsyncEngine,
) -> None:
    suffix = uuid4().hex[:12]
    project_key = f"integ-immutable-{suffix}"
    async with engine.begin() as connection:
        await connection.execute(
            sa.text(
                """
                INSERT INTO project_contexts (project_key, name, description)
                VALUES (:project_key, :project_key, 'immutable key test')
                """
            ),
            {"project_key": project_key},
        )

    async with engine.connect() as connection:
        transaction = await connection.begin()
        with pytest.raises(Exception, match="project_contexts.project_key is immutable"):
            await connection.execute(
                sa.text(
                    "UPDATE project_contexts SET project_key = :new_key "
                    "WHERE project_key = :old_key"
                ),
                {"old_key": project_key, "new_key": f"{project_key}-renamed"},
            )
        await transaction.rollback()

    async with engine.connect() as connection:
        current_key = (
            await connection.execute(
                sa.text("SELECT project_key FROM project_contexts WHERE project_key = :key"),
                {"key": project_key},
            )
        ).scalar_one()
    assert current_key == project_key


async def test_auxiliary_project_reference_creates_registry_and_projection_event(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    suffix = uuid4().hex[:12]
    project_key = f"integ-reference-{suffix}"

    async with session_factory.begin() as session:
        await session.execute(
            sa.text(
                """
                INSERT INTO search_log (
                    tool_name,
                    project_key,
                    result_count,
                    latency_ms
                ) VALUES ('brain_search', :project_key, 0, 1.0)
                """
            ),
            {"project_key": project_key},
        )

    async with session_factory() as session:
        row = (
            (
                await session.execute(
                    sa.text(
                        """
                    SELECT project.registry_status,
                           entity.entity_type,
                           entity.lifecycle,
                           outbox.operation
                    FROM projects AS project
                    JOIN brain_entities AS entity
                      ON entity.entity_type = 'project'
                     AND entity.entity_key = project.project_key
                    JOIN graph_outbox AS outbox
                      ON outbox.entity_id = entity.id
                     AND outbox.aggregate_revision = entity.revision
                    WHERE project.project_key = :project_key
                    """
                    ),
                    {"project_key": project_key},
                )
            )
            .mappings()
            .one()
        )

    assert row == {
        "registry_status": "unclaimed",
        "entity_type": "project",
        "lifecycle": "active",
        "operation": "upsert_entity",
    }


async def test_named_membership_scope_is_revalidated_under_the_registry_lock(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    suffix = uuid4().hex[:12]
    original_project = f"integ-scope-{suffix}"
    moved_project = f"integ-moved-{suffix}"
    async with session_factory.begin() as session:
        for project_key in (original_project, moved_project):
            await session.execute(
                sa.text(
                    """
                    INSERT INTO project_contexts (project_key, name, description)
                    VALUES (:project_key, :project_key, 'named target scope test')
                    """
                ),
                {"project_key": project_key},
            )
        entity_id = (
            await session.execute(
                sa.text(
                    """
                    INSERT INTO features (project_key, name, description)
                    VALUES (:project_key, :name, 'named target scope endpoint')
                    RETURNING id
                    """
                ),
                {"project_key": original_project, "name": f"scope-{suffix}"},
            )
        ).scalar_one()

    repo = PgGraphLedgerRepo(session_factory)
    async with session_factory() as lock_session, session_factory() as move_session:
        await lock_session.begin()
        await move_session.begin()
        endpoints = await repo._resolve_named_target(
            lock_session,
            entity_id=entity_id,
            target_type="domain",
            target_key="infra",
        )
        assert endpoints["source_project_key"] == original_project

        move = asyncio.create_task(
            move_session.execute(
                sa.text("UPDATE features SET project_key = :project_key WHERE id = :id"),
                {"project_key": moved_project, "id": entity_id},
            )
        )
        await asyncio.sleep(0.1)
        assert not move.done()
        await lock_session.commit()
        await asyncio.wait_for(move, timeout=5)
        await move_session.commit()

    with pytest.raises(ValueError, match="authorized project"):
        await repo.stage_domain_membership(
            entity_id,
            "infra",
            project_key=original_project,
        )


async def test_successful_revision_retires_older_terminal_dead_letter(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    suffix = uuid4().hex[:12]
    project_key = f"integ-deadletter-{suffix}"
    async with session_factory.begin() as session:
        await session.execute(
            sa.text(
                """
                INSERT INTO project_contexts (project_key, name, description)
                VALUES (:project_key, :project_key, 'dead letter retirement test')
                """
            ),
            {"project_key": project_key},
        )
        entity_id = (
            await session.execute(
                sa.text(
                    """
                    INSERT INTO features (project_key, name, description)
                    VALUES (:project_key, :name, 'dead letter aggregate')
                    RETURNING id
                    """
                ),
                {"project_key": project_key, "name": f"deadletter-{suffix}"},
            )
        ).scalar_one()
        await session.execute(
            sa.text(
                """
                UPDATE graph_outbox
                SET attempt_count = 10,
                    available_at = 'infinity'::timestamptz,
                    last_error_code = 'max_attempts'
                WHERE entity_id = :entity_id AND aggregate_revision = 1
                """
            ),
            {"entity_id": entity_id},
        )
        await session.execute(
            sa.text("UPDATE features SET status = 'archived' WHERE id = :id"),
            {"id": entity_id},
        )

    async with session_factory() as session:
        current_event_id = (
            await session.execute(
                sa.text(
                    """
                    SELECT event_id
                    FROM graph_outbox
                    WHERE entity_id = :entity_id AND aggregate_revision = 2
                    """
                ),
                {"entity_id": entity_id},
            )
        ).scalar_one()

    async with session_factory.begin() as session:
        await session.execute(
            sa.text(
                """
                UPDATE graph_projection_leases
                SET leased_until = clock_timestamp() - INTERVAL '1 second'
                WHERE slot = 'neo4j'
                """
            )
        )
        await session.execute(
            sa.text(
                """
                UPDATE graph_outbox
                SET available_at = '-infinity'::timestamptz
                WHERE event_id = :event_id
                """
            ),
            {"event_id": current_event_id},
        )

    repo = PgGraphLedgerRepo(session_factory)
    leader = await repo.acquire_leadership("dead-letter-retirement", lease_seconds=30)
    assert leader is not None
    assert await repo.arm_leadership(leader) is True
    claims = await repo.claim_pending(leader, limit=100, lease_seconds=30)
    current_claim = next(claim for claim in claims if claim.event.event_id == current_event_id)
    assert await repo.mark_delivered(current_claim) is True
    assert await repo.release_leadership(leader) is True

    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    sa.text(
                        """
                        SELECT aggregate_revision,
                               delivered_at IS NOT NULL AS delivered,
                               last_error_code
                        FROM graph_outbox
                        WHERE entity_id = :entity_id
                          AND aggregate_revision IN (1, 2)
                        ORDER BY aggregate_revision
                        """
                    ),
                    {"entity_id": entity_id},
                )
            )
            .mappings()
            .all()
        )

    assert rows == [
        {"aggregate_revision": 1, "delivered": True, "last_error_code": "superseded"},
        {"aggregate_revision": 2, "delivered": True, "last_error_code": None},
    ]
