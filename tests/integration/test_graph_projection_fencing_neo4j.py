"""Real Neo4j proofs for the projection barrier and aggregate tombstones."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brain_v42.repositories.pg_graph_ledger import (
    GraphOutboxEvent,
    PgGraphLedgerRepo,
    ProjectionClaim,
    ProjectionLeadership,
    ProjectionRecoveryLease,
)
from brain_v42.services.graph_projection_schema import ensure_graph_projection_schema
from brain_v42.services.neo4j_graph_projection_writer import (
    Neo4jGraphProjectionWriter,
    ProjectionActivation,
    ProjectionOutcome,
)

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture(autouse=True)
async def _stable_projection_fence(neo4j_driver):  # type: ignore[misc]
    """Give every test an explicit fence; runtime activation may never bootstrap it."""
    await ensure_graph_projection_schema(neo4j_driver)
    async with neo4j_driver.session() as session:
        result = await session.run(
            """
            MERGE (fence:BrainProjectionFence {name: 'canonical'})
            ON CREATE SET fence.generation = 0,
                          fence.owner_id = NULL,
                          fence.protocol_version = 2
            RETURN fence.generation AS generation,
                   fence.owner_id AS owner_id,
                   fence.protocol_version AS protocol_version
            """
        )
        original = dict(await result.single())
    yield  # type: ignore[misc]
    async with neo4j_driver.session() as session:
        await session.run(
            """
            MATCH (fence:BrainProjectionFence {name: 'canonical'})
            SET fence.generation = $generation,
                fence.owner_id = $owner_id,
                fence.protocol_version = $protocol_version
            REMOVE fence.recovery_id
            """,
            original,
        )


class _CachedSingleResult:
    def __init__(self, record: Any) -> None:
        self._record = record

    async def single(self) -> Any:
        return self._record


class _PausingTransaction:
    def __init__(
        self,
        transaction: Any,
        *,
        fence_locked: asyncio.Event,
        resume: asyncio.Event,
    ) -> None:
        self._transaction = transaction
        self._fence_locked = fence_locked
        self._resume = resume
        self._paused = False

    async def run(self, query: str, parameters: dict[str, Any]) -> Any:
        result = await self._transaction.run(query, parameters)
        if not self._paused and "MERGE (cursor:BrainProjectionCursor" in query:
            self._paused = True
            record = await result.single()
            self._fence_locked.set()
            await self._resume.wait()
            return _CachedSingleResult(record)
        return result

    async def commit(self) -> None:
        await self._transaction.commit()

    async def rollback(self) -> None:
        await self._transaction.rollback()


class _PausingSession:
    def __init__(
        self,
        session: Any,
        *,
        fence_locked: asyncio.Event,
        resume: asyncio.Event,
    ) -> None:
        self._session = session
        self._fence_locked = fence_locked
        self._resume = resume

    async def begin_transaction(self, **kwargs: Any) -> _PausingTransaction:
        transaction = await self._session.begin_transaction(**kwargs)
        return _PausingTransaction(
            transaction,
            fence_locked=self._fence_locked,
            resume=self._resume,
        )


class _PausingSessionContext:
    def __init__(
        self,
        context: Any,
        *,
        fence_locked: asyncio.Event,
        resume: asyncio.Event,
    ) -> None:
        self._context = context
        self._fence_locked = fence_locked
        self._resume = resume

    async def __aenter__(self) -> _PausingSession:
        session = await self._context.__aenter__()
        return _PausingSession(
            session,
            fence_locked=self._fence_locked,
            resume=self._resume,
        )

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        return bool(await self._context.__aexit__(exc_type, exc, traceback))


class _PausingDriver:
    def __init__(
        self,
        driver: Any,
        *,
        fence_locked: asyncio.Event,
        resume: asyncio.Event,
    ) -> None:
        self._driver = driver
        self._fence_locked = fence_locked
        self._resume = resume

    def session(self, **kwargs: Any) -> _PausingSessionContext:
        return _PausingSessionContext(
            self._driver.session(**kwargs),
            fence_locked=self._fence_locked,
            resume=self._resume,
        )


def _leadership(
    owner: str,
    generation: int,
    *,
    armed: bool = False,
) -> ProjectionLeadership:
    return ProjectionLeadership(
        owner_id=owner,
        generation=generation,
        lease_until=datetime.now(UTC) + timedelta(seconds=30),
        armed=armed,
    )


def _claim(
    event: GraphOutboxEvent,
    *,
    owner: str,
    generation: int,
    claim_version: int,
) -> ProjectionClaim:
    return ProjectionClaim(
        event=event,
        owner_id=owner,
        lease_generation=generation,
        claim_version=claim_version,
        leased_until=datetime.now(UTC) + timedelta(seconds=30),
    )


async def test_activation_rejects_skipped_or_already_armed_lineage(
    neo4j_driver,
) -> None:
    await ensure_graph_projection_schema(neo4j_driver)
    async with neo4j_driver.session() as session:
        result = await session.run(
            """
            MATCH (fence:BrainProjectionFence {name: 'canonical'})
            RETURN fence.generation AS generation,
                   fence.owner_id AS owner_id,
                   fence.protocol_version AS protocol_version
            """
        )
        original = dict(await result.single())

    writer = Neo4jGraphProjectionWriter(neo4j_driver, timeout=3.0)
    try:
        skipped = await writer.activate_generation(
            _leadership("neo-skipped-lineage", int(original["generation"]) + 2)
        )
        assert skipped == ProjectionActivation(False, int(original["generation"]))

        already_armed = await writer.activate_generation(
            _leadership(
                "neo-armed-lineage",
                int(original["generation"]) + 1,
                armed=True,
            )
        )
        assert already_armed == ProjectionActivation(False, int(original["generation"]))
    finally:
        async with neo4j_driver.session() as session:
            await session.run(
                """
                MATCH (fence:BrainProjectionFence {name: 'canonical'})
                SET fence.generation = $generation,
                    fence.owner_id = $owner_id,
                    fence.protocol_version = $protocol_version
                """,
                original,
            )


async def test_successor_barrier_and_relation_tombstone_reject_stale_work(
    neo4j_driver,
) -> None:
    await ensure_graph_projection_schema(neo4j_driver)
    source_id, target_id = uuid4(), uuid4()
    relation_id = uuid4()
    stale_entity_id = uuid4()
    cursor_keys = [f"relation:{relation_id}", f"entity:{stale_entity_id}"]

    async with neo4j_driver.session() as session:
        generation_result = await session.run(
            """
            OPTIONAL MATCH (fence:BrainProjectionFence {name: 'canonical'})
            RETURN coalesce(fence.generation, 0) AS generation
            """
        )
        generation = int((await generation_result.single())["generation"]) + 1
        await session.run(
            """
            MERGE (:Decision {id: $source_id})
            MERGE (:Learning {id: $target_id})
            """,
            {"source_id": str(source_id), "target_id": str(target_id)},
        )
    writer = Neo4jGraphProjectionWriter(neo4j_driver, timeout=3.0)
    first_leader = _leadership("neo-integration-a", generation)
    assert (await writer.activate_generation(first_leader)).accepted is True

    created_event = GraphOutboxEvent(
        event_id=uuid4(),
        operation="upsert_relation",
        aggregate_revision=1,
        relation_id=relation_id,
        source_type="decision",
        source_key=str(source_id),
        target_type="learning",
        target_key=str(target_id),
        relation_type="RELATED_TO",
        relation_lifecycle="active",
    )
    deleted_event = GraphOutboxEvent(
        event_id=uuid4(),
        operation="delete_relation",
        aggregate_revision=2,
        relation_id=relation_id,
        source_type="decision",
        source_key=str(source_id),
        target_type="learning",
        target_key=str(target_id),
        relation_type="RELATED_TO",
        relation_lifecycle="deleted",
    )

    try:
        assert (
            await writer.apply(
                _claim(
                    created_event,
                    owner=first_leader.owner_id,
                    generation=generation,
                    claim_version=1,
                )
            )
            is ProjectionOutcome.APPLIED
        )
        assert (
            await writer.apply(
                _claim(
                    deleted_event,
                    owner=first_leader.owner_id,
                    generation=generation,
                    claim_version=1,
                )
            )
            is ProjectionOutcome.APPLIED
        )
        assert (
            await writer.apply(
                _claim(
                    created_event,
                    owner=first_leader.owner_id,
                    generation=generation,
                    claim_version=99,
                )
            )
            is ProjectionOutcome.SUPERSEDED
        )

        second_leader = _leadership("neo-integration-b", generation + 1)
        assert (await writer.activate_generation(second_leader)).accepted is True
        stale_entity = GraphOutboxEvent(
            event_id=uuid4(),
            operation="upsert_entity",
            aggregate_revision=1,
            entity_id=stale_entity_id,
            entity_type="decision",
            entity_key=str(stale_entity_id),
            source_uuid=stale_entity_id,
            project_key="brain-v42",
            display_label="must stay fenced",
            lifecycle="active",
        )
        assert (
            await writer.apply(
                _claim(
                    stale_entity,
                    owner=first_leader.owner_id,
                    generation=generation,
                    claim_version=1,
                )
            )
            is ProjectionOutcome.STALE_GENERATION
        )

        async with neo4j_driver.session() as session:
            result = await session.run(
                """
                MATCH (source {id: $source_id})
                MATCH (target {id: $target_id})
                OPTIONAL MATCH (source)-[relation:RELATED_TO]-(target)
                OPTIONAL MATCH (stale {id: $stale_entity_id})
                MATCH (cursor:BrainProjectionCursor {aggregate_key: $cursor_key})
                RETURN count(relation) AS relation_count,
                       count(stale) AS stale_entity_count,
                       cursor.revision AS cursor_revision,
                       cursor.operation AS cursor_operation
                """,
                {
                    "source_id": str(source_id),
                    "target_id": str(target_id),
                    "stale_entity_id": str(stale_entity_id),
                    "cursor_key": f"relation:{relation_id}",
                },
            )
            record = await result.single()
        assert record["relation_count"] == 0
        assert record["stale_entity_count"] == 0
        assert record["cursor_revision"] == 2
        assert record["cursor_operation"] == "delete_relation"
    finally:
        async with neo4j_driver.session() as session:
            await session.run(
                "MATCH (node) WHERE node.id IN $ids DETACH DELETE node",
                {"ids": [str(source_id), str(target_id), str(stale_entity_id)]},
            )
            await session.run(
                "MATCH (cursor:BrainProjectionCursor) "
                "WHERE cursor.aggregate_key IN $keys DELETE cursor",
                {"keys": cursor_keys},
            )


async def test_relation_tombstone_is_idempotent_after_target_detach_delete(
    neo4j_driver,
) -> None:
    await ensure_graph_projection_schema(neo4j_driver)
    source_id, target_id, relation_id = uuid4(), uuid4(), uuid4()
    cursor_key = f"relation:{relation_id}"

    async with neo4j_driver.session() as session:
        generation_result = await session.run(
            """
            MATCH (fence:BrainProjectionFence {name: 'canonical'})
            RETURN fence.generation AS generation
            """
        )
        generation = int((await generation_result.single())["generation"]) + 1
        await session.run(
            """
            MERGE (source:Decision {id: $source_id})
            MERGE (target:Learning {id: $target_id})
            MERGE (source)-[:RELATED_TO]->(target)
            WITH target
            DETACH DELETE target
            """,
            {"source_id": str(source_id), "target_id": str(target_id)},
        )
        missing_target_result = await session.run(
            "MATCH (target {id: $target_id}) RETURN count(target) AS target_count",
            {"target_id": str(target_id)},
        )
        assert (await missing_target_result.single())["target_count"] == 0

    writer = Neo4jGraphProjectionWriter(neo4j_driver, timeout=3.0)
    leader = _leadership("neo-missing-target", generation)
    event = GraphOutboxEvent(
        event_id=uuid4(),
        operation="delete_relation",
        aggregate_revision=2,
        relation_id=relation_id,
        source_type="decision",
        source_key=str(source_id),
        target_type="learning",
        target_key=str(target_id),
        relation_type="RELATED_TO",
        relation_lifecycle="deleted",
    )

    try:
        assert (await writer.activate_generation(leader)).accepted is True
        assert (
            await writer.apply(
                _claim(
                    event,
                    owner=leader.owner_id,
                    generation=leader.generation,
                    claim_version=1,
                )
            )
            is ProjectionOutcome.APPLIED
        )
        async with neo4j_driver.session() as session:
            result = await session.run(
                """
                MATCH (cursor:BrainProjectionCursor {aggregate_key: $cursor_key})
                RETURN cursor.revision AS revision,
                       cursor.operation AS operation
                """,
                {"cursor_key": cursor_key},
            )
            record = await result.single()
        assert record["revision"] == 2
        assert record["operation"] == "delete_relation"
    finally:
        async with neo4j_driver.session() as session:
            await session.run(
                "MATCH (node) WHERE node.id IN $ids DETACH DELETE node",
                {"ids": [str(source_id), str(target_id)]},
            )
            await session.run(
                "MATCH (cursor:BrainProjectionCursor {aggregate_key: $cursor_key}) DELETE cursor",
                {"cursor_key": cursor_key},
            )


async def test_relation_revision_replaces_removed_canonical_properties(
    neo4j_driver,
) -> None:
    await ensure_graph_projection_schema(neo4j_driver)
    source_id, target_id, relation_id = uuid4(), uuid4(), uuid4()
    cursor_key = f"relation:{relation_id}"

    async with neo4j_driver.session() as session:
        generation_result = await session.run(
            """
            MATCH (fence:BrainProjectionFence {name: 'canonical'})
            RETURN fence.generation AS generation
            """
        )
        generation = int((await generation_result.single())["generation"]) + 1
        await session.run(
            """
            MERGE (:Decision {id: $source_id})
            MERGE (:Learning {id: $target_id})
            """,
            {"source_id": str(source_id), "target_id": str(target_id)},
        )

    writer = Neo4jGraphProjectionWriter(neo4j_driver, timeout=3.0)
    leader = _leadership("neo-property-replacement", generation)

    def relation_event(revision: int, properties: dict[str, Any]) -> GraphOutboxEvent:
        return GraphOutboxEvent(
            event_id=uuid4(),
            operation="upsert_relation",
            aggregate_revision=revision,
            relation_id=relation_id,
            source_type="decision",
            source_key=str(source_id),
            target_type="learning",
            target_key=str(target_id),
            relation_type="RELATED_TO",
            relation_lifecycle="active",
            properties=properties,
        )

    try:
        assert (await writer.activate_generation(leader)).accepted is True
        assert (
            await writer.apply(
                _claim(
                    relation_event(1, {"score": 0.8, "model": "old-model"}),
                    owner=leader.owner_id,
                    generation=leader.generation,
                    claim_version=1,
                )
            )
            is ProjectionOutcome.APPLIED
        )
        assert (
            await writer.apply(
                _claim(
                    relation_event(2, {"score": 0.9}),
                    owner=leader.owner_id,
                    generation=leader.generation,
                    claim_version=1,
                )
            )
            is ProjectionOutcome.APPLIED
        )

        async with neo4j_driver.session() as session:
            result = await session.run(
                """
                MATCH ({id: $source_id})-[relation:RELATED_TO]-({id: $target_id})
                RETURN relation.score AS score,
                       relation.model AS model,
                       keys(relation) AS property_keys
                """,
                {"source_id": str(source_id), "target_id": str(target_id)},
            )
            record = await result.single()
        assert record["score"] == pytest.approx(0.9)
        assert record["model"] is None
        assert set(record["property_keys"]) == {"score"}
    finally:
        async with neo4j_driver.session() as session:
            await session.run(
                "MATCH (node) WHERE node.id IN $ids DETACH DELETE node",
                {"ids": [str(source_id), str(target_id)]},
            )
            await session.run(
                "MATCH (cursor:BrainProjectionCursor {aggregate_key: $cursor_key}) DELETE cursor",
                {"cursor_key": cursor_key},
            )


async def test_successor_activation_waits_for_inflight_predecessor_mutation(
    neo4j_driver,
) -> None:
    await ensure_graph_projection_schema(neo4j_driver)
    entity_id = uuid4()
    cursor_key = f"entity:{entity_id}"
    async with neo4j_driver.session() as session:
        result = await session.run(
            """
            MATCH (fence:BrainProjectionFence {name: 'canonical'})
            RETURN fence.generation AS generation
            """
        )
        generation = int((await result.single())["generation"]) + 1

    writer = Neo4jGraphProjectionWriter(neo4j_driver, timeout=3.0)
    predecessor = _leadership("neo-concurrent-a", generation)
    successor = _leadership("neo-concurrent-b", generation + 1)
    assert (await writer.activate_generation(predecessor)).accepted is True
    event = GraphOutboxEvent(
        event_id=uuid4(),
        operation="upsert_entity",
        aggregate_revision=1,
        entity_id=entity_id,
        entity_type="decision",
        entity_key=str(entity_id),
        source_uuid=entity_id,
        project_key="brain-v42",
        display_label="concurrent fence proof",
        lifecycle="active",
    )
    predecessor_claim = _claim(
        event,
        owner=predecessor.owner_id,
        generation=predecessor.generation,
        claim_version=1,
    )
    fence_locked = asyncio.Event()
    resume = asyncio.Event()
    pausing_writer = Neo4jGraphProjectionWriter(
        _PausingDriver(
            neo4j_driver,
            fence_locked=fence_locked,
            resume=resume,
        ),
        timeout=3.0,
    )
    apply_task = asyncio.create_task(pausing_writer.apply(predecessor_claim))
    activation_task: asyncio.Task[Any] | None = None
    try:
        await asyncio.wait_for(fence_locked.wait(), timeout=3.0)
        activation_task = asyncio.create_task(writer.activate_generation(successor))
        await asyncio.sleep(0.2)
        assert activation_task.done() is False

        resume.set()
        assert await asyncio.wait_for(apply_task, timeout=3.0) is ProjectionOutcome.APPLIED
        activation = await asyncio.wait_for(activation_task, timeout=3.0)
        assert activation.accepted is True
        assert activation.current_generation == successor.generation
        assert await writer.apply(predecessor_claim) is ProjectionOutcome.STALE_GENERATION

        async with neo4j_driver.session() as session:
            result = await session.run(
                """
                MATCH (node:Decision {id: $entity_id})
                MATCH (cursor:BrainProjectionCursor {aggregate_key: $cursor_key})
                RETURN count(node) AS node_count,
                       cursor.revision AS cursor_revision
                """,
                {"entity_id": str(entity_id), "cursor_key": cursor_key},
            )
            record = await result.single()
        assert record["node_count"] == 1
        assert record["cursor_revision"] == 1
    finally:
        resume.set()
        if not apply_task.done():
            apply_task.cancel()
        if activation_task is not None and not activation_task.done():
            activation_task.cancel()
        async with neo4j_driver.session() as session:
            await session.run(
                "MATCH (node {id: $entity_id}) DETACH DELETE node", {"entity_id": str(entity_id)}
            )
            await session.run(
                "MATCH (cursor:BrainProjectionCursor {aggregate_key: $cursor_key}) DELETE cursor",
                {"cursor_key": cursor_key},
            )


async def test_crash_after_neo_commit_replays_and_acks_with_successor(
    neo4j_driver,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await ensure_graph_projection_schema(neo4j_driver)
    suffix = uuid4().hex[:12]
    project_key = f"integ-fence-replay-{suffix}"
    async with session_factory.begin() as session:
        await session.execute(
            sa.text(
                """
                INSERT INTO project_contexts (project_key, name, description)
                VALUES (:project_key, :project_key, 'cross-store replay proof')
                """
            ),
            {"project_key": project_key},
        )
        feature_id = (
            await session.execute(
                sa.text(
                    """
                    INSERT INTO features (project_key, name, description)
                    VALUES (:project_key, :name, 'cross-store replay proof')
                    RETURNING id
                    """
                ),
                {"project_key": project_key, "name": f"replay-{suffix}"},
            )
        ).scalar_one()
    async with session_factory.begin() as session:
        event_id = (
            await session.execute(
                sa.text(
                    """
                    SELECT outbox.event_id
                    FROM graph_outbox AS outbox
                    JOIN brain_entities AS entity ON entity.id = outbox.entity_id
                    WHERE entity.source_uuid = :feature_id
                    ORDER BY outbox.aggregate_revision DESC
                    LIMIT 1
                    """
                ),
                {"feature_id": feature_id},
            )
        ).scalar_one()
        await session.execute(
            sa.text(
                "UPDATE graph_outbox SET available_at = '-infinity' WHERE event_id = :event_id"
            ),
            {"event_id": event_id},
        )

    async with neo4j_driver.session() as session:
        result = await session.run(
            """
            MATCH (fence:BrainProjectionFence {name: 'canonical'})
            RETURN fence.generation AS generation
            """
        )
        neo4j_generation = int((await result.single())["generation"])
    async with session_factory.begin() as session:
        await session.execute(
            sa.text(
                """
                UPDATE graph_projection_leases
                SET generation = :generation,
                    owner = NULL,
                    leased_until = NULL,
                    neo4j_armed_generation = :generation
                WHERE slot = 'neo4j'
                """
            ),
            {"generation": neo4j_generation},
        )

    repo_a = PgGraphLedgerRepo(session_factory)
    repo_b = PgGraphLedgerRepo(session_factory)
    writer = Neo4jGraphProjectionWriter(neo4j_driver, timeout=3.0)
    leader_a = await repo_a.acquire_leadership("cross-store-a", lease_seconds=60)
    assert leader_a is not None
    assert (await writer.activate_generation(leader_a)).accepted is True
    assert await repo_a.arm_leadership(leader_a) is True
    claim_a = (await repo_a.claim_pending(leader_a, limit=1, lease_seconds=60))[0]
    assert claim_a.event.event_id == event_id
    renewed_a = await repo_a.renew_claim(claim_a, lease_seconds=60)
    assert renewed_a is not None

    cursor_key = f"entity:{renewed_a.event.entity_id}"
    leader_b = None
    try:
        assert await writer.apply(renewed_a) is ProjectionOutcome.APPLIED

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
                    SET leased_until = clock_timestamp() - INTERVAL '1 second'
                    WHERE event_id = :event_id
                    """
                ),
                {"event_id": event_id},
            )

        leader_b = await repo_b.acquire_leadership("cross-store-b", lease_seconds=60)
        assert leader_b is not None
        assert leader_b.generation == leader_a.generation + 1
        assert (await writer.activate_generation(leader_b)).accepted is True
        assert await repo_b.arm_leadership(leader_b) is True
        claim_b = (await repo_b.claim_pending(leader_b, limit=1, lease_seconds=60))[0]
        assert claim_b.event.event_id == event_id
        assert claim_b.claim_version == claim_a.claim_version + 1
        renewed_b = await repo_b.renew_claim(claim_b, lease_seconds=60)
        assert renewed_b is not None

        assert await writer.apply(renewed_b) is ProjectionOutcome.ALREADY_CURRENT
        assert await repo_b.mark_delivered(renewed_b) is True
        assert await repo_a.mark_delivered(renewed_a) is False

        async with session_factory() as session:
            outbox = (
                (
                    await session.execute(
                        sa.text(
                            """
                        SELECT delivered_at, lease_owner, lease_generation, claim_version
                        FROM graph_outbox
                        WHERE event_id = :event_id
                        """
                        ),
                        {"event_id": event_id},
                    )
                )
                .mappings()
                .one()
            )
        assert outbox["delivered_at"] is not None
        assert outbox["lease_owner"] is None
        assert outbox["claim_version"] == renewed_b.claim_version

        async with neo4j_driver.session() as session:
            result = await session.run(
                """
                MATCH (node:Feature {id: $entity_id})
                MATCH (cursor:BrainProjectionCursor {aggregate_key: $cursor_key})
                RETURN count(node) AS node_count,
                       cursor.revision AS cursor_revision,
                       cursor.claim_version AS cursor_claim_version
                """,
                {"entity_id": str(feature_id), "cursor_key": cursor_key},
            )
            record = await result.single()
        assert record["node_count"] == 1
        assert record["cursor_revision"] == renewed_b.event.aggregate_revision
        assert record["cursor_claim_version"] == renewed_b.claim_version
    finally:
        if leader_b is not None:
            await repo_b.release_leadership(leader_b)
        async with neo4j_driver.session() as session:
            await session.run(
                "MATCH (node {id: $entity_id}) DETACH DELETE node",
                {"entity_id": str(feature_id)},
            )
            await session.run(
                "MATCH (cursor:BrainProjectionCursor {aggregate_key: $cursor_key}) DELETE cursor",
                {"cursor_key": cursor_key},
            )


async def test_recovery_reset_wipes_projection_then_finalizes_idempotently(
    neo4j_driver,
    neo4j_destructive_recovery,
) -> None:
    entity_id = f"test-recovery-{uuid4()}"
    foreign_id = f"test-foreign-{uuid4()}"
    recovery_id = uuid4()
    async with neo4j_driver.session() as session:
        result = await session.run(
            """
            MATCH (fence:BrainProjectionFence {name: 'canonical'})
            RETURN fence.generation AS generation
            """
        )
        generation = int((await result.single())["generation"]) + 1
        await session.run(
            """
            MERGE (:Decision {id: $entity_id})
            MERGE (:ForeignSentinel {id: $foreign_id})
            MERGE (:BrainProjectionCursor {
                aggregate_key: $cursor_key,
                revision: 3,
                claim_version: 1
            })
            """,
            {
                "entity_id": entity_id,
                "foreign_id": foreign_id,
                "cursor_key": f"entity:{entity_id}",
            },
        )

    prepared = ProjectionRecoveryLease(
        recovery_id=recovery_id,
        owner_id="recovery-owner",
        generation=generation,
        lease_until=datetime.now(UTC) + timedelta(minutes=5),
        phase="prepared",
    )
    writer = Neo4jGraphProjectionWriter(neo4j_driver, timeout=3.0)

    reset = await writer.reset_for_recovery(prepared)

    assert reset.accepted is True
    assert reset.current_generation == generation
    assert reset.deleted_nodes >= 2
    async with neo4j_driver.session() as session:
        result = await session.run(
            """
            MATCH (fence:BrainProjectionFence {name: 'canonical'})
            OPTIONAL MATCH (business)
            WHERE business:Decision OR business:BrainProjectionCursor
            OPTIONAL MATCH (foreign:ForeignSentinel {id: $foreign_id})
            RETURN fence.recovery_id AS recovery_id,
                   fence.owner_id AS owner_id,
                   fence.generation AS generation,
                   count(DISTINCT business) AS business_nodes,
                   count(DISTINCT foreign) AS foreign_nodes
            """,
            {"foreign_id": foreign_id},
        )
        record = await result.single()
    assert record["recovery_id"] == str(recovery_id)
    assert record["owner_id"] == "recovery-owner"
    assert record["generation"] == generation
    assert record["business_nodes"] == 0
    assert record["foreign_nodes"] == 1

    ready = ProjectionRecoveryLease(
        recovery_id=prepared.recovery_id,
        owner_id=prepared.owner_id,
        generation=prepared.generation,
        lease_until=prepared.lease_until,
        phase="neo_ready",
    )
    assert (await writer.finalize_recovery(ready)).accepted is True
    assert (await writer.finalize_recovery(ready)).accepted is True

    stale_reset = await writer.reset_for_recovery(prepared)
    assert stale_reset.accepted is False
    assert stale_reset.current_generation == generation


@pytest.mark.parametrize("conflict", ["newer_generation", "wrong_protocol"])
async def test_recovery_reset_rejects_conflicting_fence_without_deleting(
    neo4j_driver,
    neo4j_destructive_recovery,
    conflict: str,
) -> None:
    entity_id = f"test-recovery-conflict-{uuid4()}"
    async with neo4j_driver.session() as session:
        result = await session.run(
            "MATCH (fence:BrainProjectionFence {name: 'canonical'}) "
            "RETURN fence.generation AS generation"
        )
        original_generation = int((await result.single())["generation"])
        target_generation = original_generation + 1
        await session.run(
            """
            MATCH (fence:BrainProjectionFence {name: 'canonical'})
            SET fence.generation = $generation,
                fence.protocol_version = $protocol_version
            REMOVE fence.recovery_id
            MERGE (:Decision {id: $entity_id})
            """,
            {
                "generation": target_generation + (1 if conflict == "newer_generation" else -1),
                "protocol_version": 2 if conflict == "newer_generation" else 1,
                "entity_id": entity_id,
            },
        )

    state = ProjectionRecoveryLease(
        recovery_id=uuid4(),
        owner_id="recovery-conflict",
        generation=target_generation,
        lease_until=datetime.now(UTC) + timedelta(minutes=5),
        phase="prepared",
    )
    writer = Neo4jGraphProjectionWriter(neo4j_driver, timeout=3.0)

    rejected = await writer.reset_for_recovery(state)

    assert rejected.accepted is False
    async with neo4j_driver.session() as session:
        result = await session.run(
            "MATCH (node:Decision {id: $entity_id}) RETURN count(node) AS node_count",
            {"entity_id": entity_id},
        )
        assert (await result.single())["node_count"] == 1


async def test_runtime_activation_refuses_a_missing_fence_without_recreating_it(
    neo4j_driver,
) -> None:
    async with neo4j_driver.session() as session:
        await session.run("MATCH (fence:BrainProjectionFence) DELETE fence")

    writer = Neo4jGraphProjectionWriter(neo4j_driver, timeout=3.0)
    activation = await writer.activate_generation(_leadership("runtime-owner", 1))

    assert activation == ProjectionActivation(False, -1)
    async with neo4j_driver.session() as session:
        result = await session.run(
            "MATCH (fence:BrainProjectionFence {name: 'canonical'}) "
            "RETURN count(fence) AS fence_count"
        )
        assert (await result.single())["fence_count"] == 0
