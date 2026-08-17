"""Real PostgreSQL classification proof for graph outbox go/no-go metrics."""

from __future__ import annotations

from unittest.mock import MagicMock
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from brain_v42.metrics.collector import MetricsCollector

pytestmark = pytest.mark.integration


async def test_graph_outbox_metrics_classify_ready_claimed_delayed_and_exhausted(
    engine: AsyncEngine,
) -> None:
    connection = await engine.connect()
    outer = await connection.begin()
    factory = async_sessionmaker(
        connection,
        class_=AsyncSession,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    entity_ids = [uuid4() for _ in range(4)]

    try:
        await connection.execute(
            sa.text("UPDATE graph_outbox SET delivered_at = clock_timestamp()")
        )
        for position, entity_id in enumerate(entity_ids):
            await connection.execute(
                sa.text(
                    "INSERT INTO brain_entities "
                    "(id, entity_type, entity_key, scope_kind) "
                    "VALUES (:id, 'decision', :key, 'global')"
                ),
                {"id": entity_id, "key": f"metrics-outbox-{position}-{entity_id}"},
            )

        await connection.execute(
            sa.text(
                """
                INSERT INTO graph_outbox (
                    entity_id, aggregate_revision, operation, available_at,
                    leased_until, lease_owner, lease_generation,
                    delivered_at, last_error_code, created_at
                ) VALUES
                    (:ready_id, 1, 'upsert_entity',
                     clock_timestamp() - INTERVAL '1 minute',
                     NULL, NULL, NULL, NULL, NULL,
                     clock_timestamp() - INTERVAL '2 minutes'),
                    (:claimed_id, 1, 'upsert_entity',
                     clock_timestamp() - INTERVAL '1 minute',
                     clock_timestamp() + INTERVAL '5 minutes',
                     'metrics-projector', 42, NULL, NULL,
                     clock_timestamp() - INTERVAL '90 seconds'),
                    (:delayed_id, 1, 'upsert_entity',
                     clock_timestamp() + INTERVAL '5 minutes',
                     NULL, NULL, NULL, NULL, NULL,
                     clock_timestamp() - INTERVAL '1 minute'),
                    (:exhausted_id, 1, 'upsert_entity',
                     clock_timestamp() - INTERVAL '1 minute',
                     NULL, NULL, NULL, NULL, 'max_attempts',
                     clock_timestamp() - INTERVAL '3 minutes')
                """
            ),
            {
                "ready_id": entity_ids[0],
                "claimed_id": entity_ids[1],
                "delayed_id": entity_ids[2],
                "exhausted_id": entity_ids[3],
            },
        )
        await connection.execute(
            sa.text(
                """
                UPDATE graph_projection_leases
                SET generation = 42,
                    owner = 'metrics-projector',
                    leased_until = clock_timestamp() + INTERVAL '5 minutes',
                    neo4j_armed_generation = 42,
                    recovery_id = NULL,
                    recovery_phase = 'idle'
                WHERE slot = 'neo4j'
                """
            )
        )

        metrics_engine = MagicMock()
        metrics_engine.sync_engine.pool.size.return_value = 5
        metrics_engine.sync_engine.pool.checkedout.return_value = 0
        metrics_engine.sync_engine.pool.checkedin.return_value = 1
        metrics_engine.sync_engine.pool.overflow.return_value = -4
        metrics_engine.sync_engine.pool._max_overflow = 10
        graph_outbox = (
            await MetricsCollector(
                engine=metrics_engine,
                session_factory=factory,
            ).collect_db_stats()
        )["graph_outbox"]

        assert graph_outbox["available"] is True
        assert graph_outbox["pending"] == 3
        assert graph_outbox["ready"] == 1
        assert graph_outbox["claimed"] == 1
        assert graph_outbox["exhausted"] == 1
        assert 119.0 <= graph_outbox["oldest_pending_age_seconds"] < 150.0
        assert graph_outbox["projector"] == {
            "generation": 42,
            "armed": True,
            "lease_active": True,
            "recovery_active": False,
            "healthy": True,
        }
    finally:
        await outer.rollback()
        await connection.close()
