"""Cross-store crash/resume proof for projection recovery 035."""

from __future__ import annotations

from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from brain_v42.maintenance.graph_projection_recovery import recover_projection_lineage
from brain_v42.repositories.pg_graph_ledger import PgGraphLedgerRepo
from brain_v42.services.graph_projection_schema import ensure_graph_projection_schema
from brain_v42.services.neo4j_graph_projection_writer import Neo4jGraphProjectionWriter

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "crash_after",
    [
        "postgres_prepared",
        "neo4j_reset",
        "postgres_neo_ready",
        "neo4j_lost_after_postgres_neo_ready",
        "neo4j_finalized",
    ],
)
async def test_recovery_resumes_across_every_cross_store_commit_boundary(
    engine: AsyncEngine,
    neo4j_driver,
    neo4j_destructive_recovery,
    crash_after: str,
) -> None:
    await ensure_graph_projection_schema(neo4j_driver)
    connection = await engine.connect()
    outer = await connection.begin()
    factory = async_sessionmaker(
        connection,
        class_=AsyncSession,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    recovery_id = uuid4()
    worker_id = f"recovery:{recovery_id}"
    before_id = f"test-recovery-before-{recovery_id}"
    after_id = f"test-recovery-after-{recovery_id}"

    async with neo4j_driver.session() as session:
        original_result = await session.run(
            """
            MERGE (fence:BrainProjectionFence {name: 'canonical'})
            ON CREATE SET fence.generation = 0,
                          fence.owner_id = NULL,
                          fence.protocol_version = 2
            RETURN fence.generation AS generation,
                   fence.owner_id AS owner_id,
                   fence.protocol_version AS protocol_version,
                   fence.recovery_id AS recovery_id
            """
        )
        original_fence = dict(await original_result.single())
        base_generation = int(original_fence["generation"]) + 20
        await session.run(
            """
            MATCH (fence:BrainProjectionFence {name: 'canonical'})
            SET fence.generation = $generation,
                fence.owner_id = 'e2e-predecessor',
                fence.protocol_version = 2
            REMOVE fence.recovery_id
            MERGE (:Decision {id: $before_id})
            MERGE (:BrainProjectionCursor {
                aggregate_key: $cursor_key,
                revision: 1,
                claim_version: 1
            })
            """,
            {
                "generation": base_generation,
                "before_id": before_id,
                "cursor_key": f"entity:{before_id}",
            },
        )

    try:
        async with factory.begin() as session:
            await session.execute(
                sa.text(
                    """
                    UPDATE graph_projection_leases
                    SET generation = :generation,
                        owner = NULL,
                        leased_until = NULL,
                        neo4j_armed_generation = :generation,
                        recovery_id = NULL,
                        recovery_phase = 'idle',
                        last_completed_recovery_id = NULL
                    WHERE slot = 'neo4j'
                    """
                ),
                {"generation": base_generation},
            )

        repo = PgGraphLedgerRepo(factory)
        writer = Neo4jGraphProjectionWriter(neo4j_driver, timeout=3.0)
        preparation = await repo.prepare_projection_recovery(
            recovery_id,
            worker_id,
            lease_seconds=300,
        )
        assert preparation is not None and preparation.lease is not None
        state = preparation.lease

        if crash_after != "postgres_prepared":
            reset = await writer.reset_for_recovery(state)
            assert reset.accepted is True
        if crash_after in {
            "postgres_neo_ready",
            "neo4j_lost_after_postgres_neo_ready",
            "neo4j_finalized",
        }:
            ready = await repo.mark_projection_recovery_neo_ready(
                state,
                lease_seconds=300,
            )
            assert ready is not None
            state = ready
        if crash_after == "neo4j_lost_after_postgres_neo_ready":
            async with neo4j_driver.session() as session:
                await session.run(
                    "MATCH (fence:BrainProjectionFence {name: 'canonical'}) DELETE fence"
                )
        if crash_after == "neo4j_finalized":
            assert (await writer.finalize_recovery(state)).accepted is True

        report = await recover_projection_lineage(
            repo,
            writer,
            recovery_id=recovery_id,
            worker_id=worker_id,
            lease_seconds=300,
        )
        assert report.status == "recovered"
        assert report.generation == base_generation + 1

        async with factory() as session:
            row = (
                (
                    await session.execute(
                        sa.text(
                            """
                            SELECT recovery_id, recovery_phase,
                                   last_completed_recovery_id,
                                   neo4j_armed_generation,
                                   generation
                            FROM graph_projection_leases
                            WHERE slot = 'neo4j'
                            """
                        )
                    )
                )
                .mappings()
                .one()
            )
        assert row["recovery_id"] is None
        assert row["recovery_phase"] == "idle"
        assert row["last_completed_recovery_id"] == recovery_id
        assert row["neo4j_armed_generation"] == row["generation"]

        async with neo4j_driver.session() as session:
            fence_result = await session.run(
                """
                MATCH (fence:BrainProjectionFence {name: 'canonical'})
                RETURN fence.generation AS generation,
                       fence.recovery_id AS recovery_id
                """
            )
            fence = await fence_result.single()
            assert fence["generation"] == base_generation + 1
            assert fence["recovery_id"] is None
            await session.run("MERGE (:Decision {id: $after_id})", {"after_id": after_id})

        retry = await recover_projection_lineage(
            repo,
            writer,
            recovery_id=recovery_id,
            worker_id=worker_id,
            lease_seconds=300,
        )
        assert retry.status == "already_completed"
        async with neo4j_driver.session() as session:
            result = await session.run(
                "MATCH (node:Decision {id: $after_id}) RETURN count(node) AS node_count",
                {"after_id": after_id},
            )
            assert (await result.single())["node_count"] == 1
    finally:
        await outer.rollback()
        await connection.close()
        async with neo4j_driver.session() as session:
            await session.run(
                "MATCH (node) WHERE node.id IN [$before_id, $after_id] DETACH DELETE node",
                {"before_id": before_id, "after_id": after_id},
            )
            await session.run(
                """
                MATCH (fence:BrainProjectionFence {name: 'canonical'})
                SET fence.generation = $generation,
                    fence.owner_id = $owner_id,
                    fence.protocol_version = $protocol_version
                FOREACH (_ IN CASE WHEN $recovery_id IS NULL THEN [1] ELSE [] END |
                    REMOVE fence.recovery_id
                )
                FOREACH (_ IN CASE WHEN $recovery_id IS NULL THEN [] ELSE [1] END |
                    SET fence.recovery_id = $recovery_id
                )
                """,
                original_fence,
            )
